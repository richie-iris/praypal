"""rt_bridge.py — Conference Shield: dial policy, scam signatures, bridge ledger.

Iris can pull a third party onto a call ("call that number back with me") so an
older adult is never alone on the line with a stranger. This module owns the
deterministic half: WHO may be dialed, HOW OFTEN, and WHAT a scam sounds like.
The model never decides any of it — it only asks.

Policy, in one breath: US/Canada only, never emergency or premium numbers,
never more than a handful of bridges a day per caller, never a dial the caller
did not ask for out loud.
"""
from __future__ import annotations

import rt_obs

import contextlib
import json
import os
import re
from datetime import datetime, timezone

import rt_prefs

BRIDGE_CATEGORY = "bridge_log"
SCAM_CATEGORY = "scam_reports"

MAX_BRIDGES_PER_DAY = 8
# Per-call ceiling, enforced by the agent's bridge tool; the day cap lives in the ledger.
MAX_BRIDGES_PER_CALL = 4
MAX_BRIDGE_SECONDS = 20 * 60
MAX_ASSIST_SECONDS = 45 * 60

MODE_ASSIST = "assist"
MODE_SHIELD = "shield"


def _guard(guard: str, allowed: bool, reason: str, **fields) -> None:
    """Every allow/refuse decision in this module leaves one identically shaped line.

    Telemetry only. It cannot raise, and it never carries the caller-facing
    wording — a short reason code instead, so refusals are groupable.
    """
    with contextlib.suppress(Exception):
        rt_obs.obs.event("guard.decision", guard=guard, allowed=allowed,
                         reason=reason, **fields)


def _sip(action: str, status: str, **fields) -> None:
    """The outbound leg this module authorises, named by the trunk it leaves on."""
    with contextlib.suppress(Exception):
        rt_obs.obs.event("lk.sip", action=action, status=status,
                         trunk=(os.getenv("SIP_OUTBOUND_TRUNK_ID") or "unset"),
                         **fields)


def _signal(signature: str, **fields) -> None:
    """A signature fired. Deterministic regex, so the confidence is not a guess."""
    with contextlib.suppress(Exception):
        rt_obs.obs.event("scam.signal", signature=signature,
                         confidence="deterministic", **fields)


_SUSPICIOUS_HINTS = re.compile(
    r"\b(scam|scammer|fraud|suspicious|weird call|strange call|robocall|"
    r"claim(?:ed|s|ing)? to be|said (?:he|she|they) (?:was|were) from|"
    r"grandson|granddaughter|arrested|jail|bail|warrant|irs|social security|"
    r"gift ?card|wire|bitcoin|crypto|refund|prize|sweepstakes|lawsuit|"
    r"don'?t trust|not sure who|unknown number|didn'?t recognize)\b", re.I)


def classify_mode(caller_words: str, looked_up: bool = False) -> str:
    """Decide how she should behave on this bridge, from the caller's own words.

    DEFAULT IS ASSIST — she's a phone pal, not a bodyguard. Being on a call with
    her person is ordinary: the doctor, the pharmacy, a friend, a shop. She only
    drops into silent-witness SHIELD when the caller's own framing says something
    is off, and the fraud watch escalates her there mid-call if it needs to.
    """
    text = caller_words or ""
    if _SUSPICIOUS_HINTS.search(text):
        _guard("bridge_mode", False, MODE_SHIELD, mode=MODE_SHIELD,
               looked_up=looked_up, chars=len(text))
        return MODE_SHIELD
    _guard("bridge_mode", True, MODE_ASSIST, mode=MODE_ASSIST,
           looked_up=looked_up, chars=len(text))
    return MODE_ASSIST

_EMERGENCY = {"911", "112", "999"}
_CRISIS_LINE = {"988", "9-8-8"}
_N11 = re.compile(r"^\+1(\d{3})?(211|311|411|511|611|711|811|911)$")
_PREMIUM_NPA = {"900", "976"}
_TOLLFREE_NPA = {"800", "833", "844", "855", "866", "877", "888"}
_NON_US_CA_NPA = {
    "242", "246", "264", "268", "284", "340", "345", "441", "473", "649", "658",
    "664", "670", "671", "684", "721", "758", "767", "784", "787", "809", "829",
    "849", "868", "869", "876", "939",
}


class DialRefused(Exception):
    """Raised with a caller-facing reason when a number may not be dialed."""


def normalize_dialable(raw: str | None) -> str:
    """Return an E.164 number Iris is permitted to dial, or raise DialRefused.

    The reason text is written to be spoken to an older adult, not logged.
    """
    s = (raw or "").strip()
    if not s:
        _guard("dial_policy", False, "no_number")
        raise DialRefused("I didn't catch a number to call.")

    digits = re.sub(r"[^\d+]", "", s)
    bare = digits.lstrip("+")
    if bare in _EMERGENCY or digits in _EMERGENCY:
        _guard("dial_policy", False, "emergency")
        raise DialRefused(
            "I can't dial emergency services for you — they need to hear your own "
            "voice and know where you are. Please hang up and dial 911 yourself, "
            "right now. I'll be here after.")
    if bare in _CRISIS_LINE or digits in _CRISIS_LINE:
        _guard("dial_policy", False, "crisis_line")
        raise DialRefused(
            "I can't connect you to the crisis line myself — they need to hear "
            "your own voice. Please hang up and dial 9 8 8 right now; someone "
            "kind will answer, any hour. Stay on with them, not with me. "
            "I'll be right here whenever you want to talk after.")

    e164 = rt_prefs.normalize_e164(s)
    if not e164:
        _guard("dial_policy", False, "unparseable", chars=len(s))
        raise DialRefused("That doesn't sound like a complete phone number — "
                          "could you read it to me again, digit by digit?")

    if not e164.startswith("+1"):
        _guard("dial_policy", False, "outside_nanp")
        raise DialRefused("I can only place calls inside the US and Canada.")
    if len(e164) != 12:
        _guard("dial_policy", False, "bad_length", digits=len(e164))
        raise DialRefused("That doesn't sound like a complete US or Canadian number.")
    if _N11.match(e164):
        _guard("dial_policy", False, "n11_service")
        raise DialRefused("That's a service line I'm not able to dial for you.")

    npa = e164[2:5]
    if npa in _NON_US_CA_NPA:
        _guard("dial_policy", False, "caribbean_npa", npa=npa)
        raise DialRefused("That number is outside the US and Canada even though it "
                          "starts with a one — those calls cost a fortune, and scammers "
                          "use them on purpose. I won't dial it.")
    if npa in _PREMIUM_NPA:
        _guard("dial_policy", False, "premium_rate", npa=npa)
        raise DialRefused("That's a premium-rate number, so I won't call it — "
                          "those charge by the minute, and scammers use them.")
    if npa.startswith("0") or npa.startswith("1"):
        _guard("dial_policy", False, "invalid_npa", npa=npa)
        raise DialRefused("That area code isn't a real one — could you read the "
                          "number to me again?")
    _guard("dial_policy", True, "ok", phone=e164, npa=npa,
           tollfree=(npa in _TOLLFREE_NPA))
    return e164


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_log(schemas: list[dict] | None) -> dict:
    """The caller's bridge ledger, or {} when they have none yet.

    A row that exists but will not parse RAISES rather than reading as empty: an
    empty ledger means "no calls today", and that is exactly what a corrupt one
    must never be mistaken for (fail closed, docs/adr/0003).
    """
    for s in (schemas or []):
        if (s.get("category") or "").lower() == BRIDGE_CATEGORY:
            raw = s.get("data_summary")
            # Only an ABSENT summary (null column, or a blank string) is an empty
            # ledger. `or "{}"` used to swallow 0 / [] / false the same way.
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                raw = "{}"
            try:
                obj = json.loads(raw) if isinstance(raw, str) else raw
            except Exception as _exc:
                rt_obs.obs.caught("rt_bridge._load_log", _exc)
                raise ValueError("bridge ledger row is not valid JSON") from _exc
            if not isinstance(obj, dict):
                raise ValueError(f"bridge ledger row is a {type(obj).__name__}, not an object")
            return obj
    return {}


def check_and_record_dial(h: str, e164: str) -> None:
    """Enforce the per-caller daily bridge cap and record this dial.

    Raises DialRefused when the cap is spent. The ledger doubles as the audit
    trail for every number Iris has ever dialed on this caller's behalf.
    """
    # Read-modify-write with no lock. Two simultaneous sessions for ONE caller
    # could each read count=N and both write N+1, under-counting by one; that is
    # an accepted residual — a caller has one phone, and the per-call ceiling
    # (MAX_BRIDGES_PER_CALL) bounds how far any single session can overrun.
    _unavailable = DialRefused(
        "I can't check our call log right now, so I'd rather not place that call "
        "just yet. Let's try again in a few minutes.")
    # The carrier's daily cap, decided FIRST and from Twilio's own number for
    # today: Telnyx used to refuse the INVITE itself at $25/day, Twilio cannot,
    # so the refusal happens here, before this dial is even written down.
    import rt_carrier
    allowed, why, detail = rt_carrier.outbound_allowed()
    if not allowed:
        _guard("carrier_cap", False, why, **detail)
        if why == "daily_cap_spent":
            raise DialRefused(
                "We've used up today's calling budget, so I'd rather not place that "
                "call. If something urgent needs a call, let's get a family member on it.")
        raise DialRefused(
            "I can't confirm our phone budget right now, so I'd rather not place that "
            "call just yet. Let's try again in a few minutes.")
    if rt_prefs._db() is None:
        # Decided BEFORE any RPC: with no database there is no ledger, and _req
        # would answer None for both the read and the write.
        _guard("bridge_cap", False, "ledger_unavailable", err="db_unavailable")
        raise _unavailable
    try:
        bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h})
        if not isinstance(bundle, dict):
            # None is _req's "db unconfigured / ref refused"; anything else is not a
            # bundle. Neither says "no calls today", so neither may dial.
            raise TypeError(f"bundle is {type(bundle).__name__}, not a dict")
        log = _load_log(bundle.get("schemas"))
    except Exception as _exc:
        # FAIL CLOSED. An unreadable ledger looks exactly like an empty one, and an
        # empty one would let a spent daily cap be dialed past — so no read, no dial.
        rt_obs.obs.caught("rt_bridge.dial_ledger_read", _exc)
        _guard("bridge_cap", False, "ledger_unavailable", err=type(_exc).__name__)
        raise _unavailable from _exc

    day = _today()
    try:
        used, numbers = _day_entry(log, day)
    except ValueError as _exc:
        # A day entry that is not an object, or a count that is not a whole number,
        # is a corrupt ledger — and a corrupt ledger reads as NOTHING, not as zero.
        rt_obs.obs.caught("rt_bridge.dial_ledger_corrupt", _exc)
        _guard("bridge_cap", False, "ledger_corrupt", err=type(_exc).__name__)
        raise DialRefused(
            "Our call log doesn't look right to me, so I'd rather not place that call "
            "just yet. Let's have someone in the family take a look.") from _exc
    if used >= MAX_BRIDGES_PER_DAY:
        _guard("bridge_cap", False, "daily_cap_spent", used=used, cap=MAX_BRIDGES_PER_DAY)
        raise DialRefused(
            "We've already made several calls together today, so I'd rather stop "
            "here. If something urgent needs a call, let's get a family member on it.")

    _guard("bridge_cap", True, "under_cap", used=used, cap=MAX_BRIDGES_PER_DAY, phone=e164)
    today = {"count": used + 1, "numbers": ([*numbers, e164])[-MAX_BRIDGES_PER_DAY:]}
    keep = {k: v for k, v in log.items() if isinstance(k, str) and k >= day}
    keep[day] = today
    # The WRITE happens before the dial is authorised, and its failure is fatal: a
    # dial that never reached the ledger is a dial the daily cap cannot see. The
    # RPC RETURNS VOID, so a committed write comes back as None / an empty body;
    # transport and HTTP failures surface as exceptions from rt_http, never as None.
    try:
        rt_prefs._req("POST", "rpc/rt_add_schema_entry", {
            "p_hash": h, "p_table": f"caller_{h[:8]}_{BRIDGE_CATEGORY}",
            "p_cat": BRIDGE_CATEGORY, "p_summary": json.dumps(keep)})
    except Exception as _exc:
        rt_obs.obs.caught("rt_bridge.dial_ledger_write", _exc)
        _guard("bridge_cap", False, "ledger_write_failed", err=type(_exc).__name__)
        raise DialRefused(
            "I couldn't write that call down in our log, so I'd rather not place it "
            "just yet. Let's try again in a few minutes.") from _exc
    _sip("invite", "authorized", phone=e164, bridges_today=today.get("count"))


def _day_entry(log: dict, day: str) -> tuple[int, list]:
    """(bridges used today, numbers dialed today) from a parsed ledger.

    Absent day -> (0, []). Present but malformed -> ValueError: a string, list,
    null or number where the day object should be, a count that is not a
    non-negative int (bool is not a count), or a numbers field that is not a list.
    """
    if day not in log:
        return 0, []
    entry = log[day]
    if not isinstance(entry, dict):
        raise ValueError(f"day entry is a {type(entry).__name__}, not an object")
    count = entry.get("count", 0)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        raise ValueError(f"count is {count!r}, not a non-negative int")
    numbers = entry.get("numbers")
    if numbers is None:
        numbers = []
    if not isinstance(numbers, list):
        raise ValueError(f"numbers is a {type(numbers).__name__}, not a list")
    return count, numbers


_SIGNATURES: list[tuple[str, str, re.Pattern]] = [
    ("payment_gift_cards", "asked for gift cards as payment",
     re.compile(r"\b(gift|itunes|google play|apple|steam|target|walmart)\s*(card|cards)\b", re.I)),
    ("payment_wire_crypto", "asked for a wire transfer, crypto, or a Bitcoin ATM",
     re.compile(r"\b(wire transfer|western union|moneygram|bitcoin|crypto(currency)?|"
                r"btc atm|coin ?machine|zelle|cash app)\b", re.I)),
    ("secrecy", "told them to keep it secret from their family",
     re.compile(r"\b(don'?t tell|do not tell|keep (this|it) (a )?(secret|between us)|"
                r"no one can know|don'?t (talk|speak) to (your|anyone))\b", re.I)),
    ("authority_claim", "claimed to be a government or law-enforcement official",
     re.compile(r"\b(i'?m (from|with|calling from) (the )?(irs|social security|ssa|medicare|"
                r"police|sheriff|fbi|dea|immigration)|warrant for your arrest|"
                r"your social security number has been (suspended|compromised))\b", re.I)),
    ("grandchild_trouble", "used the grandchild-in-trouble script",
     re.compile(r"\b(grand(son|daughter|child)|nephew|niece)\b[^.!?]{0,80}"
                r"\b(jail|arrested|accident|hospital|bail|trouble|detained)\b", re.I)),
    ("remote_access", "asked for remote access to a computer",
     re.compile(r"\b(anydesk|teamviewer|remote (access|desktop)|let me (in|connect) to your "
                r"(computer|pc)|install (this|the) (app|software))\b", re.I)),
    ("credential_request", "asked for a code, PIN, password, or account number",
     re.compile(r"\b(read me the (code|number)|verification code|one[- ]time (code|password)|"
                r"your (pin|password|social security number|account number|routing number))\b", re.I)),
    ("urgency_threat", "used pressure or threats to force a decision now",
     re.compile(r"\b(right now or|within the (next )?(hour|30 minutes)|you will be arrested|"
                r"account will be (closed|frozen|suspended)|last (chance|warning)|"
                r"stay on the (line|phone) (with me )?until)\b", re.I)),
]


def scan_signatures(text: str) -> list[tuple[str, str]]:
    """Return (key, plain-English description) for every signature present."""
    hits = [(k, d) for k, d, rx in _SIGNATURES if rx.search(text or "")]
    with contextlib.suppress(Exception):
        for _k, _d in hits:
            _signal(_k, signals=len(hits), chars=len(text or ""))
        if hits:
            urgency = sum(1 for k, _ in hits if "urgency" in k or "pressure" in k or "deadline" in k or "threat" in k)
            monetary = sum(1 for k, _ in hits if "payment" in k or "wire" in k or "gift" in k or "crypto" in k or "card" in k)
            impersonation = sum(1 for k, _ in hits if "impersonat" in k or "authority" in k or "agency" in k or "irs" in k or "support" in k)
            rt_obs.obs.event("scam.risk_breakdown", urgency=urgency, monetary=monetary,
                             impersonation=impersonation, score=len(hits))
    return hits


def scan_transcript(transcript: str, speaker_prefix: str = "line:") -> list[tuple[str, str]]:
    """Scan ONLY the bridged third party's speech, never the caller's own words.

    A senior repeating 'he wants gift cards' back to Iris must not itself count
    as a scam signature — the evidence has to come from the stranger's mouth.
    """
    lines = [l for l in (transcript or "").splitlines()
             if l.strip().lower().startswith(speaker_prefix)]
    hits: dict[str, str] = {}
    for l in lines:
        for k, d in scan_signatures(l):
            hits.setdefault(k, d)
    return sorted(hits.items())


_BAIT_QUERY = re.compile(
    r"\b(refund|gift ?card|bitcoin|crypto|antivirus|virus|malware|"
    r"tech support|computer support|microsoft support|apple support|"
    r"unlock|reactivate|claim (?:your )?(?:prize|winnings)|"
    r"sweepstakes|lottery|debt relief|loan forgiveness)\b", re.I)

_LOOKUP_PROMPT = """Find the OFFICIAL published phone number for this, for an older adult to call.

WHAT THEY ASKED FOR: {ask}

Rules:
- Give the number published by the organization ITSELF (its own website, or the number on
  a government/health-plan directory). Never a third-party "support" or aggregator listing.
- If several plausible numbers exist, give the main public line.
- If you cannot find a number you are confident is official, set number to null. Say so —
  a wrong number is far worse than no number.
- source must name where it came from in a few plain words, e.g. "the pharmacy's own website".

Return ONLY valid JSON:
{{"name": "the organization as it calls itself", "number": "+1XXXXXXXXXX or null",
  "source": "where this came from", "confidence": "high | medium | low",
  "note": "one short sentence if there's something the caller should know, else empty"}}"""


def lookup_number(ask: str, gemini_json, api_key: str) -> dict:
    """Look up an official phone number. Returns {} when nothing trustworthy was found.

    gemini_json is injected so tests can run this without the network.
    """
    ask = (ask or "").strip()
    if not ask:
        return {}
    if _BAIT_QUERY.search(ask):
        print(f"[rt-lookup] REFUSE bait-shaped lookup: {ask[:60]!r}", flush=True)
        _guard("lookup_bait", False, "bait_shaped_query", chars=len(ask))
        return {"refused": True, "reason": (
            "Searching the internet for that kind of number is exactly how people get "
            "connected to scammers — the fake numbers out-rank the real ones. Let's use "
            "a number from their own paperwork, a statement, or the back of the card instead.")}
    try:
        res = gemini_json(api_key, _LOOKUP_PROMPT.format(ask=ask), temperature=0.1, retries=2) or {}
    except Exception as e:
        print(f"[rt-lookup] lookup failed: {e}", flush=True)
        return {}
    raw = res.get("number")
    if not raw or str(raw).lower() in ("null", "none", ""):
        return {"name": res.get("name") or ask, "number": None,
                "confidence": "none", "source": res.get("source") or ""}
    try:
        e164 = normalize_dialable(str(raw))
    except DialRefused as e:
        print(f"[rt-lookup] found unusable number {raw!r}: {e}", flush=True)
        return {"name": res.get("name") or ask, "number": None, "confidence": "none",
                "source": res.get("source") or ""}
    out = {"name": str(res.get("name") or ask)[:80], "number": e164,
           "source": str(res.get("source") or "")[:80],
           "confidence": str(res.get("confidence") or "medium").lower(),
           "note": str(res.get("note") or "")[:160]}
    print(f"[rt-lookup] {out['name']} → {e164} ({out['confidence']}, {out['source']})", flush=True)
    return out


def save_scam_report(h: str, number: str, signatures: list[tuple[str, str]],
                     summary: str, outcome: str = "") -> None:
    """Persist what happened so the next call can check in and family can be told."""
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        key = "r_" + stamp.replace(":", "").replace("-", "")
        entry = {"at": stamp, "number": number,
                 "signals": [d for _, d in signatures],
                 "summary": (summary or "").strip()[:400], "outcome": outcome}
        rt_prefs._req("POST", "rpc/rt_add_schema_entry", {
            "p_hash": h, "p_table": f"caller_{h[:8]}_{SCAM_CATEGORY}",
            "p_cat": SCAM_CATEGORY, "p_summary": json.dumps({key: entry})})
        print(f"[rt-bridge] scam report saved: {len(signatures)} signal(s) on {number}", flush=True)
    except Exception as e:
        print(f"[rt-bridge] scam report write failed (non-fatal): {e}", flush=True)
