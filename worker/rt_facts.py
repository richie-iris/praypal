"""rt_facts.py — derive fact rows from a post-call extraction and dual-write them.

Phase 1 of the memory plan. The extractor already produces structured domains
(family, pets, hobbies, health as keyed objects) plus scalars (rules, persona,
wellbeing). Until now they were merged into prose columns and the schema
registry — useful, but with no per-fact provenance, no confirmation count, and
no honest story for contradictions.

This module turns that same extraction into rt.facts rows through ONE write
path (rt_upsert_fact), which enforces the two laws fixed in the migration:
re-hearing a fact confirms it (+0.5 confidence), hearing a different value for
the same key supersedes the old row without deleting it.

DUAL-write means exactly that: the prose columns keep being written as before.
Nothing reads rt.facts into the prompt yet, so a failure here can never touch a
live call — every entry point swallows and logs. The store just quietly grows
under every call from today, so by the time Phase 7 flips hydration to ranked
facts, confirmation counts already mean something.

Determinism owns identity: norm_key = kind:slug(subject). "Mary", "mary", and
"MARY " are one fact. (Alias resolution — "Peg is Margaret" — is Phase 3's job,
and it will REKEY, not fuzzy-match at write time.)
"""
from __future__ import annotations

import os
import re
import unicodedata
import rt_obs

import rt_prefs
import rt_shield


def _loggable(value, label: str = "") -> str:
    """The value only under RT_LOG_TRANSCRIPT=1 — a fact key is the caller's memory."""
    if os.getenv("RT_LOG_TRANSCRIPT") == "1":
        return repr(value)
    return f"<{label or 'value'} {len(str(value or ''))} chars>"

# Scalar fields that are rendered into the next system prompt. They pass the
# same shield as the prose columns before they become facts.
_DIRECTIVE_FIELDS = ("caller_rules", "persona_directives")


def _guard_directives(extracted: dict, transcript_lines: list[str] | None, tally: dict) -> dict:
    """Drop caller_rules / persona_directives that fail rt_shield.

    rule_text_allowed always applies (no steering phrases, no tool names);
    spoken_by_caller applies when the caller has a transcript to check
    against — a backfill with no transcript keeps only the text check.
    Returns a shallow copy; the caller's dict is untouched.
    """
    out = dict(extracted or {})
    for field in _DIRECTIVE_FIELDS:
        val = out.get(field)
        if not val or not str(val).strip():
            continue
        text = str(val)
        ok, why = rt_shield.rule_text_allowed(text)
        if ok and transcript_lines is not None and not rt_shield.spoken_by_caller(text, transcript_lines):
            ok, why = False, "not spoken by the caller"
        if not ok:
            out[field] = None
            tally["refused"] += 1
            print(f"[rt-facts] REFUSE {field}: {why}", flush=True)
            with __import__("contextlib").suppress(Exception):
                rt_obs.obs.event("memory.refused", kind=field, reason=why[:40],
                                 value={"chars": len(text)})
    return out


def _slug(text: str) -> str:
    """Deterministic, lowercase key fragment. Unicode-aware: Мария, 北京 and
    José all keep their identity. Review finding (blocking): the ASCII-only
    version collapsed every non-Latin name to 'unknown', so two different
    people in one family shared a norm_key and silently superseded each
    other's facts. When normalization still yields nothing (emoji-only,
    symbols), fall back to a hash of the raw subject — ugly keys are fine,
    colliding keys are corruption.
    """
    raw = str(text or "")
    t = unicodedata.normalize("NFKD", raw)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = unicodedata.normalize("NFKC", t).lower()
    t = re.sub(r"[^\w]+", "-", t, flags=re.UNICODE).replace("_", "-").strip("-")
    if t:
        return t[:64]
    if raw.strip():
        import hashlib
        return "x" + hashlib.sha1(raw.strip().encode("utf-8"), usedforsecurity=False).hexdigest()[:12]  # slug only
    return "unknown"


def _norm_key(kind: str, subject: str) -> str:
    return f"{kind}:{_slug(subject)}"


def _render(value) -> str:
    """One-line human-readable form; equality on this string decides
    confirm-vs-supersede, so it must be stable for identical inputs."""
    if isinstance(value, str):
        return " ".join(value.split())[:4000]
    if isinstance(value, dict):
        parts = [f"{k}: {_render(v)}" for k, v in sorted(value.items()) if v not in (None, "", [], {})]
        return "; ".join(parts)[:4000]
    if isinstance(value, list):
        return "; ".join(_render(v) for v in value if v not in (None, ""))[:4000]
    return str(value)[:4000]


_DOMAIN_KINDS = {
    "family": "family",
    "pets": "pet",
    "health": "health",
    "hobbies": "hobby",
    "work": "work",
    "places": "places",
    "preferences": "preference",
    "vehicles": "vehicle",
    "finances": "finance",
    "goals": "goal",
    "active_goals": "goal",
}

_SCALAR_KINDS = {
    "caller_rules": ("rule", "caller rules"),
    "persona_directives": ("persona", "voice style"),
    "wellbeing": ("wellbeing", "current state"),
}


def _predicate_of(val) -> str:
    """Coerce whatever the extractor put in relationship/relation to a short
    string. Gemini has returned dicts and lists here; slicing those raised and
    — because derivation was one try-block — killed EVERY fact from the call
    (review finding). Never trust the shape, only the rendering."""
    if not isinstance(val, dict):
        return ""
    raw = val.get("relationship") or val.get("relation") or ""
    if not isinstance(raw, str):
        raw = _render(raw)
    return raw[:80]


def _as_subject_dict(obj) -> dict:
    """Accept dict domains as-is; adapt list-shaped domains ([{'name': 'Rex',
    ...}]) that the extractor sometimes emits instead of keyed objects."""
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, list):
        out = {}
        for item in obj:
            if isinstance(item, dict):
                name = item.get("name") or item.get("subject")
                if name:
                    out[str(name)] = item
            elif isinstance(item, str) and item.strip():
                out[item.strip()] = {"detail": item.strip()}
        return out
    return {}


def derive_facts(extracted: dict) -> list[dict]:
    """Pure function: extraction dict -> list of fact param dicts. No I/O.

    Per-subject isolation: one malformed member can only lose itself, never
    the rest of the call's facts.
    """
    out: list[dict] = []
    for field, kind in _DOMAIN_KINDS.items():
        for subject, val in _as_subject_dict(extracted.get(field)).items():
            if val in (None, "", {}, []):
                continue
            try:
                out.append({
                    "p_kind": kind,
                    "p_subject": str(subject)[:120],
                    "p_norm_key": _norm_key(kind, subject),
                    "p_predicate": _predicate_of(val),
                    "p_value_json": val if isinstance(val, (dict, list)) else {"detail": val},
                    "p_value_text": _render(val),
                })
            except Exception as e:
                print(f"[rt-facts] skipped malformed {kind}/{_loggable(subject, 'subject')}: {e}", flush=True)
    for c in (extracted.get("corrections") or []):
        if not isinstance(c, dict):
            continue
        wrong = " ".join(str(c.get("wrong") or "").split())
        right = " ".join(str(c.get("right") or "").split())
        if not right:
            continue
        try:
            out.append({
                "p_kind": "correction",
                "p_subject": right[:120],
                "p_norm_key": _norm_key("correction", wrong or right),
                "p_predicate": "",
                "p_value_json": {"wrong": wrong, "right": right},
                "p_value_text": (f"said '{wrong}' — truth: {right}" if wrong else right)[:4000],
            })
        except Exception as e:
            print(f"[rt-facts] skipped correction: {e}", flush=True)
    for t in (extracted.get("open_threads") or []):
        if not isinstance(t, dict):
            continue
        topic = " ".join(str(t.get("topic") or "").split())
        detail = " ".join(str(t.get("detail") or "").split())
        if not topic or not detail:
            continue
        try:
            out.append({
                "p_kind": "thread",
                "p_subject": topic[:120],
                "p_norm_key": _norm_key("thread", topic),
                "p_predicate": "",
                "p_value_json": {"topic": topic, "detail": detail},
                "p_value_text": detail[:4000],
            })
        except Exception as e:
            print(f"[rt-facts] skipped thread {_loggable(topic, 'topic')}: {e}", flush=True)
    for field, (kind, subject) in _SCALAR_KINDS.items():
        val = extracted.get(field)
        if not val or not str(val).strip():
            continue
        try:
            text = str(val)
            if rt_prefs.looks_credential(text):
                text = rt_prefs.redact_codes(text)
            out.append({
                "p_kind": kind,
                "p_subject": subject,
                "p_norm_key": _norm_key(kind, subject),
                "p_predicate": "",
                "p_value_json": {"text": text},
                "p_value_text": _render(text),
            })
        except Exception as e:
            print(f"[rt-facts] skipped scalar {field}: {e}", flush=True)
    return out


def dual_write(h_hash: str, extracted: dict, call_id: str | None = None,
               transcript_lines: list[str] | None = None) -> dict:
    """Write derived facts through rt_upsert_fact. Never raises.

    Returns a tally for the post-call trace: {'insert': n, 'confirm': n,
    'supersede': n, 'failed': n, 'refused': n} — visible in postcall JSON so
    drift between the prose columns and the fact store can be noticed early.
    """
    tally = {"insert": 0, "confirm": 0, "supersede": 0, "failed": 0, "skipped": 0, "refused": 0}
    if not h_hash:
        return tally
    try:
        facts = derive_facts(_guard_directives(extracted or {}, transcript_lines, tally))
    except Exception as e:
        print(f"[rt-facts] derivation failed (non-fatal): {e}", flush=True)
        return tally
    import time as _t
    _deadline = _t.monotonic() + float(__import__("os").getenv("RT_FACTS_BUDGET_SEC", "6"))
    for f in facts:
        if _t.monotonic() > _deadline:
            tally["skipped"] += len(facts) - sum(v for k, v in tally.items() if k not in ("skipped", "refused"))
            print(f"[rt-facts] budget exhausted — {tally['skipped']} deferred", flush=True)
            break
        try:
            body = dict(f, p_hash=h_hash, p_source_call_id=call_id)
            res = rt_prefs._req("POST", "rpc/rt_upsert_fact", body, _skip_audit=True)
            op = (res or {}).get("op") if isinstance(res, dict) else None
            tally[op if op in tally else "failed"] += 1
            with __import__("contextlib").suppress(Exception):
                rt_obs.obs.event("memory.fact_mutation",
                                 domain=f.get("p_kind"),
                                 action=op or "unknown",
                                 confidence=float((res or {}).get("confidence") or 1.0))
        except Exception as e:
            tally["failed"] += 1
            print(f"[rt-facts] upsert failed for {_loggable(f.get('p_norm_key'), 'key')}: {e}", flush=True)
    if any(tally.values()):
        print(f"[rt-facts] dual-write: {tally}", flush=True)
    return tally
