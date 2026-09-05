"""rt_executor.py — the between-calls executor organ.

Owns AGENT tasks ("look into X, tell me next call") on the agent's side of the
determinism↔probabilism membrane. Runs inside the postcall pipeline seconds
after hangup: capture → execute (search + synthesize) → gate → surface → retire.

Tasks live in the `agent_tasks` schema category as one JSON object keyed by
content hash. Probabilism (search snippets, Gemini synthesis) never crosses
into the canvas unless the grounding gate passes; failure is stated honestly.
"""

from __future__ import annotations

import rt_obs

import hashlib
import json
import re

import rt_prefs

TASK_CATEGORY = "agent_tasks"
MAX_ATTEMPTS = 3
ANSWER_CHAR_CAP = 400
MAX_TASKS_PER_RUN = 3

_SYNTH_PROMPT = """Answer ONE research question for a phone assistant to read aloud.

QUESTION: {ask}

SEARCH RESULTS (raw, may be ads or noise):
{sources}

Rules:
- At most 2 short spoken sentences, under {cap} characters. No URLs, no markdown,
  no "according to the results".
- Every number, price and name in your answer must appear verbatim in the results
  above. Invent nothing.
- If the results don't answer the question, the answer is exactly NO_ANSWER.

Return only JSON: {{"answer": "..."}}"""


_CHATTER = frozenset("""
wanna gonna gotta yeah yep nope okay sure right well just like really think
thing things stuff that this these those them they your yours want wants
know knew said says tell told what when where about there here have has
been being does done make made take took give gave good great nice
""".split())


def _researchable(ask: str) -> bool:
    """True when an ask has enough substance to be worth searching for."""
    words = [w for w in re.findall(r"[a-z]{4,}", (ask or "").lower())
             if w not in _CHATTER]
    return len(words) >= 3


def task_id(ask: str) -> str:
    norm = " ".join("".join(c.lower() if c.isalnum() else " " for c in ask).split())
    return "t_" + hashlib.md5(norm.encode(), usedforsecurity=False).hexdigest()[:6]  # short id only


def _load_tasks(schemas: list[dict]) -> dict:
    for s in schemas:
        if (s.get("category") or "").lower() == TASK_CATEGORY:
            try:
                obj = json.loads(s.get("data_summary") or "{}")
                return obj if isinstance(obj, dict) else {}
            except Exception as _exc:
                rt_obs.obs.caught("rt_executor._load_tasks", _exc)
                return {}
    return {}


def _save_tasks(h: str, updates: dict) -> None:
    """Merge task updates (jsonb shallow-merge by task id — ids are stable keys)."""
    if not updates:
        return
    try:
        rt_prefs._req("POST", "rpc/rt_add_schema_entry", {
            "p_hash": h,
            "p_table": f"caller_{h[:8]}_{TASK_CATEGORY}",
            "p_cat": TASK_CATEGORY,
            "p_summary": json.dumps(updates),
        })
    except Exception as e:
        print(f"[rt-exec] task save failed (non-fatal): {e}", flush=True)


def capture(h: str, ask: str, schemas: list[dict] | None = None, visit: int = 0) -> str:
    """Register an agent task (idempotent by content hash). Returns the task id,
    or '' when the ask is refused — a credential is a secret to store, not homework.
    Mixed asks keep their question but lose the code before anything is stored."""
    ask = ask.strip()
    if _credential_ask(ask):
        print(f"[rt-exec] REFUSE credential-like ask at capture: {ask[:50]!r}", flush=True)
        return ""
    if not _researchable(ask):
        print(f"[rt-exec] REFUSE unresearchable ask: {ask[:50]!r}", flush=True)
        return ""
    ask = rt_prefs.redact_codes(ask)
    tid = task_id(ask)
    existing = _load_tasks(schemas or [])
    if tid in existing and existing[tid]:
        return tid
    _save_tasks(h, {tid: {"ask": ask.strip(), "status": "open", "answer": "",
                          "attempts": 0, "created_visit": visit}})
    print(f"[rt-exec] task captured {tid}: {ask[:70]!r}", flush=True)
    return tid


def _grounded(answer: str, sources: str) -> bool:
    """Deterministic gate: every number in the answer must appear in the sources.

    A confidently spoken wrong price is worse than an honest miss.
    """
    src_numbers = set(re.findall(r"\d[\d,.]*", sources))
    for num in re.findall(r"\d[\d,.]*", answer):
        if num not in src_numbers and num.replace(",", "") not in {s.replace(",", "") for s in src_numbers}:
            return False
    if "http" in answer.lower():
        return False
    return len(answer) <= ANSWER_CHAR_CAP


_ASK_FILLER = {"the", "for", "and", "our", "your", "their", "his", "her", "was",
               "code", "number", "remember", "keep", "save", "note", "write",
               "look", "into", "find", "out", "what", "next", "call", "time"}


def _credential_ask(ask: str) -> bool:
    """True when the ask is mostly a credential (code phrasing + digit run,
    almost no research content left once both are removed). A credential is
    something to store, never homework to send out to the internet."""
    if not rt_prefs.looks_credential(ask):
        return False
    residue = rt_prefs._DIGIT_RUN.sub(" ", ask)
    residue = rt_prefs._CRED_CONTEXT.sub(" ", residue)
    content = [w for w in re.findall(r"[a-zA-Z]{3,}", residue) if w.lower() not in _ASK_FILLER]
    return len(content) <= 2


def _search(ask: str, widen: bool = False) -> str:
    """One phrasing through the existing search stack. '' when nothing usable.

    `widen` asks the same question the other way round — it is the phrasing that
    rescues a price question the plain wording missed, and it is issued only when
    the plain wording has already failed, so an easy question costs one grounded
    request instead of two and a hard one keeps both of its chances.
    """
    import agent as _agent
    q = f"average {ask}" if widen and not ask.lower().startswith("average") else ask
    q = rt_prefs.redact_codes(q)
    try:
        r = _agent._perform_web_search(q)
        if r and "No conclusive" not in r:
            return r
    except Exception as e:
        print(f"[rt-exec] search error: {e}", flush=True)
    return ""


def _synthesize(gemini_json, api_key: str, ask: str, sources: str, tid: str) -> str:
    """Sources → one spoken answer, or '' when nothing survives the grounding gate."""
    if not sources.strip():
        return ""
    try:
        result = gemini_json(api_key, _SYNTH_PROMPT.format(
            ask=ask, sources=sources[:4000], cap=ANSWER_CHAR_CAP), temperature=0.1, retries=2)
        candidate = (result.get("answer") or "").strip()
        if candidate and candidate != "NO_ANSWER" and _grounded(candidate, sources):
            return candidate
        if candidate and candidate != "NO_ANSWER":
            print(f"[rt-exec] REJECT ungrounded answer for {tid}: {candidate[:60]!r}", flush=True)
    except Exception as e:
        print(f"[rt-exec] synthesis failed for {tid}: {e}", flush=True)
    return ""


def execute_open_tasks(h: str, api_key: str, gemini_json, schemas: list[dict]) -> dict:
    """Run every open task: search → synthesize → gate. Returns the updates written.

    gemini_json: injected callable (api_key, prompt, **kw) -> dict, so tests can
    can the LLM and the live path shares rt_postcall_worker._gemini_json.
    """
    tasks = _load_tasks(schemas)
    updates: dict = {}
    ran = 0
    for tid, t in tasks.items():
        if not isinstance(t, dict) or t.get("status") != "open":
            continue
        ask = t.get("ask", "")
        if _credential_ask(ask):
            updates[tid] = ""
            print(f"[rt-exec] REFUSE credential-like task {tid} — retired unexecuted", flush=True)
            continue
        if ran >= MAX_TASKS_PER_RUN:
            print(f"[rt-exec] {tid} waits for the next pass ({MAX_TASKS_PER_RUN} already run)", flush=True)
            continue
        ran += 1
        attempts = int(t.get("attempts", 0)) + 1

        sources = _search(ask)
        answer = _synthesize(gemini_json, api_key, ask, sources, tid)
        if not answer:
            widened = _search(ask, widen=True)
            if widened.strip() and widened not in sources:
                sources = (sources + "\n" + widened).strip()
                answer = _synthesize(gemini_json, api_key, ask, sources, tid)

        if answer:
            updates[tid] = {**t, "status": "answered", "answer": answer, "attempts": attempts}
            print(f"[rt-exec] task answered {tid}: {answer[:70]!r}", flush=True)
        elif attempts >= MAX_ATTEMPTS:
            updates[tid] = {**t, "status": "failed", "attempts": attempts}
            print(f"[rt-exec] task failed after {attempts} attempts: {tid}", flush=True)
        else:
            updates[tid] = {**t, "status": "open", "attempts": attempts}
            print(f"[rt-exec] task still open ({attempts}/{MAX_ATTEMPTS}): {tid}", flush=True)
    _save_tasks(h, updates)
    return updates


def retire_delivered(h: str, transcript: str, schemas: list[dict]) -> list[str]:
    """Mark answered tasks delivered when the agent actually spoke the answer.

    Detection is deterministic: a distinctive chunk of the stored answer (first
    number, or longest word ≥ 6 chars) appears in an agent: line. Failed tasks
    are retired after surfacing once.
    """
    agent_text = " ".join(l for l in transcript.splitlines()
                          if l.strip().lower().startswith("agent:")).lower()
    tasks = _load_tasks(schemas)
    updates: dict = {}
    retired: list[str] = []
    for tid, t in tasks.items():
        if not isinstance(t, dict):
            continue
        if t.get("status") == "answered":
            ans = (t.get("answer") or "").lower()
            nums = re.findall(r"\d[\d,.]*", ans)
            words = sorted(re.findall(r"[a-z]{6,}", ans), key=len, reverse=True)
            probes = (nums[:2] or []) + words[:2]
            if probes and any(p in agent_text for p in probes):
                updates[tid] = {**t, "status": "delivered"}
                retired.append(tid)
                print(f"[rt-exec] task delivered {tid}", flush=True)
        elif t.get("status") == "failed":
            updates[tid] = ""
    if updates:
        _save_tasks(h, updates)
    return retired


def render_block(schemas: list[dict], display_name: str = "the caller") -> str:
    """Prompt section for the hydrator. '' when there is nothing to say (0 tokens)."""
    tasks = _load_tasks(schemas)
    answered, working, failed = [], [], []
    for t in tasks.values():
        if not isinstance(t, dict):
            continue
        if _credential_ask(t.get("ask", "")):
            continue
        if t.get("status") == "answered":
            answered.append(f'- You looked into "{t["ask"]}". Tell {display_name} when natural: {t["answer"]}')
        elif t.get("status") == "open":
            working.append(f'"{t["ask"]}"')
        elif t.get("status") == "failed":
            failed.append(f'- You could not find a solid answer on "{t["ask"]}" — say so honestly if it comes up.')
    if not (answered or working or failed):
        return ""
    in_flight = ([f'- Still working on {", ".join(working[:3])} — if asked, say you are '
                  f'on it. NEVER hand it back as their task.'] if working else [])
    lines = "\n".join(answered[:3] + in_flight + failed[:2])
    return ("\n\n# YOUR TASKS (work YOU own for this caller — never theirs)\n" + lines)
