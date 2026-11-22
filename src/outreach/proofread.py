"""The proofread wall: a pre-dispatch sanity gate on the FINAL rendered body.

The owner's rule (2026-11-07): the guards upstream judge the dish CANDIDATE -
is this word really a dish, is it in the reviews. Nobody judged the SENTENCE
those guards produce, so `Saw the pies post` could reach a restaurateur's
inbox as our first impression.

Law: the wall VETOES, never edits. Owner copy is byte-pinned; a flagged
message goes to a hold queue with the reason and the lead is left exactly as
it was, for a retry after review. `review()` returns a Verdict and touches
nothing else - the rendered bytes cannot change here, by construction.

Two layers:
  1. deterministic, always on, Ollama-independent (this is the wall while the
     local model is down, which is most of the time on this machine);
  2. a model second opinion, fail-open: if Ollama is unreachable the message
     is judged by layer 1 alone and the verdict says so. It never stalls a
     send window waiting for a model.

SCOPE OF LAYER 2: generated copy only (the variant engine's composed lines
and filled packet bodies). Owner-written bytes that are hash-pinned are
judged by layer 1 alone, because a model's taste must never retire the
owner's own sentence: live evidence 2026-11-09, the local judge scored the
pinned day-3 rung copy 0.20 ("abruptly switches to restaurant hours") and
would have held every rung in the ladder on day one. Callers that hand it
pinned copy pass no judge and the verdict records layers="1".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from outreach.store import Database

#: Instagram's DM ceiling; the wall refuses anything the transport would split.
MAX_BODY_CHARS = 1000
#: Sentences a first-touch may run to before it reads like a paragraph.
#: The pinned v1.2 opener+P.S. is 7 (`Salam!`, the question, P.S., the product
#: sentence, the closing), so the line sits above real copy, not across it.
MAX_SENTENCES = 8
#: Below this it is not a message.
MIN_BODY_CHARS = 15
#: The only emoji the owner's copy ships (U+1F604, smiling face).
ALLOWED_EMOJI = frozenset({"\U0001F604"})

_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{[^{}]*\}")
_BRACE = re.compile(r"[{}]")
_NULLISH = re.compile(r"\b(None|null|NULL|undefined|NaN|nan)\b")
_EM_DASH = "\u2014"
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_ARTICLE = re.compile(r"\b(a|an)\s+([A-Za-z][\w'\-]*)", re.IGNORECASE)
#: `Saw the pies post` / `that kebabs reel` - a plural dish used as a
#: singular modifier. The live failures were exactly this shape.
_PLURAL_MODIFIER = re.compile(
    r"\b(?:the|that|this|your|their)\s+([a-z]+(?:ies|es|s))\s+"
    r"(?:post|reel|photo|picture|plate|dish|menu|feed)\b",
    re.IGNORECASE,
)
#: Words that are a MENU or a marketing phrase, never a plate. Vetoing one of
#: these is right ("what made you pick X for your food?" proves nothing).
#: LIVE 2026-11-09: this set used to hold real dishes - poutine, kebab, lamb,
#: noodle, pizza - so three leads whose dish was literally `poutine`, `lamb`,
#: `noodle` were held for naming the thing we came to talk about. Concrete
#: food never belongs in here.
CATEGORY_NOUNS = frozenset(
    {
        "food", "cuisine", "menu", "dishes", "specials", "specialties",
        "flavors", "flavours", "meat", "halal", "breakfast", "lunch",
        "dinner", "catering",
    }
)
#: Owner ruling 2026-11-11: the opener reacts to what the guest SAID; the
#: existence of reviews is never the hook ("saw your reviews" is banned). The
#: owner's V2 body names the review in the third person ("X's reviews said
#: ..."), which is allowed: the ban is on addressing the lead about their
#: reviews as a fact, not on quoting one.
_REVIEW_EXISTENCE = re.compile(
    r"\b(?:saw|read|seen|noticed|checked|loved)\s+(?:your|ur)\s+"
    r"(?:reviews?|ratings?|stars?)\b"
    r"|\byour\s+(?:reviews?|ratings?)\s+(?:are|say|said|look|were)\b",
    re.IGNORECASE,
)
#: A model answer whose ONLY complaint is that no dish appears. The wall's law
#: is coherence of what IS there; absence is the no-dish variants' job.
#: Ways a model says the WRITING is broken, as opposed to saying the message
#: names no dish. On a no-dish render only these may hold a send (2026-11-15).
_WRITING_FAULTS = (
    "verb", "grammar", "gibberish", "nonsense", "incoherent", "not a sentence",
    "unreadable", "typo",
)
_ABSENCE_MARKERS = (
    "no dish",
    "no food",
    "no specific dish",
    "no menu item",
    "does not mention",
    "doesn't mention",
    "mentions no",
)
_VOWEL_EXCEPTIONS = ("uni", "use", "usu", "one", "once", "euro", "ubiq")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]"
)


@dataclass(frozen=True)
class Verdict:
    """The wall's answer. `ok=False` means hold; the body is untouched."""

    ok: bool
    reason: str
    layers: str  # "1" or "1+2"
    score: float | None = None
    issues: tuple[str, ...] = ()
    #: Advisory only (owner ruling 2026-11-11). Recorded, never a veto input.
    tone_score: float | None = None
    tone_note: str = ""

    @property
    def layer1_only(self) -> bool:
        return self.layers == "1"


Judge = Callable[[str, str | None], "tuple[bool, float, str]"]


# ---------------------------------------------------------------------------
# layer 1 - deterministic
# ---------------------------------------------------------------------------


def _article_issue(body: str) -> list[str]:
    issues: list[str] = []
    for match in _ARTICLE.finditer(body):
        article, word = match.group(1).lower(), match.group(2).lower()
        vowel_sound = word[:1] in "aeiou" and not word.startswith(_VOWEL_EXCEPTIONS)
        if vowel_sound and article == "a":
            issues.append(f"article agreement: 'a {word}' needs 'an'")
        elif not vowel_sound and article == "an":
            issues.append(f"article agreement: 'an {word}' needs 'a'")
    return issues


def _layer1(body: str, dish: str | None) -> list[str]:
    issues: list[str] = []
    if not body or not body.strip():
        return ["empty body"]
    # (a) fill integrity: an unfilled slot is never sendable text.
    if _PLACEHOLDER.search(body) or _BRACE.search(body):
        issues.append("unfilled placeholder or orphan brace")
    if _NULLISH.search(body):
        issues.append("literal None/null in the body")
    # (b) grammar shape around the dish slot
    issues.extend(_article_issue(body))
    for match in _PLURAL_MODIFIER.finditer(body):
        issues.append(f"plural dish as modifier: '{match.group(1)}'")
    if dish:
        if dish.strip().lower() in CATEGORY_NOUNS:
            issues.append(f"category noun, not a plate: '{dish.strip()}'")
        elif dish.strip().lower().endswith("s") and _PLURAL_MODIFIER.search(body):
            issues.append(f"plural dish: '{dish.strip()}'")
    # (c) length and sentence count
    if len(body) > MAX_BODY_CHARS:
        issues.append(f"{len(body)} chars exceeds {MAX_BODY_CHARS}")
    if len(body.strip()) < MIN_BODY_CHARS:
        issues.append("body too short to be a message")
    sentences = [s for s in _SENTENCE_SPLIT.split(body.strip()) if s.strip()]
    if len(sentences) > MAX_SENTENCES:
        issues.append(f"{len(sentences)} sentences reads like a paragraph")
    # (d) the copy laws, re-checked on the render rather than the template
    if _EM_DASH in body:
        issues.append("em dash (owner law: never)")
    # Deliberately NOT an ellipsis or double-punctuation rule: the owner's
    # pinned rung copy opens `Salam!! no worries if this got buried...`, and a
    # wall that vetoes his own punctuation would silently retire the ladder.
    if body != body.strip():
        issues.append("leading/trailing whitespace")
    for char in _EMOJI.findall(body):
        if char not in ALLOWED_EMOJI:
            issues.append(f"unapproved emoji U+{ord(char):X}")
    # (e) owner ruling 2026-11-11: never react to the FACT of reviews
    if _REVIEW_EXISTENCE.search(body):
        issues.append(
            "review-existence opener (owner ban: answer what the guest said)"
        )
    return issues


# ---------------------------------------------------------------------------
# layer 2 - model second opinion (fail-open)
# ---------------------------------------------------------------------------


def _judge_prompt(body: str, dish: str | None) -> str:
    """One narrow question: does the DISH mention make sense?

    Live 2026-11-09: the model was asked whether the message read as "a natural
    message from a real person" and vetoed eight of ten dish first touches for
    being "overly formal", "insincere" or "self-promotional" - always because
    of the owner's own pinned skeleton (`Salam!` opener and the `P.S. I build
    restaurant systems` line). A model's taste must never retire the owner's
    sentence, so layer 2 now judges coherence of the fill and is told, in the
    prompt, not to judge tone. Veto power stays narrow; tone is not its call.
    """
    subject = f" The message mentions the dish '{dish}'." if dish else ""
    return (
        "You check one Instagram DM for ONE thing only: whether any dish or "
        "food mention in it is coherent - a real food named in a sentence "
        "that means something." + subject + "\n\n"
        f"Message:\n{body}\n\n"
        "Do not judge tone, politeness, formality, length, salesiness, or the "
        "sender's decision to mention their own work: that wording is the "
        "owner's and is not under review. A message that mentions NO dish at "
        "all is completely fine (the no-dish variants are legitimate) - answer "
        "coherent: true. Only a dish that is a verb, a word that is not a "
        "plate, or a phrase that makes no sense is incoherent.\n\n"
        'Answer with JSON only: {"coherent": true|false, "score": 0.0-1.0, '
        '"reason": "<short>"}'
    )


def default_judge(body: str, dish: str | None) -> tuple[bool, float, str]:
    """Ask the local model. Raises on a dead model; `review` fails open."""
    from outreach.ollama_adapter import generate

    # One attempt, no backoff: layer 2 is a second opinion, never a stall. A
    # dead model must fail open at once (the wall's whole point), not sit in
    # the adapter's 3-attempt retry budget on every render.
    payload = generate(
        _judge_prompt(body, dish), expect_json=True, retries=1, sleep_s=0
    )
    if not isinstance(payload, dict):
        raise ValueError("judge did not answer with an object")
    score = float(payload.get("score", 1.0))
    # Only an explicit `coherent: false` may veto. A missing key, a stale
    # `natural`-shaped answer or any other shape is a PASS: the wall must never
    # invent a veto out of a model that did not answer the question asked.
    coherent = payload.get("coherent", True)
    if isinstance(coherent, str):
        coherent = coherent.strip().lower() not in ("false", "no", "0")
    reason = str(payload.get("reason") or "")[:200]
    # LIVE 2026-11-11: 21 stale holds still carried "no dish mentioned (score
    # 0.00)" - the model vetoing a message for ABSENCE, which the prompt tells
    # it is legitimate and which the no-dish variants require. Absence is not
    # incoherence; only a bad dish can be.
    lowered = reason.casefold()
    if not coherent and any(marker in lowered for marker in _ABSENCE_MARKERS):
        coherent, reason = True, f"no dish mentioned (legitimate): {reason}"
    return bool(coherent), score, reason


def default_tone_judge(body: str, dish: str | None) -> tuple[bool, float, str]:
    """Ask the model how the copy READS to a stranger. Telemetry only.

    Owner ruling 2026-11-11: tone comes back as advisory telemetry, never a
    veto, and never on owner copy. Its boolean is deliberately ignored by
    `review`; only the score and the note are stored. Raises on a dead model
    exactly like `default_judge`, and the caller fails open.
    """
    from outreach.ollama_adapter import generate

    prompt = (
        "Rate how this Instagram DM reads to a restaurant owner, on tone only."
        ' Return JSON: {"ok": true, "score": 0.0-1.0, "note": "<short>"}.'
        " Score 1.0 = warm, specific, human; 0.0 = pushy, generic or robotic."
        " This rating is advisory and can never block the message."
        + chr(10) + chr(10) + "Message:" + chr(10) + body
    )
    payload = generate(prompt, expect_json=True, retries=1, sleep_s=0)
    if not isinstance(payload, dict):
        raise ValueError("tone judge did not answer with an object")
    score = float(payload.get("score", 1.0))
    return True, score, str(payload.get("note") or payload.get("reason") or "")[:200]

def review(
    body: str,
    *,
    dish: str | None = None,
    judge: Judge | None = None,
    tone_judge: Judge | None = None,
) -> Verdict:
    """Judge a final rendered body. Pure: never returns edited bytes."""
    issues = _layer1(body, dish)
    if issues:
        return Verdict(
            ok=False, reason="; ".join(issues), layers="1", issues=tuple(issues)
        )
    if judge is None:
        # owner copy: layer 1 only, and no tone call either (owner ruling:
        # a model's taste never touches the owner's own sentence)
        return Verdict(ok=True, reason="layer 1 clean", layers="1")
    try:
        natural, score, why = judge(body, dish)
    except Exception as exc:  # noqa: BLE001 — a dead judge never blocks a send
        return Verdict(
            ok=True,
            reason=f"layer 2 unavailable ({type(exc).__name__}); layer 1 clean",
            layers="1",
        )
    # Coherence only: the score is telemetry, never a veto input. LIVE
    # 2026-11-09: the model answered `coherent: true` with `score: 0.00` on
    # no-dish renders and the score floor vetoed them anyway, so 5 holds went
    # to the queue quoting "No food mention" as the reason it had just passed.
    if not natural and dish is None and not any(
        word in (why or "").casefold() for word in _WRITING_FAULTS
    ):
        # Owner law (2026-11-15): a no-dish render has no dish to be incoherent
        # about. Holds 464/557/186 were refused because the model read the
        # RESTAURANT NAME as if it were a dish ("Poké", "Aulde Dubliner",
        # "Shawarma Bay"). The no-dish variants are legitimate, so a note about
        # a dish that does not exist is telemetry. A note that the WRITING is
        # broken ("dish is a verb") still holds — that is a real refusal, and
        # `_WRITING_FAULTS` is the line between the two. This sits in `review`
        # so it binds every judge, injected ones included.
        return Verdict(
            ok=True,
            reason=f"model note is advisory on a no-dish render: {why or 'no dish'}",
            layers="1+2",
            score=score,
        )
    if not natural:
        return Verdict(
            ok=False,
            reason=f"model: {why or 'reads incoherent'} (score {score:.2f})",
            layers="1+2",
            score=score,
        )
    tone_score: float | None = None
    tone_note = ""
    if tone_judge is not None:
        try:
            _tone_ok, tone_score, tone_note = tone_judge(body, dish)
        except Exception as exc:  # noqa: BLE001 - telemetry never blocks a send
            tone_score, tone_note = None, f"tone judge unavailable ({type(exc).__name__})"
    return Verdict(
        ok=True,
        reason=f"model ok ({score:.2f})",
        layers="1+2",
        score=score,
        tone_score=tone_score,
        tone_note=tone_note,
    )


# ---------------------------------------------------------------------------
# the hold queue + veto-rate monitor
# ---------------------------------------------------------------------------


def _now_stamp(now: datetime | None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def has_open_hold(db: "Database", lead_id: int) -> bool:
    """A vetoed lead waits for a human; claim_next must not retry it blind."""
    row = db.conn.execute(
        "SELECT 1 FROM proofread_log WHERE lead_id=? AND vetoed=1"
        " AND cleared_at IS NULL LIMIT 1",
        (lead_id,),
    ).fetchone()
    return row is not None


def veto_rate(db: "Database", variant: str, since: str) -> tuple[int, int]:
    """(vetoes, renders) for one variant since a stamp.

    A veto whose hold the owner has already CLEARED is not evidence of a copy
    bug any more: it was reviewed and released. Counting it was live
    2026-11-09 - nine tone-based vetoes cleared after the judge was recalibrated
    to coherence-only, then the very next render re-counted them and
    self-paused the queue at "8/13 (62%)". Resolved rows stay in the log for
    the audit; they no longer vote on whether the copy is broken.
    """
    row = db.conn.execute(
        "SELECT COALESCE(SUM(vetoed), 0), COUNT(*) FROM proofread_log"
        " WHERE variant=? AND created_at >= ? AND cleared_at IS NULL",
        (variant, since),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def _alert_once(db: "Database", body: str, now: str) -> None:
    day = now[:10]
    existing = db.conn.execute(
        "SELECT 1 FROM digest WHERE body LIKE ? AND created_at >= ? LIMIT 1",
        (f"PROOFREAD {body.split(':')[0]}%", day + "T00:00:00"),
    ).fetchone()
    if existing is None:
        with db.conn:
            db.conn.execute(
                "INSERT INTO digest(body, created_at) VALUES (?, ?)",
                (f"PROOFREAD {body}", now),
            )


def screen(
    db: "Database",
    body: str,
    *,
    lead_id: int,
    variant: str | None = None,
    dish: str | None = None,
    now: datetime | None = None,
    judge: Judge | None = None,
    tone_judge: Judge | None = None,
) -> Verdict:
    """Judge one render, log it, hold + alert on a veto.

    Returns the verdict. Never edits `body`; never changes the lead's state.
    """
    verdict = review(body, dish=dish, judge=judge, tone_judge=tone_judge)
    stamp = _now_stamp(now)
    label = variant or "unknown"
    with db.conn:
        cur = db.conn.execute(
            "INSERT INTO proofread_log(lead_id, variant, dish, body, ok,"
            " layers, reason, vetoed, created_at, tone_score, tone_note)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                lead_id, label, dish, body, 1 if verdict.ok else 0,
                verdict.layers, verdict.reason, 0 if verdict.ok else 1, stamp,
                verdict.tone_score, verdict.tone_note or None,
            ),
        )
        new_id = int(cur.lastrowid or 0)
    if not verdict.ok:
        vetoes, renders = veto_rate(db, label, stamp[:10] + "T00:00:00")
        surfaced = db.conn.execute(
            "SELECT COUNT(*) FROM proofread_log WHERE variant=? AND vetoed=1"
            " AND created_at >= ?",
            (label, stamp[:10] + "T00:00:00"),
        ).fetchone()[0]
        # first three vetoes of the day: the owner sees the actual sentence
        if int(surfaced) <= 3:
            _alert_once(
                db,
                f"VETO [{label}] lead {lead_id} (row {new_id}): "
                f"{verdict.reason} | body: {body[:300]}",
                stamp,
            )
        else:
            _alert_once(
                db,
                f"VETO [{label}] lead {lead_id}: {verdict.reason}",
                stamp,
            )
        if renders >= 5:
            rate = vetoes / renders
            if rate > 0.50:
                with db.conn:
                    db.conn.execute(
                        "INSERT INTO config(key, value) VALUES ('queue_status', '\"PAUSED\"')"
                        " ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                    )
                _alert_once(
                    db,
                    f"AUTO-PAUSE [{label}] {vetoes}/{renders} vetoed"
                    f" ({rate:.0%}) - copy bug, queue PAUSED",
                    stamp,
                )
            elif rate > 0.20:
                _alert_once(
                    db,
                    f"ALERT [{label}] {vetoes}/{renders} vetoed ({rate:.0%})"
                    " - copy bug signal",
                    stamp,
                )
    return verdict


def clear_hold(db: "Database", lead_id: int, *, now: datetime | None = None) -> int:
    """Owner reviewed a vetoed lead: allow it to be claimed again."""
    with db.conn:
        cur = db.conn.execute(
            "UPDATE proofread_log SET cleared_at=? WHERE lead_id=?"
            " AND vetoed=1 AND cleared_at IS NULL",
            (_now_stamp(now), lead_id),
        )
    return int(cur.rowcount or 0)


def release_copy_holds(
    db: "Database", *, now: datetime | None = None, lead_id: int | None = None
) -> tuple[int, ...]:
    """Return COPY_HOLD leads to ROUTED once nothing is vetoing them.

    LIVE 2026-11-16: `clear_hold` cleared the veto and nothing moved the LEAD,
    so the free edge COPY_HOLD -> ROUTED in the machine had no caller and all
    18 held leads were unreachable by any path — a permanent park. Owner
    judgement decides when a veto stops mattering (that is `clear_hold`); this
    function only performs the mechanical return, and it refuses any lead that
    still has an open veto.
    """
    from outreach.machine import transition

    moment = now or datetime.now(timezone.utc)
    rows = db.conn.execute(
        "SELECT id FROM leads WHERE state='COPY_HOLD'"
        + (" AND id=?" if lead_id is not None else "")
        + " ORDER BY id",
        (lead_id,) if lead_id is not None else (),
    ).fetchall()
    released: list[int] = []
    for row in rows:
        target = int(row["id"])
        if has_open_hold(db, target):
            continue  # the wall still objects: releasing would re-hold it
        transition(
            db, target, "ROUTED",
            reason="copy hold released 2026-11-16: the veto is cleared, the lead"
                   " returns to the queue for a fresh render",
            actor="proofread",
            at=moment,
        )
        released.append(target)
    return tuple(released)


# --- R7 (2026-11-13): the hold inventory --------------------------------------

@dataclass(frozen=True)
class OpenHold:
    """One vetoed render still blocking its lead.

    A hold is not a verdict on the lead: the wall refused to let the body leave
    the building. Only the owner can clear one (`clear_hold`), because clearing
    is a judgement about copy, not a mechanical repair.
    """

    id: int
    lead_id: int
    lead_name: str
    lead_state: str
    variant: str
    dish: str | None
    reason: str
    layers: str
    created_at: str

    @property
    def kind(self) -> str:
        """`copy_lint` (deterministic layer 1) or `model_veto` (layer 2)."""
        return classify_veto(self.reason)


def classify_veto(reason: str) -> str:
    """Which layer decided — the owner's "copy bug or model noise?" question.

    Live 2026-11-13: all 9 open holds carried `layers='1+2'` with a `model:`
    reason about the DISH, while the layer-1-only vetoes read
    ``category noun, not a plate: '<word>'``. The layer that spoke answers the
    question, so nobody has to read nine bodies by hand to find out.
    """
    text = (reason or "").strip().lower()
    return "model_veto" if text.startswith("model:") else "copy_lint"


def open_holds(db: "Database") -> list[OpenHold]:
    """Every open hold, oldest first. Read-only; never clears anything."""
    rows = db.conn.execute(
        "SELECT p.id, p.lead_id, l.name AS lead_name, l.state AS lead_state,"
        " p.variant, p.dish, p.reason, p.layers, p.created_at"
        " FROM proofread_log p JOIN leads l ON l.id = p.lead_id"
        " WHERE p.vetoed = 1 AND p.cleared_at IS NULL"
        " ORDER BY p.id"
    ).fetchall()
    return [
        OpenHold(
            id=int(row["id"]),
            lead_id=int(row["lead_id"]),
            lead_name=str(row["lead_name"] or ""),
            lead_state=str(row["lead_state"] or ""),
            variant=str(row["variant"] or ""),
            dish=row["dish"],
            reason=str(row["reason"] or ""),
            layers=str(row["layers"] or ""),
            created_at=str(row["created_at"] or ""),
        )
        for row in rows
    ]


def format_holds(holds: list[OpenHold]) -> list[str]:
    """Owner-facing inventory: one block per hold, then the split by layer."""
    if not holds:
        return ["holds: none (no render is blocked by the wall)"]
    lines = [f"holds: {len(holds)} render(s) blocked by the wall"]
    for hold in holds:
        dish = hold.dish if hold.dish else "—"
        lines.append(
            f"  lead {hold.lead_id} [{hold.lead_state}] {hold.lead_name}"
            f" | {hold.variant} | dish: {dish} | {hold.kind}"
        )
        lines.append(f"    {hold.reason}")
        lines.append(f"    logged {hold.created_at} (layers {hold.layers})")
    lint = sum(1 for h in holds if h.kind == "copy_lint")
    lines.append(
        f"  split: {lint} copy-lint (layer 1) / {len(holds) - lint} model (layer 2)"
    )
    lines.append(
        "  clear one (owner judgement): python -m outreach.command_center"
        " holds --clear LEAD_ID"
    )
    return lines
