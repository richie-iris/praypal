"""rt_prefs.py — per-account caller preference & profile helper for the realtime lane.

Handles phone number normalization, SHA-256 phone hashing, and Supabase RPC calls
for caller lookup (rt_get_caller) and post-call counter increments (rt_bump_call).
"""
from __future__ import annotations

import rt_obs

import contextlib
import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dotenv import load_dotenv

import rt_http

load_dotenv(".env.local")

_BLOCKED = {"anonymous", "private", "unknown", "restricted", "unavailable", "blocked", "withheld"}


def normalize_e164(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    if not s or s.lower() in _BLOCKED:
        return None
    had_plus = s.startswith("+")
    digits = re.sub(r"[^\d]", "", s)
    if not had_plus and digits.startswith("00"):
        digits = digits[2:]
    if not had_plus:
        if len(digits) == 10:
            digits = "1" + digits
    if not (8 <= len(digits) <= 15):
        return None
    if digits.startswith("0"):
        return None
    return "+" + digits


# One-time flags: each warning prints once per process, not once per call.
_WARNED_UNPEPPERED = False
_WARNED_WEAK_PEPPER = False

# Only these spellings switch the pepper requirement OFF. Anything else — "required",
# a typo, "maybe" — means ON: a deploy lane that fat-fingers the flag must not
# silently hash unpeppered (fail closed, docs/adr/0003).
_PEPPER_NOT_REQUIRED = ("", "0", "false", "no", "off")
_PEPPER_PLACEHOLDERS = ("replace", "example", "changeme", "todo", "xxxx")
_PEPPER_MIN_CHARS = 16


def _pepper_ok_inline(value: str | None) -> bool:
    """config.pepper_ok's rule, kept here so a broken config import cannot loosen it."""
    v = (value or "").strip()
    if not v or v.startswith("#") or len(v) < _PEPPER_MIN_CHARS:
        return False
    low = v.lower()
    return not any(p in low for p in _PEPPER_PLACEHOLDERS)


def pepper_ok(value: str | None) -> bool:
    """Is this string a pepper worth keying with? config owns the rule; the inline
    copy is the floor, so the answer is never looser than either."""
    try:
        import config
        fn = getattr(config, "pepper_ok", None)
        if fn is not None:
            return bool(fn(value)) and _pepper_ok_inline(value)
    except Exception as _exc:
        rt_obs.obs.caught("rt_prefs.pepper_ok", _exc)
    return _pepper_ok_inline(value)


def pepper_required() -> bool:
    """Is the HMAC pepper mandatory on this lane? Required unless RT_REQUIRE_PEPPER
    is one of the explicit off-spellings; also required whenever config says so,
    so this module can never be looser than the boot check."""
    raw = (os.getenv("RT_REQUIRE_PEPPER") or "").strip().lower()
    required = raw not in _PEPPER_NOT_REQUIRED
    with contextlib.suppress(Exception):
        import config
        required = required or bool(config.pepper_required())
    return required


def phone_hash(e164: str, pepper: str | None = None) -> str | None:
    """Calculate deterministic SHA256 / HMAC-SHA256 caller ID hash.

    If RT_PHONE_HASH_PEPPER is set (or pepper provided), uses keyed HMAC-SHA256
    to protect against GPU rainbow-table / precomputed dictionary reversals
    of phone numbers. The argument wins outright when given, so pepper="" means
    "no pepper" even if the env has one.

    RT_REQUIRE_PEPPER (anything but 0/false/no/off; every deploy lane) makes a
    missing pepper fatal: an unpeppered SHA-256 of a phone number is reversible
    in minutes, so a lane that lost its pepper must refuse to hash rather than
    quietly downgrade. A pepper that is present but unusable — a template
    placeholder, a commented line, fewer than 16 chars — counts as MISSING for
    that decision: "changeme" keys nothing.
    """
    global _WARNED_UNPEPPERED, _WARNED_WEAK_PEPPER
    pep = (pepper if pepper is not None else os.getenv("RT_PHONE_HASH_PEPPER", "")).strip()
    usable = pepper_ok(pep)
    if not usable and pepper_required():
        why = "not set" if not pep else "set to a placeholder or too short to key with"
        raise RuntimeError(
            f"RT_PHONE_HASH_PEPPER is {why} but RT_REQUIRE_PEPPER is on — refusing to "
            "hash phone numbers with it. Set a real RT_PHONE_HASH_PEPPER for this lane.")
    norm = normalize_e164(e164)
    if not norm:
        return None
    if pep:
        if not usable and not _WARNED_WEAK_PEPPER:
            _WARNED_WEAK_PEPPER = True
            # Not required, so the lane is dev: warn, but still key with what it has —
            # a weak key beats none, and switching to plain SHA would orphan every
            # row this dev box already hashed.
            print("[rt-prefs] WARNING: RT_PHONE_HASH_PEPPER is unusable (placeholder or "
                  "under 16 chars) — a deploy lane would refuse it; set a real pepper",
                  flush=True)
        return hmac.new(pep.encode("utf-8"), norm.encode("utf-8"), hashlib.sha256).hexdigest()
    if not _WARNED_UNPEPPERED:
        _WARNED_UNPEPPERED = True
        # Plain print, not an obs event: the word must show up exactly once in the log.
        print("[rt-prefs] WARNING: RT_PHONE_HASH_PEPPER unset — phone hashes are "
              "unpeppered SHA-256 (dev only; every deploy lane sets RT_REQUIRE_PEPPER=1)",
              flush=True)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


_SPELLED_RUN = re.compile(r"\b(?:[a-zA-Z][ \-.]+){2,}[a-zA-Z]\b")


def despell(text: str) -> str:
    """Collapse letter-by-letter runs: 'r i c h i e' / 'R-I-C-H-I-E' → 'richie'."""
    return _SPELLED_RUN.sub(lambda m: re.sub(r"[ \-.]+", "", m.group(0)), text or "")


def join_spelled(raw: str) -> str:
    """'R I C H I E' → 'Richie'; anything that isn't purely spelled letters passes through."""
    tokens = re.split(r"[ \-.]+", (raw or "").strip())
    if len(tokens) >= 2 and all(len(t) == 1 and t.isalpha() for t in tokens):
        return "".join(tokens).capitalize()
    return raw


CRED_CATEGORY = "credentials"

# Schema categories the platform writes for itself. The model may never read or
# write one by name: the bridge ledger and minute budget are enforcement state
# (a model that can edit them can reset its own caps), scam reports are evidence,
# and the rest are bookkeeping. String literals, not imports — rt_bridge and
# agent import THIS module, so the shared list has to live below them.
INTERNAL_CATS: frozenset[str] = frozenset({
    "bridge_log", "scam_reports", "daily_minutes",
    "clarifications", "onboarding", "call_log", "agent_tasks", CRED_CATEGORY,
    "wellbeing",
})

_CRED_CONTEXT = re.compile(
    r"\b(?:pin|password|passcode|passwd|passkey|keycode|credentials?|"
    r"combination|combo|keypad|"
    r"(?<!zip[ -])(?<!area[ -])(?<!postal[ -])(?<!dress[ -])code)s?\b", re.IGNORECASE)
_DIGIT_RUN = re.compile(r"\d(?:[ \-]?\d){3,}")


_SSN = re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b")


from typing import Any

def scrub_ssn(obj: Any) -> Any:
    """Recursively remove SSN-shaped numbers from any structure before storage.

    An SSN is never stored — not as a fact, not vaulted, not in a reminder.
    """
    if isinstance(obj, str):
        if _SSN.search(obj):
            with contextlib.suppress(Exception):
                rt_obs.obs.warn("memory.refused", kind="ssn", value="[removed]",
                                reason="ssn_never_stored")
                rt_obs.obs.event("pii.redaction", category="ssn", count=1)
            return _SSN.sub("[SSN REDACTED] [removed]", obj)
        return obj
    if isinstance(obj, dict):
        return {k: scrub_ssn(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub_ssn(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(scrub_ssn(v) for v in obj)
    if isinstance(obj, set):
        return {scrub_ssn(v) for v in obj}
    return obj


def looks_credential(text: str) -> bool:
    """True when text pairs code/credential phrasing with a run of 4+ digits."""
    t = (text or "").replace("_", " ")
    return bool(_CRED_CONTEXT.search(t) and _DIGIT_RUN.search(t))


def redact_codes(text: str) -> str:
    """Blank the digit runs out of credential-flavored text; otherwise unchanged."""
    if not looks_credential(text):
        return text
    with contextlib.suppress(Exception):
        rt_obs.obs.warn("memory.refused", kind=CRED_CATEGORY, value="####",
                        reason="credential_pattern")
        rt_obs.obs.event("pii.redaction", category="credential", count=1)
    return _DIGIT_RUN.sub("####", text)


# ONE ref by default: dev, the project the 606 lane actually writes to. This
# listed two for a while, which meant an unset RT_ALLOWED_SUPABASE_REF let a
# worker reach either database — the opposite of what a lane guard is for. Every
# other lane sets this explicitly to its own ref; more than one entry here turns
# the guard off.
_ALLOWED_REFS_RAW = os.getenv(
    "RT_ALLOWED_SUPABASE_REF",
    "ojoppcyvkxwfuwzjjxbw"
)
ALLOWED_REFS: set[str] = {r.strip() for r in _ALLOWED_REFS_RAW.split(",") if r.strip()}


def _db() -> tuple[str, str] | None:
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        return None
    # HOSTNAME, not substring. This was `any(ref in url for ref in ALLOWED_REFS)`
    # — a scan of the whole URL string — so any URL that merely CONTAINED an
    # allowed project ref passed the guard. https://attacker.example.com/ojoppc…
    # would be accepted and _req would then send SUPABASE_SERVICE_ROLE_KEY to
    # that host in both the apikey and Authorization headers. The guard exists
    # to stop exactly that, so it has to parse the host and match the ref there.
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception as _exc:
        rt_obs.obs.caught("rt_prefs.host_parse", _exc)
        host = ""
    if not host or not any(host == f"{ref}.supabase.co" for ref in ALLOWED_REFS):
        print(f"[rt-prefs] REFUSING DB: host {host!r} is not an allowed "
              f"<ref>.supabase.co for {sorted(ALLOWED_REFS)}", flush=True)
        return None
    return url, key


def _obs_host(url: str | None) -> str | None:
    """Host only — never the key, never the query string."""
    return urllib.parse.urlsplit(url or "").hostname or None


def _obs_path(path: str) -> str:
    """Path without its query string: a phone_hash must not ride into the log."""
    return str(path or "").split("?", 1)[0]


def _obs_count(v) -> int:
    if v is None:
        return 0
    if isinstance(v, (list, tuple, set, dict, str)):
        return len(v)
    return 1


_BUNDLE_RPC = "rpc/rt_get_caller_full_bundle"


def _rpc_name(path: str) -> str:
    """'rpc/rt_bump_call?x=y' -> 'rt_bump_call'; a table path keeps its bare name."""
    return str(path or "").split("?", 1)[0].removeprefix("rpc/")


# The ONLY RPCs a transient failure may re-send. An allow-list, not a deny-list:
# a new RPC is sent once until someone proves it is a pure read and adds it here.
# rt_get_pending_jobs is deliberately absent despite its name — it UPDATEs rows to
# 'running' (a claim), so a re-send after a lost reply strands the first batch.
_READ_RPC_PREFIXES = ("rt_get_", "rt_count_", "rt_console_")
_READ_RPCS = frozenset({"rt_call_transcript", "rt_postcall_queue_stats"})
_CLAIMING_RPCS = frozenset({"rt_get_pending_jobs"})


def _is_read(method: str, path: str) -> bool:
    """May a second send of this call be sent without changing the database twice?"""
    if not path.startswith("rpc/"):
        return method.upper() in ("GET", "HEAD")
    name = _rpc_name(path)
    if name in _CLAIMING_RPCS:
        return False
    return name in _READ_RPCS or name.startswith(_READ_RPC_PREFIXES)


def _req(method: str, path: str, body: dict | None = None, _skip_audit: bool = False,
         deadline_s: float | None = None) -> list | dict | None:
    """One Supabase REST/RPC call. The socket, per-attempt net.* telemetry and the
    bounded transient retry all live in rt_http; this layer owns the auth headers,
    the RPC-level events and the audit trail.

    Everything is sent ONCE unless _is_read says otherwise: a read-timeout after
    the bytes left may already be committed, and re-sending rt_bump_call /
    rt_upsert_fact / rt_audit would double-count. Reads get rt_http.MAX_ATTEMPTS.
    deadline_s is forwarded as the total wall-clock budget across attempts (None =
    per-attempt timeout only)."""
    db = _db()
    if db is None:
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("net.failed", host=_obs_host(os.getenv("SUPABASE_URL")),
                            path=_obs_path(path), ms=0.0, err="db_unavailable")
        return None
    url, key = db
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "apikey": key, "Authorization": f"Bearer {key}",
        "Content-Type": "application/json", "Prefer": "return=representation",
    }
    _obs_p = _obs_path(path)
    _obs_t0 = time.perf_counter()
    _retries = rt_http.MAX_ATTEMPTS - 1 if _is_read(method, path) else 0
    try:
        result = rt_http.http_client.request(
            method, f"{url}/rest/v1/{path}", headers=headers, data=data, timeout=10,
            retries=_retries, deadline_s=deadline_s)
    except Exception as e:
        # rt_http already emitted net.failed per attempt; this is the RPC-level view.
        rt_obs.obs.caught("rt_prefs._req", e)
        with contextlib.suppress(Exception):
            rt_obs.obs.event("net.rpc_latency", rpc=_obs_p,
                             ms=round((time.perf_counter() - _obs_t0) * 1000, 1),
                             status=getattr(e, "code", None), err=type(e).__name__)
        raise
    _obs_status = None  # per-attempt status rides on rt_http's net.request event
    _obs_ms = round((time.perf_counter() - _obs_t0) * 1000, 1)
    with contextlib.suppress(Exception):
        rt_obs.obs.event("net.rpc_latency", rpc=_obs_p, ms=_obs_ms, status=_obs_status)
    with contextlib.suppress(Exception):
        if _obs_p == _BUNDLE_RPC:
            _bundle = result if isinstance(result, dict) else {}
            rt_obs.obs.event("memory.bundle", ms=_obs_ms,
                             facts=_obs_count(_bundle.get("facts")),
                             schemas=_obs_count(_bundle.get("schemas")),
                             reminders=_obs_count(_bundle.get("reminders")),
                             cached=False)
        _obs_rpc = _obs_p.removeprefix("rpc/")
        if _obs_rpc in _MUTATING_RPCS:
            rt_obs.obs.event("memory.saved", kind=_obs_rpc, count=_obs_count(result),
                             status=_obs_status)
    if not _skip_audit:
        rpc_name = _rpc_name(path)
        if rpc_name in _MUTATING_RPCS:
            audit(rpc_name, body)
    return result


ACTOR = "unknown"
_AUDIT_ENABLED: bool | None = None

_MUTATING_RPCS = {
    "rt_set_display_name", "rt_set_agent_alias", "rt_set_caller_rules",
    "rt_set_persona_directives", "rt_set_voice", "rt_set_loved_ones",
    "rt_set_caller_context", "rt_add_schema_entry", "rt_remove_schema_entry",
    "rt_add_reminder", "rt_complete_reminder", "rt_bump_call",
    "rt_save_call_metrics", "rt_wipe_all_data",
}


def audit(op: str, body: dict | None) -> None:
    """Fire-and-forget write attribution to rt.audit_log (best-effort, non-blocking)."""
    global _AUDIT_ENABLED
    if _AUDIT_ENABLED is False:
        return

    def _send() -> None:
        global _AUDIT_ENABLED
        try:
            args = {k: (str(v)[:200] if v is not None else None) for k, v in (body or {}).items()
                    if k != "p_hash"}
            _req("POST", "rpc/rt_audit", {
                "p_hash": (body or {}).get("p_hash"),
                "p_actor": ACTOR, "p_op": op, "p_args": args,
            }, _skip_audit=True)
            _AUDIT_ENABLED = True
        except Exception as e:
            rt_obs.obs.caught("rt_prefs.audit", e)
            if "404" in str(e) or "PGRST202" in str(e):
                _AUDIT_ENABLED = False
    import threading
    threading.Thread(target=_send, daemon=True).start()


def _deep_merge(old, new):
    """Field-preserving merge: new fields add, shared dict fields recurse,
    scalars update only when the new value is non-empty. Terse re-mentions
    can no longer erase rich earlier detail (the Biscuit-lost-his-breed bug)."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return new if new not in (None, "", {}) else old
    merged = dict(old)
    lower_map = {k.lower(): k for k in old}
    for k, v in new.items():
        target = lower_map.get(k.lower(), k)
        if target in merged:
            merged[target] = _deep_merge(merged[target], v)
        else:
            merged[k] = v
    return merged


def merge_schema_entry(h: str, cat: str, new_obj: dict, existing_schemas: list[dict] | None = None) -> dict:
    """Deep-merge new_obj onto the category's existing data and return the superset.

    The SQL upsert only shallow-merges top-level keys, so callers pass the
    returned superset to rt_add_schema_entry to avoid wholesale entity loss.
    """
    old_obj: dict = {}
    try:
        if existing_schemas is None:
            bundle = _req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
            existing_schemas = bundle.get("schemas") or []
        for s in existing_schemas:
            if (s.get("category") or "").lower() == cat.lower():
                raw = s.get("data_summary")
                old_obj = json.loads(raw) if isinstance(raw, str) else (raw or {})
                break
    except Exception as _exc:
        rt_obs.obs.caught("rt_prefs.merge_schema_entry", _exc)
        old_obj = {}
    if not isinstance(old_obj, dict):
        old_obj = {}
    return _deep_merge(old_obj, new_obj)


_EXCLUSIVE_DIMENSIONS = frozenset({
    "accent", "voice", "language", "dialect", "tone", "volume", "speed", "pace",
    "name", "nickname", "greeting", "pronoun", "pronouns",
})

_DIRECTIVE_STOP = frozenset("""
the a an and or but if is are was were be been being to for from with about
call calls calling want wants wanted would like likes prefer prefers preferred
agent caller user should must always never please just really that this those
them they you your yours his her him she them their our not dont don doesn
me my mine when then than back again more most very can could may might will
bring up get got make made take took give gave keep kept let put say said tell
""".split())


def merge_scalar_rules(existing: str | None, new_rule: str, cap: int = 600) -> str:
    """Append a rule/directive to the existing set instead of overwriting.

    Skips near-duplicates; keeps newest rules when over the cap. Fixes the
    'No politics. Ever.' silently erased by the next rule bug."""
    new_rule = (new_rule or "").strip().rstrip(";")
    parts = [p.strip() for p in (existing or "").split(";") if p.strip() and p.strip().lower() != "none set."]
    def _norm(t: str) -> str:
        return " ".join("".join(c.lower() if c.isalnum() else " " for c in t).split())

    def _topic(t: str) -> set:
        """The distinctive words of a directive — what it is actually ABOUT."""
        return {w for w in _norm(t).split() if w not in _DIRECTIVE_STOP and len(w) > 2}

    if new_rule:
        exact = any(_norm(new_rule) == _norm(p) or _norm(new_rule) in _norm(p)
                    or _norm(p) in _norm(new_rule) for p in parts)
        if not exact:
            nt = _topic(new_rule)
            if nt:
                kept = []
                for p_ in parts:
                    pt = _topic(p_)
                    shared = nt & pt
                    same_dimension = bool(shared & _EXCLUSIVE_DIMENSIONS)
                    close_enough = (bool(shared)
                                    and len(shared) / min(len(nt), len(pt)) >= 0.5)
                    if pt and (same_dimension or close_enough):
                        print(f"[rt-prefs] superseded {p_!r} with {new_rule!r}", flush=True)
                        continue
                    kept.append(p_)
                parts = kept
            parts.append(new_rule)
        else:
            with contextlib.suppress(Exception):
                rt_obs.obs.warn("memory.refused", kind="rule", value="[withheld]",
                                reason="duplicate_of_existing", chars=len(new_rule))
    out = "; ".join(parts)
    _obs_dropped = 0
    while len(out) > cap and len(parts) > 1:
        parts.pop(0)
        _obs_dropped += 1
        out = "; ".join(parts)
    if _obs_dropped:
        with contextlib.suppress(Exception):
            rt_obs.obs.warn("memory.refused", kind="rule", value="[withheld]",
                            reason="over_cap", count=_obs_dropped, cap=cap)
    return out[:cap]


def merge_loved_ones(existing: str | None, new_val: str, cap: int = 500) -> str:
    """Union of loved-ones entries keyed by primary name — the cast may grow
    or update, never silently shrink to whoever was mentioned last call."""

    def _key(entry: str) -> str:
        caps = re.findall(r"[A-Z][a-z]+", entry)
        generic = {"Son", "Daughter", "Dog", "Cat", "Wife", "Husband", "Friend", "Pet",
                   "Sister", "Brother", "Grandson", "Granddaughter", "Late", "The"}
        names = [c for c in caps if c not in generic]
        return (names[0] if names else entry.strip()).lower()

    merged: dict[str, str] = {}
    for src in (existing or "", new_val or ""):
        for entry in src.split(","):
            entry = entry.strip()
            if entry:
                merged[_key(entry)] = entry
    out = ", ".join(merged.values())
    return out[:cap]


def get_caller(caller_e164: str | None) -> dict:
    """Fetch the caller's rt.callers profile bundle via the rt_get_caller RPC."""
    empty = {"voice_pref": None, "call_count": 0, "last_call_at": None}
    try:
        h = phone_hash(caller_e164 or "")
        if not h:
            print("[rt-prefs] get: no usable caller id", flush=True)
            return empty
        if _db() is None:
            print("[rt-prefs] get: DB env missing/refused", flush=True)
            return empty
        row = _req("POST", "rpc/rt_get_caller", {"p_hash": h})
        if isinstance(row, dict):
            print(f"[rt-prefs] get: voice_pref={row.get('voice_pref') or '(none)'} "
                  f"call_count={row.get('call_count', 0)}", flush=True)
            return {**empty, **row}
        print(f"[rt-prefs] get: unexpected rpc reply {type(row).__name__}", flush=True)
    except Exception as e:
        print(f"[rt-prefs] get failed (non-fatal): {e}", flush=True)
        rt_obs.obs.caught("rt_prefs.get_caller", e)
    return empty


def bump_call(caller_e164: str | None) -> int | None:
    """Post-call hook: increment caller call counter via rt_bump_call RPC."""
    try:
        h = phone_hash(caller_e164 or "")
        if not h:
            return None
        n = _req("POST", "rpc/rt_bump_call", {"p_hash": h})
        print(f"[rt-prefs] bump: call_count now {n}", flush=True)
        return n if isinstance(n, int) else None
    except Exception as e:
        print(f"[rt-prefs] bump failed (non-fatal): {e}", flush=True)
        rt_obs.obs.caught("rt_prefs.bump_call", e)
        return None
