"""Manual-send scripts (v1.2 pack, owner paste 2026-11-05): pinned owner bytes.

These are the messages the OWNER sends by hand once a conversation is live:
the REVEAL (message 3), the R.5 fast-track for a hot lead, and the two
objection answers. Nothing here is automated or model-written, and nothing
here is a first touch - the first touch stays the frozen opener.

Law, identical to the DM copy:
- the body is the owner's text, byte-for-byte, in `SCRIPTS`. `SCRIPT_SHA256`
  pins each one, so an accidental edit inside this file is a test failure
  rather than a quiet copy change;
- the ONLY fill in the whole pack is `{{restaurant}}` (the REVEAL), and it
  comes from the lead row. A script that declares no fill refuses one;
- no em dash anywhere (standing law 2026-11-03). The v1.2 lines arrived
  em-dash free, so Ruling A needed no rewrite here - the lint in
  `variants.lint` is what stops the character coming back;
- `REVEAL` is checked against the channel's real message cap
  (`IG_DM_MAX_CHARS`). The "first DM <= 220 characters" note is about the
  OPENER; no such lint exists in this repository, and the opener is not this
  module's copy.
"""
from __future__ import annotations

import hashlib
import re

#: Instagram's DM length ceiling. A first DM stays short by law; the REVEAL
#: is message 3 and only has to fit the transport.
IG_DM_MAX_CHARS = 1000

SCRIPTS: dict[str, str] = {
    # message 3: the system entrance, after the lead showed interest
    "reveal": (
        "Actually since you mentioned it, I built what {restaurant}'s own"
        " system could look like: your ordering app, and behind it the"
        " counter, the kitchen, your dashboard, all seeing the same live"
        " numbers. [attach mockup]\n\n"
        "Quick question: how much of your orders these days go through the"
        " delivery apps versus people walking in?"
    ),
    # R.5: hot lead, the full system fast-track
    "r5": (
        "Happy to explain properly. I don't just build an ordering app, I"
        " build the restaurant's own system. Your app for customers, zero"
        " commission. The counter runs it on an iPad, the kitchen sees orders"
        " live, you see every transaction from your phone, and if you ever"
        " open a second branch, you watch both from one dashboard. Base build"
        " is $5,000, $25-30 a week to run it. Whole offer's on one page:"
        " labs.carnama.online. I'll run your real numbers with you anytime,"
        " 20 minutes."
    ),
    # objection: the POS/Square objection, answered on ownership
    "objection_pos": (
        "Fair, and I'm not trying to replace what works. The difference is"
        " ownership. Square's dashboard is Square's. Your data sits in their"
        " cloud, their fees stack, and if you leave, you start over. With"
        " this, the system is yours. Your customers, your numbers, your"
        " history, in one place. And it's built around YOUR menu and how YOU"
        " run service, not the other way around."
    ),
    # objection: scope honesty, what exists today versus what is being built
    "objection_scope": (
        "The customer app is built and running, that's real today. The"
        " counter and kitchen screens are the phase I'm building now, and I"
        " show every step in public on my accounts. That's the deal: you"
        " always know exactly what exists and what's coming next. No mystery"
        " boxes."
    ),
}

SCRIPT_SHA256: dict[str, str] = {
    name: hashlib.sha256(body.encode("utf-8")).hexdigest()
    for name, body in SCRIPTS.items()
}

_FILL_RE = re.compile(r"\{([a-z_]+)\}")


def declared_fills(name: str) -> frozenset[str]:
    """The fill names a script's own bytes declare (never a caller's list)."""
    body = _body(name)
    return frozenset(_FILL_RE.findall(body))


def _body(name: str) -> str:
    body = SCRIPTS.get(name)
    if body is None:
        raise ValueError(
            f"unknown script {name!r}; the pack is {sorted(SCRIPTS)}"
        )
    return body


def render_script(name: str, **fills: str) -> str:
    """Fill an owner script, refusing anything beyond its declared fills.

    Strict equality in both directions: a missing fill leaves `{...}` in the
    copy (unshippable), and an extra fill means the caller thinks this script
    says something it does not. Both are refused.
    """
    body = _body(name)
    wanted = declared_fills(name)
    given = frozenset(fills)
    if given != wanted:
        raise ValueError(
            f"script {name!r} declares fills {sorted(wanted)}; got"
            f" {sorted(given)}"
        )
    text = body
    for token, value in fills.items():
        if not str(value).strip():
            raise ValueError(f"script {name!r}: empty fill for {token!r}")
        text = text.replace("{" + token + "}", str(value).strip())
    if _FILL_RE.search(text):
        raise ValueError(f"script {name!r}: unresolved fill remains")
    return text


def assert_script_fits(name: str, text: str) -> None:
    """Transport check for a manual paste: the DM cap applies to every line."""
    if len(text) > IG_DM_MAX_CHARS:
        raise ValueError(
            f"script {name!r} is {len(text)} chars, past the"
            f" {IG_DM_MAX_CHARS}-char DM cap"
        )
