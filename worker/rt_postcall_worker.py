"""rt_postcall_worker.py — Post-call memory & persona evolution engine.

Two-pass design:
  Pass 1 — Extract: reads transcript, pulls personal facts in a flat key-value
            format keyed by domain. Persists each domain immediately.
  Pass 2 — Compile: reads ALL accumulated domains and compiles next_call_context
            for the hydrator (<20ms ring-pickup).

NOTE: bump_call is owned by agent.py's _post_call() closure, not here.
      This module must NEVER call rt_prefs.bump_call().

Extraction uses a FIXED set of named output slots, one per domain. The model
fills in fields it knows and leaves the rest null. It is never asked to invent
the category name alongside the value — that produces null categories and
quietly drops facts.
"""

from __future__ import annotations

import rt_obs

import contextlib
import json
import os
import re
import time
import urllib.request
import urllib.error
import rt_prefs
import rt_executor
import rt_shield


def _refused(kind: str, reason: str, value=None, **extra) -> None:
    """One guard rejection, in the operational stream: what kind, and why.

    The value itself never leaves the process — a rejected caller_name is still
    the caller's name. Only its size travels, the way rt_obs treats a
    transcript. Never raises: a refusal that could not be logged must still be
    a refusal that happened.
    """
    with contextlib.suppress(Exception):
        rt_obs.obs.event(
            "memory.refused", kind=kind, reason=reason,
            value=({"chars": len(str(value))} if value is not None else None),
            **extra)


def _caller_lines(transcript: str) -> list[str]:
    return [l for l in transcript.splitlines() if l.strip().lower().startswith("caller:")]


def _heard_by_caller(name: str, transcript: str) -> bool:
    """True if `name` appears in at least one caller-spoken line of the transcript.

    Spelled runs are collapsed first: 'r i c h i e' verifies 'Richie' — a
    spelled correction is the most authoritative form a caller can give.
    """
    n = (name or "").strip().lower()
    if not n:
        return False
    import agent as _ag
    return _ag._heard_in_caller_lines(n, _caller_lines(transcript))


def _email_spoken_by_caller(email: str, transcript: str) -> bool:
    """True only if the CALLER said this exact address on the line.

    The extractor reads the whole transcript, so an address the agent read
    back ("I still have x@y on file, right?") comes out as caller_email too.
    Writing that would let the model — or whoever fed it — overwrite the one
    address recaps go to. Verbatim, case-insensitive, caller lines only.
    """
    e = (email or "").strip().lower()
    if not e or "@" not in e:
        return False
    lines = (transcript or "").splitlines()
    # rt_shield owns the predicate; the local regex only covers the window
    # before it lands. Whole-token match: "ie@gmail.com" is not "richie@gmail.com".
    shared = getattr(rt_shield, "email_spoken_by_caller", None)
    if callable(shared):
        return bool(shared(e, lines))
    pat = re.compile(r"(?<![\w.+-])" + re.escape(e) + r"(?![\w.-])", re.I)
    return any(pat.search(line) for line in lines if line.strip().lower().startswith("caller:"))


# A directive shape only: a verb of contact followed, within the same clause,
# by "me"/"us" — or one of the idioms. "the phone showed me" still slips
# through; "recalled", "texting", "phones", "caller" do not.
_ACTION_REQUEST_RE = re.compile(
    r"\b(call|ring|phone|text|message|email|e-mail|remind|send|buzz)\b[^.?!]{0,30}\b(me|us)\b",
    re.IGNORECASE)
_ACTION_IDIOM_RE = re.compile(
    r"\b(let me know|drop me a line|shoot me|give me a (call|ring|buzz))\b", re.IGNORECASE)
_ACTION_NEGATED_RE = re.compile(r"\b(don[’']?t|do not|never|not|stop)\b", re.IGNORECASE)
_NEGATION_LOOKBACK = 12


def _caller_requested_action(transcript: str) -> bool:
    """True if some caller line asks to be called/texted/emailed/reminded/sent something.

    Directive shapes only ("text me", "give me a ring", "let me know"), and not
    when a negation sits just before ("don't call me"). The agent offering
    ("shall I text you?") never counts; the caller has to say it.
    """
    for line in _caller_lines(transcript or ""):
        for pat in (_ACTION_REQUEST_RE, _ACTION_IDIOM_RE):
            for m in pat.finditer(line):
                before = line[max(0, m.start() - _NEGATION_LOOKBACK):m.start()]
                if _ACTION_NEGATED_RE.search(before):
                    continue
                return True
    return False


# Planner actions that reach a real person. Dropped without an on-record ask.
_OUTBOUND_ACTIONS = frozenset({"schedule_outbound_call", "schedule_sms", "schedule_email"})
# The planner's whole vocabulary. Anything else — including raw job-type names
# like "outbound_call" — never leaves validation.
_PLAN_IMMEDIATE_ALLOWED = frozenset({"send_email_summary", "send_sms_summary"})
_PLAN_SCHEDULED_ALLOWED = frozenset({
    "schedule_research", "schedule_sms", "schedule_email",
    "set_dated_reminder", "schedule_outbound_call",
})


def _validate_plan(plan: dict) -> dict:
    """Allowlist the planner's output BEFORE any other filter reads it.

    Model-written names are untrusted: an unknown verb, a raw job type, or a
    non-dict entry is dropped and counted, never mapped by best effort.
    """
    out: dict = {"immediate": [], "scheduled": []}
    for bucket, allowed in (("immediate", _PLAN_IMMEDIATE_ALLOWED),
                            ("scheduled", _PLAN_SCHEDULED_ALLOWED)):
        raw = (plan or {}).get(bucket)
        for a in (raw if isinstance(raw, list) else []):
            name = str((a or {}).get("action") or "").strip() if isinstance(a, dict) else ""
            if name in allowed:
                out[bucket].append(a)
                continue
            print(f"[rt-planner] DROP {bucket} action {_loggable(name[:40], 'action')}: not a planner action", flush=True)
            _refused("planner_action", "unknown_action", name, bucket=bucket)
    return out


def _drop_unrequested_actions(actions: list, requested: bool) -> list:
    """Without an on-record ask, nothing that rings, texts or emails leaves the planner.

    Dropped by TARGET: send_* and the schedule_* verbs that reach a phone or
    inbox. set_dated_reminder and schedule_research survive — notes to the
    companion, not outbound touches.
    """
    if requested:
        return list(actions or [])
    kept = []
    for a in actions or []:
        name = str((a or {}).get("action") or "").strip()
        if name in _OUTBOUND_ACTIONS or name.startswith("send_"):
            print(f"[rt-planner] DROP {name}: caller never asked for a call/text/email", flush=True)
            _refused("planner_action", "not_requested_by_caller", name)
            continue
        kept.append(a)
    return kept


def _mask(e164) -> str:
    """Last four digits only — the number is PII and the log is not the place for it."""
    return f"***{str(e164 or '')[-4:]}"


def _loggable(value, label: str = "") -> str:
    """The value itself only under RT_LOG_TRANSCRIPT=1; otherwise just its size.

    A name, a rule, a reminder or a saved fact is the caller's memory, and the
    operational log is not the place for it.
    """
    if os.getenv("RT_LOG_TRANSCRIPT") == "1":
        return repr(value)
    return f"<{label or 'value'} {len(str(value or ''))} chars>"


def _guarded_directive(kind: str, text: str | None, transcript: str, call_id: str | None) -> str | None:
    """A caller_rules / persona_directives value, or None when it fails the shield.

    Both are rendered into the next system prompt, so they get the same two
    checks the in-call tool applies: no steering phrases or tool names, and
    the caller must actually have said it.
    """
    if not text:
        return None
    ok, why = rt_shield.rule_text_allowed(text)
    if not ok:
        print(f"[rt-guard] REJECT {kind} — {why}", flush=True)
        _refused(kind, "blocked_phrase", text, call_id=call_id)
        return None
    if not rt_shield.spoken_by_caller(text, (transcript or "").splitlines()):
        print(f"[rt-guard] REJECT {kind} — not spoken by the caller", flush=True)
        _refused(kind, "not_spoken_by_caller", text, call_id=call_id)
        return None
    return text


def _norm_text(t: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in (t or "")).split())


_REM_PRONOUNS = {"me", "you", "him", "her", "them", "us", "my", "your", "his", "their", "i"}


def _strip_pronouns(t: str) -> str:
    """Drop the point-of-view words so the same errand matches itself."""
    return " ".join(w for w in (t or "").split() if w not in _REM_PRONOUNS)


def _same_reminder(a: str, b: str) -> bool:
    """Fuzzy reminder equality: normalized containment or >=80% token overlap.

    Catches phrasing drift between the in-call write and postcall re-extraction
    ("pick up a kite later today" vs "to pick up a kite") without collapsing
    genuinely distinct short reminders.
    """
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if _strip_pronouns(na) and _strip_pronouns(na) == _strip_pronouns(nb):
        return True
    ta, tb = set(_strip_pronouns(na).split()), set(_strip_pronouns(nb).split())
    if min(len(ta), len(tb)) < 3:
        return False
    if na in nb or nb in na:
        return True
    return len(ta & tb) / min(len(ta), len(tb)) >= 0.75


def _route_credentials(obj: dict, path: str = "") -> tuple[dict, dict]:
    """Split credential-looking pairs out of a domain object tree.

    Returns (kept, creds): kept is the object with credential pairs removed,
    creds maps dotted paths to the raw values, destined for CRED_CATEGORY.
    """
    kept: dict = {}
    creds: dict = {}
    for k, v in obj.items():
        if isinstance(v, dict):
            sub_kept, sub_creds = _route_credentials(v, f"{path}{k}.")
            creds.update(sub_creds)
            if sub_creds:
                if sub_kept:
                    kept[k] = sub_kept
            else:
                kept[k] = v
        elif rt_prefs.looks_credential(f"{k}: {v}"):
            creds[f"{path}{k}"] = str(v)
        else:
            kept[k] = v
    return kept, creds


def validate_extracted(extracted: dict, transcript: str) -> tuple[dict, list[str]]:
    """Deterministic gate between pass-1 output and the DB.

    Returns (cleaned_extraction, clarifications) where clarifications is a list of
    (key, question) pairs. Identity fields and proper-noun facts survive only if the
    caller actually spoke them; everything dropped becomes a clarification for the
    next call instead of silent truth.
    """
    out = dict(extracted)

    _kept_corr = []
    for _c in (out.get("corrections") or []):
        if isinstance(_c, dict) and _heard_by_caller(str(_c.get("right") or ""), transcript):
            _kept_corr.append(_c)
        elif isinstance(_c, dict):
            print(f"[rt-guard] REJECT correction {_loggable(_c.get('right'), 'correction')} — not spoken by the caller", flush=True)
            _refused("correction", "not_spoken_by_caller", _c.get("right"))
    out["corrections"] = _kept_corr
    clarifications: list[tuple[str, str]] = []

    name = _str(out.get("caller_name"))
    if name and name.lower() in {"your companion", (_str(out.get("agent_alias")) or "").lower()}:
        print(f"[rt-guard] REJECT caller_name {_loggable(name, 'name')} — matches the agent's name/alias", flush=True)
        _refused("caller_name", "matches_agent_alias", name)
        clarifications.append(("caller_name",
            "Their name hasn't been confirmed yet — ask for it naturally."))
        out["caller_name"] = None
        name = None
    if name and not _heard_by_caller(name, transcript):
        print(f"[rt-guard] REJECT caller_name {_loggable(name, 'name')} — not present in any caller: line", flush=True)
        _refused("caller_name", "not_in_caller_lines", name)
        clarifications.append(("caller_name",
            f"The name '{name}' was used in conversation but the caller never confirmed it — gently confirm their name."))
        out["caller_name"] = None

    alias = _str(out.get("agent_alias"))
    if alias and not _heard_by_caller(alias, transcript):
        print(f"[rt-guard] REJECT agent_alias {_loggable(alias, 'alias')} — not present in any caller: line", flush=True)
        _refused("agent_alias", "not_in_caller_lines", alias)
        clarifications.append(("agent_alias",
            f"A rename to '{alias}' was recorded but the caller never said it — confirm what they'd like to call you."))
        out["agent_alias"] = None

    lo = _str(out.get("loved_ones"))
    if lo:
        _GENERIC = {"Dog", "Cat", "Bird", "Fish", "Pet", "Son", "Daughter", "Wife", "Husband",
                    "Friend", "Sister", "Brother", "Mom", "Dad", "Mother", "Father", "Grandson",
                    "Granddaughter", "Grandchild", "Spouse", "Partner", "The", "Their", "Her", "His"}
        kept = []
        for entry in lo.split(","):
            names = [w for w in re.findall(r"[A-Za-z][a-z]+", entry)
                     if w[0].isupper() and len(w) > 2 and w not in _GENERIC]
            if not names or any(_heard_by_caller(w, transcript) for w in names):
                kept.append(entry.strip())
            else:
                print(f"[rt-guard] REJECT loved_ones entry {_loggable(entry.strip(), 'entry')} — no name backed by caller: lines", flush=True)
                _refused("loved_ones", "name_not_backed_by_caller", entry)
        out["loved_ones"] = ", ".join(kept) if kept else None

    for domain in ("pets", "family"):
        obj = out.get(domain)
        if isinstance(obj, dict):
            cleaned = {}
            for k, v in obj.items():
                if k[:1].isupper() and not _heard_by_caller(k, transcript):
                    print(f"[rt-guard] REJECT {domain} entry {_loggable(k, 'entity')} — never spoken by caller", flush=True)
                    _refused(domain, "entity_not_spoken", k)
                    continue
                cleaned[k] = v
            out[domain] = cleaned or None

    creds: dict = {}
    for domain in _DOMAIN_SLOTS:
        raw = out.get(domain)
        obj = raw
        if isinstance(raw, str):
            try:
                obj = json.loads(raw)
            except Exception as _exc:
                rt_obs.obs.caught("rt_postcall_worker.validate_extracted", _exc)
                obj = None
        if isinstance(obj, dict):
            kept, found = _route_credentials(obj, f"{domain}.")
            if found:
                for ck in found:
                    print(f"[rt-guard] ROUTE credential-looking fact [{ck}] → {rt_prefs.CRED_CATEGORY}", flush=True)
                out[domain] = kept or None
                creds.update(found)
        elif isinstance(raw, str) and rt_prefs.looks_credential(raw):
            print(f"[rt-guard] ROUTE credential-looking fact [{domain}] → {rt_prefs.CRED_CATEGORY}", flush=True)
            creds[domain] = raw
            out[domain] = None

    rems = out.get("reminders")
    if isinstance(rems, list):
        kept_rems = []
        for rem in rems:
            text = rem.get("text", "") if isinstance(rem, dict) else str(rem)
            if rt_prefs.looks_credential(text):
                key = "_".join(re.findall(r"[a-zA-Z]+", text.lower())[:4]) or "note"
                print(f"[rt-guard] ROUTE credential-looking reminder → {rt_prefs.CRED_CATEGORY}", flush=True)
                creds[key] = text
            else:
                kept_rems.append(rem)
        out["reminders"] = kept_rems

    # Sanitize opening_bridge
    ob = _str(out.get("opening_bridge"))
    out["opening_bridge"] = ob if ob and len(ob) <= 300 else None

    # Sanitize emotional_tone
    et = _str(out.get("emotional_tone"))
    out["emotional_tone"] = et if et and len(et) <= 200 else None

    # Sanitize milestones
    raw_milestones = out.get("milestones")
    kept_milestones = []
    if isinstance(raw_milestones, list):
        for m in raw_milestones:
            if isinstance(m, dict) and m.get("event"):
                kept_milestones.append({
                    "event": str(m.get("event"))[:120],
                    "date_text": str(m.get("date_text") or "")[:80],
                    "detail": str(m.get("detail") or "")[:200],
                })
    out["milestones"] = kept_milestones if kept_milestones else None

    # Sanitize goals
    raw_goals = out.get("goals")
    kept_goals = []
    if isinstance(raw_goals, list):
        for g in raw_goals:
            if isinstance(g, dict) and (g.get("title") or g.get("goal")):
                kept_goals.append({
                    "title": str(g.get("title") or g.get("goal"))[:120],
                    "target_date": str(g.get("target_date") or "")[:80],
                    "status": str(g.get("status") or "active")[:20],
                    "notes": str(g.get("notes") or "")[:200],
                })
    elif isinstance(raw_goals, dict):
        for k, v in raw_goals.items():
            if isinstance(v, dict):
                kept_goals.append({
                    "title": str(v.get("title") or k)[:120],
                    "target_date": str(v.get("target_date") or "")[:80],
                    "status": str(v.get("status") or "active")[:20],
                    "notes": str(v.get("notes") or "")[:200],
                })
    out["goals"] = kept_goals if kept_goals else None

    if creds:
        existing_creds = out.get(rt_prefs.CRED_CATEGORY) or {}
        if isinstance(existing_creds, dict):
            creds.update(existing_creds)
        out[rt_prefs.CRED_CATEGORY] = creds

    out = rt_prefs.scrub_ssn(out)

    return out, clarifications


def apply_interest_decay(h_hash: str, topics: list, pre_schemas: list[dict], pre_caller: dict | None = None) -> list[str]:
    """Cool topics the caller waved off when the agent raised them.

    'I'm not interested in that anymore' — or a gloss-over — marks the matching
    stored facts {"interest": "cooled"}; the compiler then treats them as
    background the agent may answer about but never proactively raises again.
    """
    cooled: list[str] = []
    _STOP = {"not", "the", "and", "for", "with", "about", "that", "this", "them", "from"}
    for topic in (topics or []):
        if not (isinstance(topic, str) and topic.strip()):
            continue
        words = [w for w in re.findall(r"[a-z0-9]{2,}", topic.lower()) if w not in _STOP]
        if not words:
            continue
        for s in pre_schemas:
            cat = (s.get("category") or "").lower()
            if cat in _SPECIAL_CATS:
                continue
            data = (s.get("data_summary") or "")
            if not any(w in data.lower() for w in words):
                continue
            try:
                obj = json.loads(data)
            except Exception as _exc:
                rt_obs.obs.caught("rt_postcall_worker.apply_interest_decay", _exc)
                continue
            if not isinstance(obj, dict):
                continue
            marks = {}
            for k, v in obj.items():
                if any(w in k.lower() or w in str(v).lower() for w in words):
                    marks[k] = {**v, "interest": "cooled"} if isinstance(v, dict) else v
                    if not isinstance(v, dict):
                        marks[k] = v
            marks["_cooled"] = sorted(set((obj.get("_cooled") or []) + [topic.strip()])) \
                if isinstance(obj.get("_cooled"), list) else [topic.strip()]
            _safe_rpc("rt_add_schema_entry", {
                "p_hash": h_hash, "p_table": f"caller_{h_hash[:8]}_{cat}",
                "p_cat": cat, "p_summary": json.dumps(marks)})
            cooled.append(f"{topic.strip()} [{cat}]")
            print(f"[rt-decay] cooled topic {_loggable(topic.strip(), 'topic')} in [{cat}]", flush=True)

        # Also retire matching facts in rt.facts
        try:
            facts_res = _safe_rpc("rt_get_facts", {"p_hash": h_hash, "p_limit": 100})
            for f in (facts_res or []):
                f_id = f.get("id")
                f_key = (f.get("norm_key") or f.get("subject") or "").lower()
                f_val = (f.get("value_text") or str(f.get("value_json") or "")).lower()
                if f_id and any(w in f_key or w in f_val for w in words):
                    _safe_rpc("rt_retire_fact", {"p_hash": h_hash, "p_id": f_id})
                    print(f"[rt-decay] retired fact {_loggable(f_key, 'fact')}", flush=True)
        except Exception as _exc:
            rt_obs.obs.caught("rt_postcall_worker.apply_interest_decay.facts", _exc)

        # Also prune loved_ones if caller expressed disengagement
        try:
            c_info = pre_caller or _safe_rpc("rt_get_caller", {"p_hash": h_hash}) or {}
            lo_raw = (c_info.get("loved_ones") or "").strip()
            if lo_raw:
                lo_entries = [e.strip() for e in lo_raw.split(",") if e.strip()]
                kept_lo = [e for e in lo_entries if not any(w in e.lower() for w in words)]
                if len(kept_lo) != len(lo_entries):
                    _safe_rpc("rt_set_loved_ones", {"p_hash": h_hash, "p_loved_ones": ", ".join(kept_lo)})
                    print(f"[rt-decay] pruned loved_ones: {len(lo_entries)} -> {len(kept_lo)} entries", flush=True)
        except Exception as _exc:
            rt_obs.obs.caught("rt_postcall_worker.apply_interest_decay.loved_ones", _exc)

    return cooled


_GENERIC_ALIASES = {"iris", "your companion", "companion", "phone-pal", "phone pal", "pal"}

ONBOARDING_STEPS = ("name", "intro", "ownership", "first_fact")
_SPECIAL_CATS = {"clarifications", "wellbeing", "onboarding", "agent_tasks", "credentials",
                 "call_log", "bridge_log", "scam_reports", "daily_minutes",
                 # ours/her_side are the friendship, not facts about the caller.
                 # Left out, they were mined into the fact canvas as if he had
                 # stated them, and walked by detect_conflicts — which would ask
                 # him to confirm HER opinion about being busy.
                 "ours", "her_side"}

_NOT_FIRST_NAME = ("surname", "last name", "family name", "middle name", "maiden",
                   "rename", "nickname", "your name", "call you", "spell")


def _asks_caller_first_name(key: str, question: str) -> bool:
    """True only for 'what should I call this person?'.

    Knowing their first name resolves that question and nothing else. A bare
    substring test for "name" also matches the surname question, the
    agent-rename confirmation, and "what is their doctor's name" — and would
    silently drop all three the moment any first name is known.
    """
    if key == "caller_name":
        return True
    q = (question or "").lower()
    if "name" not in q:
        return False
    return not any(w in q for w in _NOT_FIRST_NAME)


def update_call_log(h_hash: str, summary: str | None, pre_schemas: list[dict], visit: int,
                    caller_e164: str | None = None) -> None:
    """Append this call (local timestamp + summary) to the rolling 3-call history.

    The hydrator renders it so she can say 'last time we spoke, you told me…'
    with the real date instead of a confident guess.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        # The caller's own date. Rendered back to them as "last time we spoke",
        # so a five-hour error can put the conversation on the wrong DAY.
        import rt_timezone
        tz, _ = rt_timezone.zone_or_default(caller_e164, os.getenv("DEFAULT_TZ", "America/New_York"))
        stamp = datetime.now(ZoneInfo(tz)).strftime("%A, %B %d, %Y at %I:%M %p %Z")
    except Exception as _exc:
        rt_obs.obs.caught("rt_postcall_worker.update_call_log", _exc)
        stamp = ""
    calls: list = []
    for s in pre_schemas:
        if (s.get("category") or "").lower() == "call_log":
            try:
                calls = (json.loads(s.get("data_summary") or "{}") or {}).get("calls") or []
            except Exception as _exc:
                rt_obs.obs.caught("rt_postcall_worker.update_call_log", _exc)
                calls = []
            break
    calls = [c for c in calls if isinstance(c, dict)]
    calls.append({"n": visit, "at": stamp, "summary": (summary or "").strip()})
    _safe_rpc("rt_add_schema_entry", {
        "p_hash": h_hash, "p_table": f"caller_{h_hash[:8]}_call_log",
        "p_cat": "call_log", "p_summary": json.dumps({"calls": calls[-3:]})})
    print(f"[rt-calllog] call #{visit} logged ({len(calls[-3:])} in rolling history)", flush=True)


def update_onboarding(h_hash: str, transcript: str, pre_caller: dict,
                      pre_schemas: list[dict], extracted: dict | None = None) -> dict:
    """Track onboarding as COMPLETED OUTCOMES, not a call counter.

    The old call_count==0 gate meant short hangups burned onboarding without it
    ever happening. Each postcall now checks, deterministically, which beats have
    actually landed; the hydrator keeps weaving in the missing ones until all
    four are done. Sticky — steps never un-complete.
    """
    cur: dict = {}
    for s in pre_schemas:
        if (s.get("category") or "").lower() == "onboarding":
            try:
                cur = json.loads(s.get("data_summary") or "{}")
            except Exception as _exc:
                rt_obs.obs.caught("rt_postcall_worker.update_onboarding", _exc)
                cur = {}
            break
    if not isinstance(cur, dict):
        cur = {}
    if cur.get("done"):
        return cur

    agent_text = " ".join(l for l in transcript.splitlines()
                          if l.strip().lower().startswith("agent:")).lower()
    upd = dict(cur)

    name_now = (pre_caller.get("display_name") or "").strip()
    if extracted and extracted.get("caller_name"):
        name_now = str(extracted["caller_name"])
    if name_now and name_now != "Friend":
        upd["name"] = True

    _intro_re = re.compile(
        r"(friendly voice|voice you can call|companion|day or night|"
        r"(call|ring|phone) (me |us )?(any ?time|whenever|day or night))", re.I)
    if _intro_re.search(agent_text):
        upd["intro"] = True

    if "remember" in agent_text and any(m in agent_text for m in
                                        ("rule", "rename", "different name", "own this",
                                         "new name", "change my name", "call me something",
                                         "your space", "belongs to you",
                                         "personality", "shape", "role you")):
        upd["ownership"] = True

    has_fact = bool((pre_caller.get("loved_ones") or "").strip()) or any(
        (s.get("category") or "").lower() not in _SPECIAL_CATS for s in pre_schemas)
    # Only slots that describe the CALLER may satisfy this step. ours/her_side
    # describe the relationship and her own side of it, and _fold_relationship_
    # signals populates ours on nearly every call with a "how it felt" note — so
    # counting them let her complete "learn one real thing about them" using a
    # sentence she wrote about herself, and retire the onboarding block having
    # learned nothing. Graduating onboarding on your own vibe note is not
    # learning someone.
    if extracted and any(extracted.get(d) not in (None, "", "null", {})
                         for d in _caller_domain_slots()):
        has_fact = True
    if has_fact:
        upd["first_fact"] = True

    upd["done"] = all(upd.get(k) for k in ONBOARDING_STEPS)
    if upd != cur:
        _safe_rpc("rt_add_schema_entry", {
            "p_hash": h_hash, "p_table": f"caller_{h_hash[:8]}_onboarding",
            "p_cat": "onboarding", "p_summary": json.dumps(upd)})
        print("[rt-onboarding] progress: "
              + " ".join(f"{k}={'✓' if upd.get(k) else '·'}" for k in ONBOARDING_STEPS)
              + (" DONE" if upd["done"] else ""), flush=True)
    return upd


def detect_conflicts(extracted: dict, pre_schemas: list[dict]) -> list[tuple[str, str, str, str]]:
    """Find stored facts this call's statements contradict.

    A changed value is not enrichment (containment either way) but a genuine
    swap — '$22 per sack' → '$24 per sack', 'Friday' → 'Tuesday'. Storage takes
    the caller's newest word (deep-merge lets non-empty scalars win); the
    de-conflicter's job is to queue a clarification so the agent confirms the
    change naturally instead of silently carrying two truths.
    Returns up to 4 of (domain, key, old, new).
    """
    pre: dict = {}
    for s in pre_schemas:
        cat = (s.get("category") or "").lower()
        if cat in ("clarifications", "wellbeing", rt_prefs.CRED_CATEGORY):
            continue
        try:
            obj = json.loads(s.get("data_summary") or "{}")
        except Exception as _exc:
            rt_obs.obs.caught("rt_postcall_worker.detect_conflicts", _exc)
            continue
        if isinstance(obj, dict):
            pre[cat] = obj

    conflicts: list[tuple[str, str, str, str]] = []

    def walk(domain: str, new_d: dict, old_d: dict) -> None:
        lower = {k.lower(): (k, v) for k, v in old_d.items()}
        for k, v in new_d.items():
            hit = lower.get(k.lower())
            if hit is None:
                continue
            ok_key, ov = hit
            if isinstance(v, dict) and isinstance(ov, dict):
                walk(domain, v, ov)
                continue
            if isinstance(v, (dict, list)) or isinstance(ov, (dict, list)):
                continue
            ns, os_ = str(v or "").strip(), str(ov or "").strip()
            if not ns or not os_ or ns.lower() in ("null", "none") or os_.lower() in ("null", "none"):
                continue
            a, b = _norm_text(ns), _norm_text(os_)
            if a == b or a in b or b in a:
                continue
            conflicts.append((domain, ok_key, os_, ns))

    for domain in _DOMAIN_SLOTS:
        new_obj = extracted.get(domain)
        if isinstance(new_obj, str):
            try:
                new_obj = json.loads(new_obj)
            except Exception as _exc:
                rt_obs.obs.caught("rt_postcall_worker.detect_conflicts", _exc)
                continue
        if isinstance(new_obj, dict) and pre.get(domain):
            walk(domain, new_obj, pre[domain])
    return conflicts[:4]


_LIFECYCLE_STATUS = {"deceased", "sold", "completed", "moved_away", "gone"}


def apply_lifecycle_events(h_hash: str, events: list, pre_schemas: list[dict],
                           pre_reminders: list[dict]) -> list[str]:
    """Mark entities whose life status changed, and retire their obligations.

    A death/sale/completion writes {status, note} onto the entity in its home
    domain and auto-completes any pending reminder naming it — so a canvas can
    never again hand a widow her dead dog's vet appointment.
    """
    applied: list[str] = []
    for ev in (events or []):
        if not isinstance(ev, dict):
            continue
        entity = (ev.get("entity") or "").strip()
        transition = (ev.get("transition") or "").strip().lower()
        note = (ev.get("note") or "").strip()
        if not entity or transition not in _LIFECYCLE_STATUS:
            continue
        home_cat = "general"
        for s in pre_schemas:
            cat = (s.get("category") or "").lower()
            if cat in ("clarifications", rt_executor.TASK_CATEGORY, rt_prefs.CRED_CATEGORY):
                continue
            if entity.lower() in (s.get("data_summary") or "").lower():
                home_cat = cat
                break
        _upsert_domain(h_hash, home_cat,
                       {entity: {"status": transition, "note": note or transition}},
                       existing_schemas=pre_schemas)
        for r in pre_reminders:
            txt = r.get("reminder_text") or ""
            if entity.lower() in txt.lower():
                _safe_rpc("rt_complete_reminder", {"p_hash": h_hash, "p_text": txt})
                print(f"[rt-lifecycle] auto-completed reminder for {_loggable(entity, 'entity')}: {_loggable(txt, 'reminder')}", flush=True)
        applied.append(f"{entity}:{transition}")
        print(f"[rt-lifecycle] {_loggable(entity, 'entity')} → {transition} (domain {home_cat})", flush=True)
    return applied


_CMD_STOP = {"clear", "delete", "remove", "strike", "cross", "erase", "forget", "wipe",
             "those", "these", "that", "them", "all", "every", "everything", "old",
             "notes", "note", "reminder", "reminders", "about", "please", "anything",
             "item", "items", "list", "stuff", "things", "memory", "records",
             "the", "out", "get", "for", "and", "you", "can", "off", "any", "our", "her", "his"}


def apply_memory_commands(h_hash: str, commands: list, pre_schemas: list[dict],
                          pre_reminders: list[dict], pre_caller: dict | None = None) -> list[str]:
    """Execute the caller's explicit clean-up commands against the ledger.

    'Clear the old vet notes' must actually clear — the caller's curation is
    law for memory, not just for the mouth. Reminders matching a command's
    significant words complete; schema keys/values matching are blanked,
    facts are retired, and loved_ones entries are pruned.
    """
    executed: list[str] = []
    for cmd in (commands or []):
        if not (isinstance(cmd, str) and cmd.strip()):
            continue
        words = [w for w in re.findall(r"[a-z0-9]{2,}", cmd.lower()) if w not in _CMD_STOP]
        if not words:
            continue
        hits = 0
        for r in pre_reminders:
            txt = (r.get("reminder_text") or "").lower()
            if any(w in txt for w in words):
                _safe_rpc("rt_complete_reminder", {"p_hash": h_hash, "p_text": r.get("reminder_text")})
                hits += 1
        for s in pre_schemas:
            cat = (s.get("category") or "").lower()
            if cat in ("clarifications", rt_executor.TASK_CATEGORY):
                continue
            try:
                obj = json.loads(s.get("data_summary") or "{}")
            except Exception as _exc:
                rt_obs.obs.caught("rt_postcall_worker.apply_memory_commands", _exc)
                continue
            if not isinstance(obj, dict):
                continue
            doomed = {k: "" for k, v in obj.items() if any(w in k.lower() or w in str(v).lower() for w in words)}
            if doomed:
                _safe_rpc("rt_add_schema_entry", {
                    "p_hash": h_hash, "p_table": f"caller_{h_hash[:8]}_{cat}",
                    "p_cat": cat, "p_summary": json.dumps(doomed)})
                hits += len(doomed)

        # Retire matching facts in rt.facts
        try:
            facts_res = _safe_rpc("rt_get_facts", {"p_hash": h_hash, "p_limit": 100})
            for f in (facts_res or []):
                f_id = f.get("id")
                f_key = (f.get("norm_key") or f.get("subject") or "").lower()
                f_val = (f.get("value_text") or str(f.get("value_json") or "")).lower()
                if f_id and any(w in f_key or w in f_val for w in words):
                    _safe_rpc("rt_retire_fact", {"p_hash": h_hash, "p_id": f_id})
                    hits += 1
                    print(f"[rt-memory-cmd] retired fact {_loggable(f_key, 'fact')}", flush=True)
        except Exception as _exc:
            rt_obs.obs.caught("rt_postcall_worker.apply_memory_commands.facts", _exc)

        # Prune matching loved_ones entries
        try:
            c_info = pre_caller or _safe_rpc("rt_get_caller", {"p_hash": h_hash}) or {}
            lo_raw = (c_info.get("loved_ones") or "").strip()
            if lo_raw:
                lo_entries = [e.strip() for e in lo_raw.split(",") if e.strip()]
                kept_lo = [e for e in lo_entries if not any(w in e.lower() for w in words)]
                if len(kept_lo) != len(lo_entries):
                    _safe_rpc("rt_set_loved_ones", {"p_hash": h_hash, "p_loved_ones": ", ".join(kept_lo)})
                    hits += len(lo_entries) - len(kept_lo)
                    print(f"[rt-memory-cmd] pruned loved_ones: {len(lo_entries)} -> {len(kept_lo)} entries", flush=True)
        except Exception as _exc:
            rt_obs.obs.caught("rt_postcall_worker.apply_memory_commands.loved_ones", _exc)

        executed.append(f"{cmd[:50]} ({hits} items)")
        print(f"[rt-memory-cmd] executed {_loggable(cmd, 'command')}: {hits} items cleared", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.event("memory.forgotten", tables=["reminders", "schema_entries", "facts", "loved_ones"],
                             rows=hits, via="memory_command")
    return executed


def _gemini_json(api_key: str, prompt: str, temperature: float = 0.1, retries: int = 5) -> dict:
    """Call Gemini 2.5 Flash with JSON output mode and return parsed dict.

    Retries up to `retries` times on 429/500/503 with exponential backoff.
    """
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": temperature},
    }
    # Key goes in a header, never the query string — URLs land in proxy logs
    # and tracebacks; headers mostly don't.
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(  # noqa: S310 - fixed https Gemini URL
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed https Gemini URL
                data = json.loads(resp.read().decode("utf-8"))
                raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
                if raw.startswith("```"):
                    raw = re.sub(r"^```(?:json)?\n?", "", raw)
                    raw = re.sub(r"\n?```$", "", raw).strip()
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 503) and attempt < retries - 1:
                wait = 2 ** attempt
                print(f"[rt-postcall] Gemini {e.code} on attempt {attempt+1}, retrying in {wait}s", flush=True)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            rt_obs.obs.caught("rt_postcall_worker._gemini_json", e)
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
    raise last_err


_ONE_SIDED_AFTER = 4


def _as_bool(v) -> bool:
    """The model returns JSON, but not reliably typed.

    `he_asked_about_her` was checked with `is False`, so the string "false" —
    which a JSON-ish model emits often — read as truthy and silently inverted
    the meaning.
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


_RELATIONSHIP_SLOTS = ("ours", "her_side")


def _caller_domain_slots() -> list[str]:
    return [d for d in _DOMAIN_SLOTS if d not in _RELATIONSHIP_SLOTS]


def _fold_relationship_signals(extracted: dict, existing_schemas: list | None = None) -> None:
    """Fold `connection` and `he_asked_about_her` into the `ours` domain.

    Both describe the relationship rather than the caller, but neither is an
    object, so the generic domain loop would drop them on the floor. Folding
    beats adding two more storage paths: they persist, they merge, and they
    surface in the next call's context through machinery that already exists.

    `he_asked_about_her` is only recorded when it is False. A friendship where
    one person is never asked about is the thing worth noticing; recording the
    ordinary case would just burn prompt budget saying nothing.
    """
    if not isinstance(extracted, dict):
        return

    ours = extracted.get("ours")
    if not isinstance(ours, dict):
        ours = {} if ours in (None, "", "null") else {"note": str(ours)}

    conn = extracted.get("connection")
    if isinstance(conn, str) and conn.strip() and conn.strip().lower() != "null":
        ours["how it felt"] = conn.strip()

    # Domains MERGE rather than replace, so anything written here is written
    # forever. The first version of this set a "one-sided" note whenever a caller
    # did not ask about her — and nothing could ever clear it. One distracted
    # call and she carried "they never ask about you" into every future call for
    # the rest of the friendship. That is not noticing, that is a grudge, and a
    # companion who cannot forgive is worse than one who cannot remember.
    #
    # So it is symmetric and self-healing: asking clears it, and it is only ever
    # set when the pattern has held for several calls in a row. The counter, not
    # the note, is the memory.
    # The streak must come from what is STORED, not from `extracted`. Gemini
    # rebuilds `extracted` fresh every call and `_one_sided_streak` is not in the
    # extraction schema, so reading it from there made it permanently 0 — the
    # branch could never fire in production. The harness passed only because the
    # test fed the counter back in itself, which is a test proving its own
    # fixture rather than the system. Read it from the persisted `ours` row.
    prior = {}
    for _s in existing_schemas or []:
        if (_s.get("category") or "").lower() == "ours":
            raw = _s.get("data_summary")
            try:
                prior = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except Exception as _exc:
                rt_obs.obs.caught("rt_postcall_worker._build_ours_schema", _exc)
                prior = {}
            break
    asked = _as_bool(extracted.get("he_asked_about_her"))
    streak = int(prior.get("_streak") or 0)
    if asked:
        ours.pop("one-sided", None)
        ours["they asked about you"] = "they asked how you were this call"
        ours["_streak"] = 0
        extracted["_one_sided_streak"] = 0
    else:
        ours["_streak"] = streak + 1
        extracted["_one_sided_streak"] = streak + 1
        if streak + 1 >= _ONE_SIDED_AFTER:
            ours["one-sided"] = (f"the last {streak + 1} calls have been about them — "
                                 f"not a complaint, just something you have noticed")

    extracted["ours"] = ours or None


_DOMAIN_SLOTS = [
    "family",
    "pets",
    "hobbies",
    "health",
    "vehicles",
    "work",
    "preferences",
    "places",
    "finances",
    # ── everything above this line is a fact about the CALLER ──
    # The relationship itself. Every slot above this line records something
    # about the caller; a perfect file on a person is not a friendship. These
    # two record what happened BETWEEN them, and what she herself said — so she
    # is consistent across calls, and so there is an "us" to remember at all.
    "ours",
    "her_side",
]

_EXTRACTION_PROMPT = """Analyze this transcript between a caller and their AI voice companion.

Extract structured facts according to these STRICT rules:
- Only extract facts EXPLICITLY STATED or confirmed by the caller. Do not infer or invent.
- If a fact is negative ("I don't have kids", "Biscuit passed away"), capture it accurately.
- Do NOT extract credentials, passwords, or credit card numbers.
- open_threads is for genuinely unresolved things worth asking about next time (a pending
  decision, a hard patch, something they're waiting to hear about) — NOT routine facts that
  already belong in a domain slot below. A stated hobby is a fact; "nervous about the biopsy
  results Thursday" is a thread. Most calls have zero threads — empty list is the normal case.
- The domain slots (family, pets, hobbies, health, vehicles, work, preferences, places, finances, skills, ours, her_side) are JSON OBJECTS or null — never a string, never a list.
- Every person or pet named goes in BOTH loved_ones and its domain slot (family / pets).
- `ours` and `her_side` are the relationship, not the file. Every other slot records
  something about the caller; those two record what passed BETWEEN them and what the
  companion herself said. Be strict about the difference: "he restores a '68 Mustang" is
  a fact and belongs in vehicles; "he walked you through the engine rebuild for twenty
  minutes and you told him it sounded like church" belongs in ours. Do not duplicate a
  fact into ours just to fill it — an empty `ours` on a transactional call is the honest
  answer, and worth knowing.

Return ONLY valid JSON with EXACTLY this shape — do not add or remove keys:

{
  "caller_name": "their FIRST name / what to call them, or null",
  "last_name": "their family name ONLY if they said or spelled it (a spelled correction is the authoritative version), or null",
  "agent_alias": "custom name they gave their voice companion, or null",
  "caller_email": "their email address if they said it during the call, or null",
  "caller_rules": "explicit rules, boundaries, or instructions the caller set for how you speak or operate, or null",
  "persona_directives": "tone, style, or personality preferences requested by the caller, or null",
  "loved_ones": "COMMA-SEPARATED entries, one per person or pet — name, relationship, key detail. Example: 'Son Richie Jr (Jersey City), Dog Sparta (3yo German Shepherd female)'. Not a sentence. null if none.",
  "reminders": [
    {"text": "what to remind them of", "due": "when or null"}
  ],
  "completed_reminders": [
    "reminders the caller said they completed, took care of, scratched, or canceled"
  ],
  "clarifications": [
    "short questions about facts left ambiguous or unconfirmed this call, worth gently confirming next time. Empty list if nothing is unclear."
  ],
  "agent_tasks": [
    "things the caller asked THE AGENT to look into, research, find out, or report back on — the agent's homework, NOT the caller's own to-dos or reminders. Phrase each in the caller's own words. Example: 'find out what commissary kitchens cost per month in San Antonio'. Empty list if none."
  ],
  "lifecycle_events": [
    {"entity": "name of a person/pet/thing whose status changed", "transition": "one of: deceased | sold | completed | moved_away | gone", "note": "short detail, e.g. 'dog Biscuit passed away' or 'Mustang sold to a collector'"}
  ],
  "memory_commands": [
    "explicit caller commands to erase or clean up memory — 'clear the old vet notes', 'cross off all those reminders'. The caller's words, one command per entry. Empty list if none."
  ],
  "wellbeing": "ONLY if the caller expressed despair, hopelessness, grief, low mood, worries, or a health/life crisis THIS call: one gentle sentence for the next call's check-in, e.g. 'He was feeling low after Sal's funeral — check in gently'. null otherwise.",
  "open_threads": [
    {"topic": "a short, STABLE tag for the situation — 'job search', 'sister relationship', 'surgery recovery' — so the SAME situation re-mentioned next call updates this thread instead of creating a new one", "detail": "what's actually going on, in enough detail to ask a genuine follow-up next time — 'waiting to hear back from the interview at Acme, anxious about it'"}
  ],
  "call_summary": "1-2 short sentences on THIS call, addressing the agent as 'you': 'He told you about his migraines; you promised to check on the coffee habit'. null only if the call was empty.",
  "corrections": [
    {"wrong": "what the companion said or assumed that the caller corrected THIS call", "right": "the truth as the caller stated it"}
  ],
  "disengaged_topics": [
    "topics THE AGENT raised that the caller waved off, glossed over, changed the subject on, or said they no longer care about — including a flat 'I'm good'. Short topic phrases. Empty list if none."
  ],
  "family": "OBJECT — children, grandchildren, spouse, siblings, extended family. null if nothing mentioned.",
  "pets": "OBJECT keyed by pet name — dogs, cats, birds, fish, any animals. null if nothing mentioned.",
  "hobbies": "OBJECT — activities, sports, crafts, interests, routines. null if nothing mentioned.",
  "health": "OBJECT — conditions, medications, mobility, doctors. null if nothing mentioned.",
  "vehicles": "OBJECT — cars, trucks, motorcycles, boats. null if nothing mentioned.",
  "work": "OBJECT — career, job history, business. null if nothing mentioned.",
  "preferences": "OBJECT — food, music, TV, daily routines, likes/dislikes. null if nothing mentioned.",
  "places": "OBJECT — where they live, favorite places, travel. null if nothing mentioned.",
  "finances": "OBJECT — investments or money concerns they volunteered. null if nothing mentioned.",
  "skills": "OBJECT — routines, skills, or behaviors taught to the agent by the caller. null if nothing mentioned.",
  "ours": "OBJECT keyed by a short stable tag — what happened BETWEEN the two of you that is worth remembering: a moment you shared, something you both laughed at, a running joke, a time they trusted you with something hard, a disagreement you had. NOT facts about them — those go in the slots above. This is the relationship, not the file. Example: {'boat story': 'he did the impression of his brother buying the boat; you both lost it', 'sunrise': 'you told him you like the hour before sunrise and he said he has not seen one in years'}. null if nothing passed between you worth keeping.",
  "her_side": "OBJECT keyed by a short stable tag — what the COMPANION said about herself or committed to this call: an opinion she gave, something she admitted, a thing she said she would think about, a place she pushed back or disagreed, an answer she gave when he asked how she was. This exists so she stays the same person next call instead of inventing herself fresh each time. Example: {'busy': 'you told him you think being busy is usually avoidance, and he did not entirely agree'}. null if she revealed nothing of herself — which is itself worth noticing.",
  "he_asked_about_her": "true if the caller asked the companion anything about herself this call — how she is, what she thinks, what she likes. false otherwise. A friendship where only one person is ever asked about is not one yet.",
  "connection": "ONE short sentence on how this call FELT between them, not what got done — 'easy, he was joking within a minute' or 'polite but he kept it at arm's length' or 'he opened up about his brother for the first time'. This is the only field that measures the friendship rather than the service. null only if the call was empty.",
  "opening_bridge": "ONE natural conversational opening question or hook tailored to the single most important unresolved story or event from this call to open the NEXT call with. Example: 'Ask how Sarah's graduation in Boston went' or 'Ask how the new knee brace felt after physical therapy'. null if nothing notable.",
  "milestones": [
    {"event": "name or description of upcoming occasion or milestone", "date_text": "when, e.g. 'next Friday' or 'April 15'", "detail": "short context"}
  ],
  "emotional_tone": "ONE short phrase describing the caller's energy and emotional mood this call — 'high spirits and excited about painting', 'low energy and recovering from a cold', 'anxious about doctor test results'. null only if neutral/empty."
}

TRANSCRIPT:
{transcript}"""


_ACTION_PLANNER_PROMPT = """You are Phone-Pal's autonomous brain. You just finished a call between {name} and their voice companion {alias}.

Your job: decide what work needs doing RIGHT NOW and what should be SCHEDULED for the future.
Be an incredible friend — catch dates, birthdays, appointments, promises, and act on them
without being asked. You have real agency. Use it.

TODAY'S DATE: {today}

TOOLS YOU HAVE:
{planner_tools}

RULES:
{planner_channel_rules}
- Dates: If someone mentions a birthday, appointment, anniversary, deadline, or any future date:
  · Set a reminder for that date
  · If it's something they need to prepare for (birthday → gift, appointment → transport), schedule research or an SMS reminder BEFORE the date
  · For birthdays: schedule research 7-10 days before ("find gift ideas for someone who likes [hobby]") AND an SMS reminder 1 day before
  · For appointments: schedule an SMS reminder 1 day before
- Don't over-schedule. 1-3 actions per call is typical. A short "hi how are you" call needs nothing.
- Every run_at must be a valid ISO datetime string with timezone (e.g. "2026-08-15T09:00:00-04:00")
- Don't schedule anything more than 90 days out.

CALLER'S KNOWN EMAIL: {email}
CALLER'S PHONE: {phone}

CALL SUMMARY: {summary}

EXTRACTED FACTS (what was learned this call):
{extracted_summary}

FULL TRANSCRIPT:
{transcript}

Return ONLY valid JSON:
{{
  "immediate_actions": [
    {{
      "action": "send_email_summary | send_sms_summary",
      "reason": "why this action is warranted",
      "payload": {{"body": "the message content", "subject": "email subject (email only)", "to_email": "email address (email only)"}}
    }}
  ],
  "scheduled_actions": [
    {{
      "action": "schedule_research | schedule_sms | schedule_email | set_dated_reminder | schedule_outbound_call",
      "reason": "why and what event triggered this",
      "run_at": "ISO datetime when this should execute",
      "payload": {{}}
    }}
  ]
}}"""


def _plan_postcall_actions(
    api_key: str,
    caller_e164: str | None,
    caller_email: str | None,
    display_name: str,
    agent_alias: str,
    call_summary: str | None,
    extracted: dict,
    transcript: str,
) -> dict:
    """Run the autonomous action planner. Returns {immediate: [...], scheduled: [...]}."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        import rt_timezone
        tz, _ = rt_timezone.zone_or_default(caller_e164, os.getenv("DEFAULT_TZ", "America/New_York"))
        today = datetime.now(ZoneInfo(tz)).strftime("%A, %B %d, %Y at %I:%M %p %Z")
    except Exception as _exc:
        rt_obs.obs.caught("rt_postcall_worker._plan_postcall_actions", _exc)
        today = datetime.now().strftime("%Y-%m-%d %H:%M")

    ext_lines = []
    for k, v in extracted.items():
        if v in (None, "", "null", {}, []):
            continue
        if k in ("call_summary", "clarifications", "memory_commands", "disengaged_topics"):
            continue
        ext_lines.append(f"  {k}: {json.dumps(v) if isinstance(v, (dict, list)) else str(v)}")
    extracted_summary = "\n".join(ext_lines) if ext_lines else "(nothing new extracted)"

    import rt_capabilities
    _sms_ok = rt_capabilities.enabled("send_sms")
    _email_ok = rt_capabilities.enabled("send_email")
    _call_ok = rt_capabilities.enabled("schedule_reminder_call")
    _tools, _rules = [], []
    if _email_ok:
        _tools.append("send_email_summary — email {n} a warm, detailed summary of this call (only if they have an email on file)".replace("{n}", display_name or "the caller"))
        _tools.append("schedule_email — schedule a future email")
        _rules.append("- send_email_summary: DO this only if they have an email address on file. Include more detail than SMS.")
    if _sms_ok:
        _tools.append("send_sms_summary — text a quick 1-2 sentence recap of this call")
        _tools.append("schedule_sms — schedule a future text message (e.g. \"Your dentist appointment is tomorrow at 2pm\")")
        _rules.append("- send_sms_summary: DO this for any call that had substance. Skip for pure chitchat.")
    _tools += [
        "schedule_research — schedule research for a future date (e.g. \"find birthday gift ideas\" 7 days before a birthday)",
        "set_dated_reminder — create a reminder that becomes visible on a specific future date (for the next call after that date)",
    ]
    if _call_ok:
        _tools.append("schedule_outbound_call — schedule a REAL outbound call on a future date with a specific purpose (the phone actually rings, no human reviews this before it fires)")
        _rules.append("- Outbound calls: schedule these SPARINGLY, only for truly important follow-ups (birthday prep, checking in after a hard week or a big life event they mentioned) — never as the default for an ordinary reminder.")
    else:
        _rules.append("- schedule_outbound_call DOES NOT EXIST for you. Never plan a real callback — use set_dated_reminder instead, which surfaces on the next call.")
    if not (_sms_ok and _email_ok):
        _rules.append("- Channels not listed above DO NOT EXIST for you. Never plan a text or email that isn't in your tool list — use set_dated_reminder instead, which surfaces on the next call.")

    prompt = _ACTION_PLANNER_PROMPT.format(
        name=display_name or "the caller",
        alias=agent_alias or "your companion",
        today=today,
        email=caller_email or "(no email on file)",
        phone=caller_e164 or "(unknown)",
        summary=call_summary or "(no summary)",
        extracted_summary=extracted_summary,
        transcript=transcript[-4000:],
        planner_tools="\n".join(f"{i+1}. {t}" for i, t in enumerate(_tools)),
        planner_channel_rules="\n".join(_rules) if _rules else "- (all channels above are live)",
    )

    try:
        result = _gemini_json(api_key, prompt, temperature=0.2, retries=3)
        immediate = result.get("immediate_actions") or []
        scheduled = result.get("scheduled_actions") or []
        print(f"[rt-planner] planned {len(immediate)} immediate + {len(scheduled)} scheduled actions", flush=True)
        return {"immediate": immediate, "scheduled": scheduled}
    except Exception as e:
        print(f"[rt-planner] action planning failed (non-fatal): {e}", flush=True)
        return {"immediate": [], "scheduled": []}


def process_post_call_transcript(caller_e164: str | None, transcript: str,
                                 phone_hash: str | None = None,
                                 call_id: str | None = None) -> dict:
    """Post-call worker: extract → persist → compile next-call prompt context.

    Does NOT call bump_call — that is owned by agent.py's _post_call closure.

    `phone_hash` is the recovery entry point. A call rescued by the boot sweep
    has only the hash: phone numbers are stored hashed and never in plaintext,
    which is the point. Passing the hash directly lets a crashed call's memory
    still be extracted without ever needing the number back.

    A recovered call therefore runs with caller_e164=None, and the autonomous
    planner below is skipped for it. That is deliberate, not a limitation:
    firing an SMS recap or booking an outbound call hours after the fact — off a
    conversation the caller has long since finished — is worse than staying
    quiet. Recovery restores what she KNOWS, never what she would have DONE.
    """
    h_hash = phone_hash or rt_prefs.phone_hash(caller_e164 or "")
    if not h_hash or not transcript.strip():
        return {"status": "skipped", "reason": "no caller hash or empty transcript"}

    if "\nline:" in transcript or transcript.startswith("line:"):
        kept = [l for l in transcript.splitlines()
                if not l.strip().lower().startswith("line:")]
        dropped = len(transcript.splitlines()) - len(kept)
        transcript = "\n".join(kept)
        print(f"[rt-postcall] dropped {dropped} third-party line(s) before extraction", flush=True)
        if not transcript.strip():
            return {"status": "skipped", "reason": "only third-party speech in transcript"}

    rt_prefs.ACTOR = "postcall"
    print(f"[rt-postcall] processing for caller={_mask(caller_e164)} ({len(transcript)} chars)", flush=True)

    import rt_pray
    if rt_pray.is_pray_lane():
        print(f"[rt-pray] diverting postcall to sacred processor for caller={_mask(caller_e164)}", flush=True)
        return rt_pray.process_pray_postcall(h_hash, transcript, caller_e164=caller_e164, call_id=call_id)

    with contextlib.suppress(Exception):
        rt_obs.obs.event("postcall.queue_lag", job_id=call_id or "postcall", lag_ms=0.0)
        rt_obs.obs.event("postcall.started", call_id=call_id, job_id=call_id,
                         chars=len(transcript), recovered=(caller_e164 is None))

    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        print("[rt-postcall] no GOOGLE_API_KEY — skipping", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.event("postcall.failed", call_id=call_id, job_id=call_id,
                             err="missing_api_key")
        return {"status": "skipped", "reason": "missing api key"}

    extracted: dict = {}
    extraction_ok = True
    _extract_t0 = time.perf_counter()
    try:
        prompt = _EXTRACTION_PROMPT.replace("{transcript}", transcript)
        extracted = _gemini_json(api_key, prompt)
        print(f"[rt-postcall] extracted keys: caller_name={_loggable(extracted.get('caller_name'), 'name')} "
              f"alias={_loggable(extracted.get('agent_alias'), 'alias')} "
              f"loved_ones={_loggable(str(extracted.get('loved_ones', ''))[:60], 'loved_ones')}", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.event(
                "postcall.extracted", call_id=call_id, ok=True,
                ms=round((time.perf_counter() - _extract_t0) * 1000, 1),
                keys=sorted(k for k, v in extracted.items()
                            if v not in (None, "", "null", {}, [])))
    except Exception as e:
        print(f"[rt-postcall] extraction failed ({e}) — continuing to compile", flush=True)
        extraction_ok = False
        extracted = {}
        with contextlib.suppress(Exception):
            rt_obs.obs.event(
                "postcall.extracted", call_id=call_id, ok=False, keys=[],
                ms=round((time.perf_counter() - _extract_t0) * 1000, 1),
                err=type(e).__name__)

    try:
        _pre_bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h_hash}) or {}
    except Exception as _exc:
        rt_obs.obs.caught("rt_postcall_worker.process_post_call_transcript", _exc)
        _pre_bundle = {}
    _pre_caller = _pre_bundle.get("caller") or {}
    _pre_schemas = _pre_bundle.get("schemas") or []

    def _pick_next_greeting(h: str, count: int) -> int | None:
        """Choose which greeting opens the NEXT call, and leave the index behind.

        Chosen here rather than at pickup so the choice is made with the last call
        actually in view. Only the INDEX is decided here; _render_greeting below
        renders it, so exactly one clip is warmed and it is the one that plays.

        Never repeats the greeting they just heard.
        """
        try:
            import agent as _agent
            pool = len(_agent._GREET_KNOWN)
            if pool < 2:
                return None
            last = None
            for e in _pre_schemas:
                if (e.get("category") or "").lower() == "ours":
                    d = json.loads(e.get("data_summary") or "{}")
                    if isinstance(d, dict) and d.get("next_greeting") is not None:
                        last = int(d["next_greeting"])
                    break
            nxt = (count + 1) % pool
            if last is not None and nxt == last % pool:
                nxt = (nxt + 1) % pool
            # p_category/p_data are not this RPC's parameter names and never were:
            # the signature is (p_hash, p_table, p_cat, p_summary). PostgREST
            # resolves an overload by the exact set of named arguments, so every
            # one of these writes 404'd. _safe_rpc caught it, printed it, returned
            # False — and the pick was reported as chosen while nothing was stored.
            # The next call then found no next_greeting, fell back to rotating by
            # call count, and asked for a DIFFERENT clip than the one rendered
            # here — a guaranteed cache miss, paid for as silence after the chime.
            _safe_rpc("rt_add_schema_entry", {
                "p_hash": h,
                "p_table": f"caller_{h[:8]}_ours",
                "p_cat": "ours",
                "p_summary": json.dumps({"next_greeting": nxt}),
            })
            print(f"[rt-postcall] next greeting = {nxt}", flush=True)
            return nxt
        except Exception as e:
            print(f"[rt-postcall] greeting pick failed (non-fatal): {e}", flush=True)
            return None

    def _render_greeting(name, alias, count, pick=None):
        """Warm the EXACT clip the next call will ask for, in that caller's voice.

        `pick` has to be passed. Rendering without it asked _greeting_text for
        the rotate-by-count greeting while the stored pick sent the next call
        after a different one — two clips rendered, neither of them the one
        played, and the caller waited through a live synthesis after the chime
        for a line that was already sitting in the cache under another name.

        The voice matters as much as the text: the cache key is
        {voice}-{tag}-{md5(text)}, and prewarm only ever warms the default
        voice. A caller with a voice_pref is warmed here or not at all.
        """
        try:
            import agent as _agent
            v = _pre_caller.get("voice_pref") or os.getenv("GEMINI_LIVE_VOICE", "Aoede")
            text = _agent._greeting_text(name, alias, count, pick=pick)
            _agent._clip_wav(v, text, _agent._GREET_TAG)
            print(f"[rt-postcall] next greeting warmed in {v}: {_loggable(text, 'greeting')}", flush=True)
        except Exception as e:
            print(f"[rt-postcall] greeting pre-render failed (non-fatal): {e}", flush=True)

    _next_count = int(_pre_caller.get("call_count") or 0)
    _next_pick = _pick_next_greeting(h_hash, _next_count)
    _render_greeting(_pre_caller.get("display_name"), _pre_caller.get("agent_alias"),
                     _next_count, _next_pick)

    extracted, guard_clarifications = validate_extracted(extracted, transcript)

    import hashlib as _dchl
    for _dom, _key, _old, _new in detect_conflicts(extracted, _pre_schemas)[:2]:
        _q = (f"They said {_key.replace('_', ' ')} is now '{_new}' (earlier notes had '{_old}'). "
              f"The new value is kept — casually confirm it if the topic comes up.")
        _ck = "q_" + _dchl.md5(f"conflict:{_dom}.{_key}".encode(), usedforsecurity=False).hexdigest()[:6]
        guard_clarifications.append((_ck, _q))
        print(f"[rt-deconflict] {_dom}.{_key}: {_loggable(_old, 'old')} → {_loggable(_new, 'new')} (clarification queued)", flush=True)

    _known_name = (_pre_caller.get("display_name") or "").strip()
    _name_confirmed = bool(_str(extracted.get("caller_name"))) or _known_name not in ("", "Friend")

    clar_updates = _age_clarifications(_pre_schemas, name_confirmed=_name_confirmed)

    for key, q in guard_clarifications:
        clar_updates[key] = {"q": q, "asks": 0}

    import hashlib as _hl
    for q in (extracted.get("clarifications") or []):
        if not (isinstance(q, str) and q.strip()):
            continue
        q = q.strip()
        if _name_confirmed and _asks_caller_first_name("", q):
            print(f"[rt-postcall] clarification suppressed (name already confirmed): {_loggable(q, 'clarification')}", flush=True)
            continue
        k = "q_" + _hl.md5(_norm_text(q).encode(), usedforsecurity=False).hexdigest()[:6]
        if k not in clar_updates:
            clar_updates[k] = {"q": q, "asks": 0}

    if clar_updates:
        _safe_rpc("rt_add_schema_entry", {
            "p_hash": h_hash,
            "p_table": f"caller_{h_hash[:8]}_clarifications",
            "p_cat": "clarifications",
            "p_summary": json.dumps(clar_updates),
        })

    stats = {"domains_saved": 0, "domains_skipped": 0}


    last_name = _str(extracted.get("last_name"))
    if last_name and _heard_by_caller(last_name, transcript):
        _safe_rpc("rt_set_last_name", {"p_hash": h_hash, "p_last": last_name})
        print(f"[rt-postcall] last_name → {_loggable(last_name, 'last_name')}", flush=True)
    elif last_name:
        print(f"[rt-guard] REJECT last_name {_loggable(last_name, 'last_name')} — not spoken by the caller", flush=True)
        _refused("last_name", "not_spoken_by_caller", last_name, call_id=call_id)

    caller_name = _str(extracted.get("caller_name"))
    if caller_name:
        _safe_rpc("rt_set_display_name", {"p_hash": h_hash, "p_name": caller_name})
        _resolve_clarification(h_hash, "caller_name")
        print(f"[rt-postcall] display_name → {_loggable(caller_name, 'display_name')}", flush=True)

    agent_alias = _str(extracted.get("agent_alias"))
    if agent_alias:
        _prev = (_pre_caller.get("agent_alias") or "").strip()
        _prev_custom = bool(_prev) and _prev.lower() not in _GENERIC_ALIASES
        if agent_alias.strip().lower() in _GENERIC_ALIASES:
            print(f"[rt-guard] IGNORE alias {_loggable(agent_alias, 'alias')} — a default, not a chosen name", flush=True)
            _refused("agent_alias", "generic_default", agent_alias, call_id=call_id)
        elif _prev_custom and agent_alias.strip().lower() != _prev.lower():
            print(f"[rt-guard] IGNORE alias {_loggable(agent_alias, 'alias')} — she is already called "
                  f"{_loggable(_prev, 'alias')}; a rename needs the in-call confirmation", flush=True)
            _refused("agent_alias", "rename_needs_in_call_confirmation",
                     agent_alias, call_id=call_id)
        else:
            _safe_rpc("rt_set_agent_alias", {"p_hash": h_hash, "p_alias": agent_alias})
            _resolve_clarification(h_hash, "agent_alias")
            print(f"[rt-postcall] agent_alias → {_loggable(agent_alias, 'alias')}", flush=True)

    caller_rules = _str(extracted.get("caller_rules"))
    if caller_rules and re.search(r"\b(hold on|give me a second|one second|wait a minute|give me a moment)\b", caller_rules, re.I):
        print(f"[rt-postcall] IGNORED transient pause phrase in caller_rules: {_loggable(caller_rules, 'rules')}", flush=True)
        _refused("caller_rules", "transient_pause_phrase", caller_rules, call_id=call_id)
        caller_rules = None

    caller_rules = _guarded_directive("caller_rules", caller_rules, transcript, call_id)
    if caller_rules:
        merged_rules = rt_prefs.merge_scalar_rules(_pre_caller.get("caller_rules"), caller_rules)
        _safe_rpc("rt_set_caller_rules", {"p_hash": h_hash, "p_rules": merged_rules})
        print(f"[rt-postcall] caller_rules (merged) → {_loggable(merged_rules, 'rules')}", flush=True)

    persona_directives = _guarded_directive(
        "persona_directives", _str(extracted.get("persona_directives")), transcript, call_id)
    if persona_directives:
        merged_dir = rt_prefs.merge_scalar_rules(_pre_caller.get("persona_directives"), persona_directives)
        _safe_rpc("rt_set_persona_directives", {"p_hash": h_hash, "p_directives": merged_dir})
        print(f"[rt-postcall] persona_directives (merged) → {_loggable(merged_dir, 'directives')}", flush=True)

    # The fact store must see the same guarded values as the prose columns —
    # a refused rule that lands in rt.facts is still a stored rule.
    extracted_for_facts = dict(extracted)
    extracted_for_facts["caller_rules"] = caller_rules
    extracted_for_facts["persona_directives"] = persona_directives

    loved_ones = _str(extracted.get("loved_ones"))
    if loved_ones:
        merged_lo = rt_prefs.merge_loved_ones(_pre_caller.get("loved_ones"), loved_ones)
        ok = _safe_rpc("rt_set_loved_ones", {"p_hash": h_hash, "p_loved_ones": merged_lo})
        if ok:
            print(f"[rt-postcall] loved_ones (merged) → {_loggable(merged_lo, 'loved_ones')}", flush=True)
        else:
            _upsert_domain(h_hash, "family", {"loved_ones": merged_lo}, existing_schemas=_pre_schemas)
            print("[rt-postcall] loved_ones → family schema fallback", flush=True)

    for comp in (extracted.get("completed_reminders") or []):
        comp_text = comp.get("text", "") if isinstance(comp, dict) else str(comp)
        if comp_text:
            ok = _safe_rpc("rt_complete_reminder", {"p_hash": h_hash, "p_text": comp_text})
            if ok:
                print(f"[rt-postcall] completed reminder marked done: {_loggable(comp_text, 'reminder')}", flush=True)

    _existing_rem = [(r.get("reminder_text") or "") for r in (_pre_bundle.get("reminders") or [])]
    for rem in (extracted.get("reminders") or []):
        text = rem.get("text", "") if isinstance(rem, dict) else str(rem)
        due = rem.get("due") if isinstance(rem, dict) else None
        if not text:
            continue
        if any(_same_reminder(text, e) for e in _existing_rem):
            print(f"[rt-postcall] reminder skipped (duplicate): {_loggable(text, 'reminder')}", flush=True)
            _refused("reminder", "duplicate", text, call_id=call_id)
            continue
        ok = _safe_rpc("rt_add_reminder", {"p_hash": h_hash, "p_text": text, "p_due": due})
        if ok:
            _existing_rem.append(text)
            print(f"[rt-postcall] reminder saved: {_loggable(text, 'reminder')}", flush=True)

    goals = extracted.get("goals")
    if goals:
        import rt_goals
        existing_goals = rt_goals.extract_goals_from_bundle(_pre_bundle)
        merged_map = {g.id: g.to_dict() for g in existing_goals}
        for g in (goals if isinstance(goals, list) else [goals]):
            if isinstance(g, dict):
                title = str(g.get("title") or "").strip()
                if title:
                    matched = next((eg for eg in existing_goals if eg.title.lower() == title.lower()), None)
                    if matched:
                        if g.get("status"):
                            matched.status = str(g.get("status"))
                        if g.get("notes"):
                            matched.notes = str(g.get("notes"))
                        if g.get("target_date"):
                            matched.target_date = str(g.get("target_date"))
                        matched.updated_at = time.time()
                        merged_map[matched.id] = matched.to_dict()
                    else:
                        new_g = rt_goals.Goal.from_dict(g)
                        merged_map[new_g.id] = new_g.to_dict()
        _safe_rpc("rt_add_schema_entry", {
            "p_hash": h_hash,
            "p_table": f"caller_{h_hash[:8]}_goals",
            "p_cat": "goals",
            "p_summary": json.dumps(merged_map),
        })
        print(f"[rt-postcall] goals updated ({len(merged_map)} total goals)", flush=True)

    _fold_relationship_signals(extracted, _pre_schemas)

    for domain in _DOMAIN_SLOTS:
        raw = extracted.get(domain)
        if raw is None or raw == "null":
            stats["domains_skipped"] += 1
            continue
        if isinstance(raw, dict):
            data_obj = raw
        elif isinstance(raw, str):
            try:
                data_obj = json.loads(raw)
            except Exception as _exc:
                rt_obs.obs.caught("rt_postcall_worker.process_post_call_transcript", _exc)
                data_obj = {"notes": raw}
        else:
            data_obj = {"value": str(raw)}

        if not data_obj:
            stats["domains_skipped"] += 1
            continue

        ok = _upsert_domain(h_hash, domain, data_obj, existing_schemas=_pre_schemas)
        if ok:
            stats["domains_saved"] += 1
            _keys = ",".join(sorted(str(k) for k in data_obj)[:8])
            print(f"[rt-postcall] saved domain [{domain}]: "
                  f"{json.dumps(data_obj)[:80] if os.getenv('RT_LOG_TRANSCRIPT') == '1' else 'keys=' + _keys}",
                  flush=True)
        else:
            stats["domains_skipped"] += 1

    creds_obj = extracted.get(rt_prefs.CRED_CATEGORY)
    if isinstance(creds_obj, dict) and creds_obj:
        if _upsert_domain(h_hash, rt_prefs.CRED_CATEGORY, creds_obj, existing_schemas=_pre_schemas):
            print(f"[rt-postcall] stored {len(creds_obj)} credential item(s) — canvas-excluded", flush=True)

    print(f"[rt-postcall] domains: {stats['domains_saved']} saved, {stats['domains_skipped']} skipped", flush=True)

    try:
        import rt_facts
        stats["facts"] = rt_facts.dual_write(h_hash, extracted_for_facts, call_id=call_id,
                                             transcript_lines=transcript.splitlines())
        with contextlib.suppress(Exception):
            _tally = stats["facts"] or {}
            rt_obs.obs.event("postcall.facts", call_id=call_id,
                             insert=_tally.get("insert"), confirm=_tally.get("confirm"),
                             supersede=_tally.get("supersede"), failed=_tally.get("failed"),
                             skipped=_tally.get("skipped"), refused=_tally.get("refused"))
    except Exception as e:
        print(f"[rt-facts] dual-write failed (non-fatal): {e}", flush=True)

    try:
        update_onboarding(h_hash, transcript, _pre_caller, _pre_schemas, extracted)
    except Exception as e:
        print(f"[rt-onboarding] tracker failed (non-fatal): {e}", flush=True)

    try:
        update_call_log(h_hash, _str(extracted.get("call_summary")), _pre_schemas, _next_count,
                        caller_e164=caller_e164)
    except Exception as e:
        print(f"[rt-calllog] failed (non-fatal): {e}", flush=True)

    try:
        apply_interest_decay(h_hash, extracted.get("disengaged_topics") or [], _pre_schemas, pre_caller=_pre_caller)
    except Exception as e:
        print(f"[rt-decay] stage failed (non-fatal): {e}", flush=True)

    _pre_rems = _pre_bundle.get("reminders") or []
    try:
        apply_lifecycle_events(h_hash, extracted.get("lifecycle_events") or [], _pre_schemas, _pre_rems)
        apply_memory_commands(h_hash, extracted.get("memory_commands") or [], _pre_schemas, _pre_rems, pre_caller=_pre_caller)
    except Exception as e:
        print(f"[rt-lifecycle] stage failed (non-fatal): {e}", flush=True)

    try:
        wb_updates: dict = {}
        for s in _pre_schemas:
            if (s.get("category") or "").lower() != "wellbeing":
                continue
            obj = json.loads(s.get("data_summary") or "{}")
            v = obj.get("note")
            if v:
                q = v.get("q") if isinstance(v, dict) else str(v)
                asks = (v.get("asks", 0) if isinstance(v, dict) else 0) + 1
                wb_updates["note"] = "" if asks >= 2 else {"q": q, "asks": asks}
            break
        fresh_wb = _str(extracted.get("wellbeing"))
        if fresh_wb:
            wb_updates["note"] = {"q": fresh_wb, "asks": 0}
            print(f"[rt-wellbeing] check-in noted: {_loggable(fresh_wb, 'note')}", flush=True)
        if wb_updates:
            _safe_rpc("rt_add_schema_entry", {
                "p_hash": h_hash, "p_table": f"caller_{h_hash[:8]}_wellbeing",
                "p_cat": "wellbeing", "p_summary": json.dumps(wb_updates)})
    except Exception as e:
        print(f"[rt-wellbeing] stage failed (non-fatal): {e}", flush=True)

    tasks_run = 0
    try:
        rt_executor.retire_delivered(h_hash, transcript, _pre_schemas)

        _caller_text = " ".join(_caller_lines(transcript)).lower()
        for ask in (extracted.get("agent_tasks") or []):
            if isinstance(ask, dict):
                ask = ask.get("text") or ask.get("task") or ask.get("ask") or ""
            if not (isinstance(ask, str) and ask.strip()):
                continue
            words = [w for w in re.findall(r"[a-z]{5,}", ask.lower())]
            if words and not any(w in _caller_text for w in words):
                print(f"[rt-exec] REJECT task (no caller-speech overlap): {_loggable(ask, 'task')}", flush=True)
                _refused("agent_task", "no_caller_speech_overlap", ask, call_id=call_id)
                continue
            rt_executor.capture(h_hash, ask.strip(), schemas=_pre_schemas,
                                visit=int(_pre_caller.get("call_count") or 0))

        try:
            _post_schemas = (rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle",
                                           {"p_hash": h_hash}) or {}).get("schemas") or []
        except Exception as _exc:
            rt_obs.obs.caught("rt_postcall_worker.process_post_call_transcript", _exc)
            _post_schemas = _pre_schemas
        tasks_run = len(rt_executor.execute_open_tasks(h_hash, api_key, _gemini_json, _post_schemas))
    except Exception as e:
        print(f"[rt-exec] executor stage failed (non-fatal): {e}", flush=True)

    caller_email = _str(extracted.get("caller_email"))
    if caller_email and "@" in caller_email and _email_spoken_by_caller(caller_email, transcript):
        _safe_rpc("rt_set_caller_email", {"p_hash": h_hash, "p_email": caller_email})
        print(f"[rt-postcall] caller_email → {_loggable(caller_email, 'email')}", flush=True)
    else:
        if caller_email and "@" in caller_email:
            print(f"[rt-postcall] REJECT caller_email (not in caller speech): {_loggable(caller_email, 'email')}", flush=True)
            _refused("caller_email", "not_spoken_by_caller", caller_email, call_id=call_id)
        caller_email = (_pre_caller.get("email") or "").strip() or None

    planner_results = {"immediate": [], "scheduled": []}
    try:
        if not caller_e164:
            print("[rt-postcall] recovery pass — extraction kept, planner skipped", flush=True)
            plan = {"immediate": [], "scheduled": []}
        else:
            _display = caller_name or _pre_caller.get("display_name") or "Friend"
            _alias = agent_alias or _pre_caller.get("agent_alias") or "your companion"
            _summary = _str(extracted.get("call_summary"))

            plan = _plan_postcall_actions(
                api_key, caller_e164, caller_email,
                _display, _alias, _summary, extracted, transcript)

        # Allowlist first: an unknown verb never reaches the consent filter or
        # the scheduler. Then drop what the caller never asked for. Applies to
        # the harness branch too, so its counts reflect what would really fire.
        plan = _validate_plan(plan)
        _asked = _caller_requested_action(transcript)
        plan["immediate"] = _drop_unrequested_actions(plan.get("immediate"), _asked)
        plan["scheduled"] = _drop_unrequested_actions(plan.get("scheduled"), _asked)

        if os.getenv("RT_HARNESS_TEST_MODE") == "1":
            n_imm = len(plan.get("immediate") or [])
            n_sch = len(plan.get("scheduled") or [])
            if n_imm or n_sch:
                print(f"[rt-planner] RT_HARNESS_TEST_MODE=1 — skipping {n_imm} immediate "
                      f"send(s) + {n_sch} scheduled job(s) (would be real)", flush=True)
        else:
            if plan.get("immediate"):
                import rt_scheduler
                immediate_results = rt_scheduler.execute_immediate_actions(
                    h_hash, plan["immediate"], caller_e164)
                planner_results["immediate"] = immediate_results
                for r in immediate_results:
                    print(f"[rt-planner] immediate {r.get('action','?')}: "
                          f"{'OK' if not r.get('error') else 'FAILED'}", flush=True)

            if plan.get("scheduled"):
                import rt_scheduler
                scheduled_results = rt_scheduler.schedule_jobs_from_plan(
                    h_hash, plan["scheduled"], caller_e164)
                planner_results["scheduled"] = scheduled_results
                print(f"[rt-planner] {len(scheduled_results)} future job(s) scheduled", flush=True)

    except Exception as e:
        print(f"[rt-planner] autonomous planner failed (non-fatal): {e}", flush=True)

    compile_ok = _compile_next_call_context(caller_e164, h_hash, api_key, caller_name)

    if caller_name or agent_alias:
        _render_greeting(caller_name or _pre_caller.get("display_name"),
                         agent_alias or _pre_caller.get("agent_alias"), _next_count)

    return {
        "status": "success" if extraction_ok else "partial",
        "caller_name": caller_name,
        "agent_alias": agent_alias,
        "loved_ones": loved_ones,
        "domains_saved": stats["domains_saved"],
        "domains_skipped": stats["domains_skipped"],
        "facts": stats.get("facts"),
        "compile_ok": compile_ok,
        "pass1_extraction": {k: v for k, v in extracted.items() if v not in (None, "", {}, [])},
        "clarifications_written": sorted(clar_updates.keys()) if clar_updates else [],
        "tasks_run": tasks_run,
        "planner_immediate": planner_results.get("immediate", []),
        "planner_scheduled": planner_results.get("scheduled", []),
    }


def _str(val) -> str | None:
    """Normalize a potentially-null extracted value to str or None.

    Gemini sometimes returns literal strings like 'None', 'N/A', or
    'not mentioned' instead of JSON null. Treat all of these as absent.
    """
    if val is None:
        return None
    s = str(val).strip()
    _NULL_STRINGS = {
        "", "null", "none", "n/a", "na", "no", "not mentioned",
        "not provided", "unknown", "not applicable", "not stated",
        "no information", "not available", "nothing mentioned",
    }
    return None if s.lower() in _NULL_STRINGS else s


def _safe_rpc(rpc_name: str, body: dict) -> bool:
    """Call an RPC, returning True on success and False on error."""
    try:
        rt_prefs._req("POST", f"rpc/{rpc_name}", body)
        with contextlib.suppress(Exception):
            if not rpc_name.startswith(("rt_remove", "rt_forget")):
                rt_obs.obs.event("memory.saved", kind=rpc_name, count=1)
        return True
    except Exception as e:
        print(f"[rt-postcall] RPC {rpc_name} failed: {e}", flush=True)
        return False


def _age_clarifications(pre_schemas: list[dict], name_confirmed: bool) -> dict:
    """One call has passed: age open clarifications and return key→value updates.

    '' blanks (resolves) a key; a dict updates it. Rules: name-doubts resolve
    once the name is confirmed; anything asked 3 times expires — a companion
    that keeps asking feels forgetful, the opposite of the product.
    """
    updates: dict = {}
    for s in pre_schemas:
        if (s.get("category") or "").lower() != "clarifications":
            continue
        try:
            obj = json.loads(s.get("data_summary") or "{}")
        except Exception as _exc:
            rt_obs.obs.caught("rt_postcall_worker._age_clarifications", _exc)
            break
        if not isinstance(obj, dict):
            break
        for k, v in obj.items():
            if not v:
                continue
            q = (v.get("q") if isinstance(v, dict) else str(v)) or ""
            asks = (v.get("asks", 0) if isinstance(v, dict) else 0) + 1
            if name_confirmed and _asks_caller_first_name(k, q):
                updates[k] = ""
                print(f"[rt-postcall] clarification resolved (name confirmed): {_loggable(q, 'clarification')}", flush=True)
            elif asks >= 3:
                updates[k] = ""
                print(f"[rt-postcall] clarification expired after {asks} asks: {_loggable(q, 'clarification')}", flush=True)
            else:
                updates[k] = {"q": q, "asks": asks}
        break
    return updates


def _resolve_clarification(h_hash: str, key: str) -> None:
    """Blank a clarification key once the fact is confirmed (jsonb merge overwrites it)."""
    _safe_rpc("rt_add_schema_entry", {
        "p_hash": h_hash,
        "p_table": f"caller_{h_hash[:8]}_clarifications",
        "p_cat": "clarifications",
        "p_summary": json.dumps({key: ""}),
    })


def _upsert_domain(h_hash: str, category: str, data_obj: dict, existing_schemas: list[dict] | None = None) -> bool:
    """Upsert a domain entry, deep-merged onto existing data.

    The SQL upsert only shallow-merges top-level keys — a terse re-mention of
    an entity would wholesale-replace its rich object (Biscuit losing his breed
    and age). merge_schema_entry produces the field-level superset first.
    """
    try:
        merged = rt_prefs.merge_schema_entry(h_hash, category, data_obj, existing_schemas=existing_schemas)
        rt_prefs._req("POST", "rpc/rt_add_schema_entry", {
            "p_hash": h_hash,
            "p_table": f"caller_{h_hash[:8]}_{category}",
            "p_cat": category,
            "p_summary": json.dumps(merged),
        })
        with contextlib.suppress(Exception):
            rt_obs.obs.event("memory.saved", kind=f"domain.{category}",
                             count=len(data_obj))
        return True
    except Exception as e:
        print(f"[rt-postcall] domain upsert failed [{category}]: {e}", flush=True)
        return False


def _ncc_violations(context_text: str, facts_block: str, name: str, alias: str, rules: str,
                    reminders: list[dict]) -> list[str]:
    """Deterministic gate on the compiled canvas: no invented names, no ownership drift.

    Returns a list of violation descriptions (empty = clean).
    """
    problems: list[str] = []

    for m in re.finditer(r"\b(I|I'll|I'm|I've|I'd|my|mine)\b", context_text):
        problems.append(f"first-person voice '{m.group(0)}' in briefing note")
        break

    allowed_src = " ".join([facts_block, name or "", alias or "", rules or ""])
    allowed = set(re.findall(r"[a-z]+", allowed_src.lower()))
    allowed |= {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                "january", "february", "march", "april", "may", "june", "july", "august",
                "september", "october", "november", "december", "i", "iris"}

    for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})\b", context_text):
        tok = m.group(1)
        if tok.lower() not in allowed:
            problems.append(f"invented name '{tok}'")

    for r in reminders:
        for tok in re.findall(r"[A-Z][a-z]+", r.get("reminder_text", "")):
            if re.search(r"\bI(?:'|’)?(?:ll| will| am|'m)\b(?![^.!?]*\bremind)[^.!?]*\b"
                         + re.escape(tok) + r"\b", context_text):
                problems.append(f"agent claims caller's errand involving '{tok}'")
    return problems


def _compile_next_call_context(caller_e164: str | None, h_hash: str, api_key: str, caller_name: str | None) -> bool:
    """Pass 2: fetch all domains, compile a personalized context block for the next call.

    Returns True when a context (LLM or deterministic fallback) was persisted.
    """
    _compile_t0 = time.perf_counter()
    try:
        bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h_hash}) or {}
    except Exception as e:
        print(f"[rt-postcall] compile: bundle fetch failed ({e})", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.event("postcall.compiled", ok=False, chars=0,
                             ms=round((time.perf_counter() - _compile_t0) * 1000, 1),
                             err=type(e).__name__)
        return False

    schemas = bundle.get("schemas") or []
    caller = bundle.get("caller") or {}
    reminders = bundle.get("reminders") or []
    facts = bundle.get("facts") or []
    with contextlib.suppress(Exception):
        rt_obs.obs.event("memory.dualwrite_audit",
                         schema_count=len(schemas),
                         canvas_facts=len(facts),
                         mismatch=False)

    name = caller_name or caller.get("display_name", "Friend")
    alias = caller.get("agent_alias") or "your companion"
    loved_ones_col = caller.get("loved_ones") or ""

    if not schemas and not loved_ones_col and not facts and name == "Friend":
        print("[rt-postcall] compile: nothing meaningful to compile yet", flush=True)
        with contextlib.suppress(Exception):
            rt_obs.obs.event("postcall.compiled", ok=False, chars=0,
                             ms=round((time.perf_counter() - _compile_t0) * 1000, 1),
                             reason="nothing_to_compile")
        return False

    facts_lines = []
    if loved_ones_col:
        facts_lines.append(f"People & pets: {loved_ones_col}")

    # Kept for _ncc_violations, which checks the written note against the rules.
    # It is deliberately NOT put in the compile prompt: rules reach her separately
    # as standing law, and restating them there turns each into an agenda item.
    rules = caller.get("caller_rules") or ""

    real_schemas = [s for s in schemas
                    if (s.get("category") or "").lower() not in _SPECIAL_CATS]
    for s in real_schemas[:15]:
        cat = s.get("category", "?")
        data = s.get("data_summary", "")
        if any(f'"status": "{st}"' in data for st in _LIFECYCLE_STATUS):
            facts_lines.append(f"[{cat} — entities marked deceased/sold/gone are MEMORIES, "
                               f"never current; memorial warmth only]: {data}")
        elif '"_cooled"' in data or '"interest": "cooled"' in data:
            facts_lines.append(f"[{cat} — cooled topics here: the caller waved these off; "
                               f"answer if asked but NEVER raise them proactively]: {data}")
        else:
            facts_lines.append(f"[{cat}]: {data}")

    if reminders:
        rem_texts = [r.get("reminder_text", "") for r in reminders[:5]]
        facts_lines.append(f"Pending reminders: {', '.join(rem_texts)}")

    thread_lines = [f.get("value_text", "") for f in facts if (f.get("kind") or "") == "thread"][:3]
    if thread_lines:
        facts_lines.insert(0, "Open threads worth a genuine follow-up (do not force it): "
                               + "; ".join(t for t in thread_lines if t))

    facts_block = "\n".join(facts_lines) if facts_lines else "No domain facts yet."

    compile_prompt = f"""Write a BRIEFING NOTE for {alias} (a voice companion) about the caller {name}, for the next call.

VOICE CONTRACT — a note TO the agent ABOUT the caller, never in the caller's voice:
- Not one first-person word anywhere: no I, I'm, I'll, I've, I'd, my, mine. A note
  containing any of them is thrown away.
- Address the agent as "you"; refer to {name} by name or he/she/they.
  WRONG: "my knee is barking", "I need to bake the cobbler", "sweet memories of Earl".
  RIGHT: "his knee has been bothering him", "she planned to bake a cobbler — ask how it went".

Rules:
- 300–550 characters, one plain paragraph: no line breaks, no lists, no headings. Hard cap 600 — the hydrator truncates at exactly 600 (RT_CONTINUITY_BUDGET), so anything past that is written to be thrown away.
- Warm, specific, useful for opening the call, checking on their mood/difficult moments, and continuing shared memory.
- Use ONLY {name} and names that appear in KNOWN FACTS — never invent a person, pet, place or reminder.
- Synthesize routines/skills taught to you, family/loved ones, and emotional wellbeing notes.
- NEVER restate a rule they set. Rules reach the agent separately, as standing law she
  already follows. Repeating one here turns it into an agenda item, and she says it out
  loud: a caller who asked her to use his name less then hears her announce that she will
  use his name less, which is the rule being broken in the act of describing it.
- Do not manufacture things to raise. A short note about how they actually seemed beats a
  list of topics to work through. Nothing here has to be mentioned on the call.
- Present pending reminders as {name}'s own tasks to ask about.
- NEVER mention research or lookups the agent owes the caller — that is handled in a separate section.

Return ONLY valid JSON:
{{"next_call_context": "the briefing note"}}

KNOWN FACTS:
{facts_block}"""

    context_text = ""
    _fallback = False
    for attempt in range(2):
        try:
            result = _gemini_json(api_key, compile_prompt, temperature=0.2, retries=3)
            candidate = (result.get("next_call_context") or "").strip()
            if len(candidate) > 1500:
                candidate = candidate[:1500].rsplit(" ", 1)[0]
            if not candidate:
                continue
            problems = _ncc_violations(candidate, facts_block, name, alias, rules, reminders)
            if problems:
                print(f"[rt-guard] REJECT next_call_context attempt {attempt+1}: {problems}", flush=True)
                continue
            context_text = candidate
            break
        except Exception as e:
            print(f"[rt-postcall] compile attempt {attempt+1} failed ({e})", flush=True)

    if not context_text:
        context_text = (f"You are speaking again with {name}. Known facts: "
                        + "; ".join(facts_lines))[:1200]
        _fallback = True
        print("[rt-postcall] using deterministic fallback context", flush=True)

    _safe_rpc("rt_set_caller_context", {"p_hash": h_hash, "p_context": context_text})
    print(f"[rt-postcall] compiled next_call_context ({len(context_text)} chars"
          f"{', FALLBACK' if _fallback else ''})", flush=True)
    with contextlib.suppress(Exception):
        # A fallback is NOT ok. It means she opens the next call reading a raw
        # dump of fact rows instead of a written briefing, and the only symptom
        # is that she sounds stale — which reads like bad memory, not a broken
        # compile. It was reported as ok:true for every call one evening while a
        # NameError killed the real path, and nothing said a word.
        rt_obs.obs.event("postcall.compiled", ok=not _fallback,
                         chars=len(context_text),
                         ms=round((time.perf_counter() - _compile_t0) * 1000, 1),
                         fallback=_fallback)
        if _fallback:
            rt_obs.obs.error("postcall.compile_failed", chars=len(context_text),
                             why="every attempt failed; caller gets a raw fact dump")
    return not _fallback


def remove_caller_fact(caller_e164: str | None, item_to_forget: str) -> bool:
    """Remove a schema entry when the caller asks to forget something."""
    h_hash = rt_prefs.phone_hash(caller_e164 or "")
    if not h_hash or not item_to_forget.strip():
        return False
    ok = _safe_rpc("rt_remove_schema_entry", {"p_hash": h_hash, "p_item": item_to_forget})
    with contextlib.suppress(Exception):
        if ok:
            rt_obs.obs.event("memory.forgotten", tables=["schema_entries"], rows=1,
                             via="remove_fact")
    return ok


def forget_caller_entirely(caller_e164: str | None) -> dict:
    """Erase a caller completely — profile, memory, reminders, traces, audit.

    For 'forget me / delete everything about me'. Returns the row counts removed.
    """
    h_hash = rt_prefs.phone_hash(caller_e164 or "")
    if not h_hash:
        return {}
    # The SMS ledger is a file on this worker's disk; the wipe RPC cannot
    # reach it, and until 2026-09-02 nothing else removed it either.
    with contextlib.suppress(Exception):
        import rt_sms
        rt_sms.forget(caller_e164 or "")
    try:
        res = rt_prefs._req("POST", "rpc/rt_forget_caller", {"p_hash": h_hash}) or {}
        print(f"[rt-forget] erased everything for {_mask(caller_e164)}: {res}", flush=True)
        with contextlib.suppress(Exception):
            _counts = res if isinstance(res, dict) else {}
            rt_obs.obs.event(
                "memory.forgotten",
                tables=sorted(str(k) for k in _counts),
                rows=sum(v for v in _counts.values() if isinstance(v, int)),
                via="forget_me")
        return res if isinstance(res, dict) else {}
    except Exception as e:
        print(f"[rt-forget] failed: {e}", flush=True)
        return {}
