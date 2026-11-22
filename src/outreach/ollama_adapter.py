"""Ollama adapter (ADR-4): the department's ONLY AI endpoint, fail-closed.

- Talks exclusively to localhost Ollama (gate #9: no other listener is ever
  touched; non-loopback URLs are refused before any I/O).
- `generate()` retries a transient/model failure up to 3× (ADR-4).
- On exhaustion `ask_with_guard()` flips the queue to PAUSED and appends a
  HALT record (gate #5: fail-closed — an AI outage must never result in
  unqualified outreach being sent). The dispatcher (B9 wired) refuses to
  dispatch while `queue_status == "PAUSED"`.
- stdlib transport only (urllib), per ADR-1.
"""
from __future__ import annotations

import functools
import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Any

if TYPE_CHECKING:
    from outreach.store import Database

DEFAULT_MODEL = "llama3.1:8b"
# Literal IPv4 loopback, NOT the name "localhost": on this host getaddrinfo
# returns ::1 first, Ollama serves v4 only, and a post-crash ::1 SYN can hang
# in connect with no RST (2026-10-26: judge calls wedged, backfill stalled).
OLLAMA_URL = "http://127.0.0.1:11434"
REQUEST_TIMEOUT_S = 120.0
# Owner, 2026-11-15: "it's a resource hog… nothing should be hogging resources."
# Ollama's default keeps a model resident for FIVE minutes after each request, and
# the engine asks the model small things every cycle, so an 8B model (4.92 GB) held
# its whole footprint against a 6 GB card all day and left the IG browsers ~1.2 GB.
# A short keepalive keeps a burst warm (each request refreshes it) and lets the
# model leave memory between bursts; one cold load is the only cost.
KEEP_ALIVE_S = 30
# Owner order 2026-11-15: "pause the llama thing so it can happen after 8:55pm
# everyday and not before that". A WINDOW, not a throttle: before 20:55 local no
# request is made at all (a 4.92 GB model on a 6 GB card otherwise competes with
# the Instagram browsers all day). The window is local wall-clock time.
MODEL_WINDOW_START_MIN = 20 * 60 + 55  # 20:55 local
MAX_RETRIES = 3
_HALT_LOG_CAP = 100

Transport = Callable[[str, dict], dict]


class OllamaDead(RuntimeError):
    """Ollama failed after the full retry budget — the queue must pause."""


class ModelWindowClosed(RuntimeError):
    """The owner's model window has not opened yet (before 20:55 local).

    Deliberately NOT an `OllamaDead`: a window that has not opened is the
    owner's schedule, not a fault, so it must never pause the queue or write a
    HALT record. Each caller degrades the way it already knows how.
    """


def model_window_open(now: datetime | None = None) -> bool:
    """Is the owner's local model window open right now?

    Open from 20:55 to midnight local. An explicit `now` always decides on its
    own, which is how the window's math is pinned. Without one, the test seam
    `OUTREACH_MODEL_WINDOW` may force `open`/`closed` (the suite sets `open` so
    a fake transport does not have to wait for the evening); otherwise the local
    clock decides. Production never sets that variable.
    """
    if now is None:
        override = os.environ.get("OUTREACH_MODEL_WINDOW", "").strip().lower()
        if override in ("open", "always"):
            return True
        if override in ("closed", "never"):
            return False
    moment = now if now is not None else datetime.now()
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.hour * 60 + moment.minute >= MODEL_WINDOW_START_MIN


def _assert_local(base_url: str) -> None:
    host = urllib.request.urlsplit(base_url).hostname or ""
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(
            f"refusing non-localhost AI endpoint {base_url!r} — "
            "the department only ever touches local Ollama (gate #9)"
        )


def _real_transport(
    url: str, payload: dict, *, timeout_s: float = REQUEST_TIMEOUT_S
) -> dict:
    # A second gate at the wire itself: before 20:55 local NO request reaches
    # the model (owner order 2026-11-15), whatever path asked. It reads the same
    # switch as `generate()`; only the suite's explicit seam (untouched in
    # production) opens it early, which lets the transport's own timeout test
    # run at any hour without beating the owner's clock.
    if not model_window_open():
        raise ModelWindowClosed(
            "local model window is closed (opens 20:55 local) - the transport"
            " refused the request"
        )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _strip_fences(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def generate(
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = OLLAMA_URL,
    expect_json: bool = False,
    retries: int = MAX_RETRIES,
    sleep_s: float = 0.5,
    transport: Transport | None = None,
    options: dict[str, Any] | None = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
    now: datetime | None = None,
) -> Any:
    """One Ollama chat turn; parsed JSON when `expect_json`.

    Raises `ModelWindowClosed` outside the owner's window WITHOUT touching the
    model; `now` is injectable so the window is testable without a clock.

    Raises `OllamaDead` only after the full retry budget — never sends
    partial content, never falls back to guessing.

    `timeout_s` is the per-socket read deadline. Production keeps 120 s (a
    cold model load is slow); a caller that must not stall — the gate's live
    tests — passes its own short deadline (R12: a wedged service returned
    zero bytes forever instead of failing, and a hang reports nothing).
    """
    _assert_local(base_url)
    if not model_window_open(now):
        raise ModelWindowClosed(
            "local model window is closed (opens 20:55 local, owner order"
            " 2026-11-15) - no request was made"
        )
    send = transport or functools.partial(_real_transport, timeout_s=timeout_s)
    url = f"{base_url}/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "keep_alive": f"{KEEP_ALIVE_S}s",
    }
    if options:
        payload["options"] = dict(options)
    last_error: Exception | None = None
    import time

    for attempt in range(retries):
        try:
            response = send(url, payload)
            content = response["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("Ollama content is not a string")
        except ModelWindowClosed:
            # The window shut between attempts (23:59:59 -> 00:00). That is the
            # owner's clock, not a dead model: retrying it would spend three
            # attempts and then hand the caller an `OllamaDead`, which
            # `ask_with_guard` turns into a queue PAUSE at midnight.
            raise
        except Exception as exc:  # noqa: BLE001 — any transport/shape failure retries
            last_error = exc
            if sleep_s:
                time.sleep(sleep_s)
            continue
        if not expect_json:
            return content
        try:
            return json.loads(_strip_fences(content))
        except json.JSONDecodeError as exc:
            # Live 2026-11-09: the model answers a correct object and then
            # adds prose (`{...}\n\nHope that helps`), so `json.loads` said
            # "Extra data: line 6" and the adapter fail-closed the whole
            # queue every few minutes. Reading the first complete object is
            # still strict about content - it never invents a continuation -
            # and it stops a cosmetic model habit from pausing dispatch.
            try:
                first, _end = json.JSONDecoder().raw_decode(
                    _strip_fences(content).strip()
                )
            except json.JSONDecodeError:
                last_error = exc
                if sleep_s:
                    time.sleep(sleep_s)
            else:
                return first
    raise OllamaDead(f"Ollama unusable after {retries} attempt(s): {last_error!r}")


def fail_closed_pause(db: "Database", *, reason: str, component: str = "ollama") -> None:
    """PAUSE the queue + append the HALT record (append-capped at 100)."""
    from outreach.store import get_config, set_config

    set_config(db, "queue_status", "PAUSED")
    halts = list(get_config(db, "halts", []) or [])
    halts.append(
        {
            "component": component,
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )
    set_config(db, "halts", halts[-_HALT_LOG_CAP:])


def ask_with_guard(
    db: "Database",
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    base_url: str = OLLAMA_URL,
    expect_json: bool = True,
    retries: int = MAX_RETRIES,
    sleep_s: float = 0.5,
    transport: Transport | None = None,
    options: dict[str, Any] | None = None,
    timeout_s: float = REQUEST_TIMEOUT_S,
    now: datetime | None = None,
) -> Any:
    """`generate()` with the fail-closed law wired to this database.

    A closed model window is the owner's schedule, not a failure: it propagates
    untouched so no caller pauses the queue over it."""
    try:
        return generate(
            prompt,
            model=model,
            base_url=base_url,
            expect_json=expect_json,
            retries=retries,
            sleep_s=sleep_s,
            transport=transport,
            options=options,
            timeout_s=timeout_s,
            now=now,
        )
    except ModelWindowClosed:
        raise
    except OllamaDead as exc:
        fail_closed_pause(db, reason=str(exc), component="ollama")
        raise
