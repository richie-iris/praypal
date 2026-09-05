"""rt_recovery.py — the post-call queue, and healing on the way up.

Extraction used to run only in the LiveKit shutdown callback. That callback is
never delivered on SIGKILL, an OOM, a `docker compose up` over a live call, or a
host maintenance event — and the transcript itself lived only in a Python list,
so those calls lost the conversation AND the memory it would have become, with
nothing left behind to recover from. The deploy workflow records this happening
to a real caller: "the caller was cut off mid-sentence and that call's memory
was lost."

A shutdown hook cannot fix that, because SIGKILL is not delivered to anyone. So
the fix is on the way UP, not on the way down:

  * every turn is written as it is spoken            (rt_trace.turn)
  * every call is queued at PICKUP, not at hangup    (enqueue, below)
  * a clean hangup marks its own job done            (complete, below)
  * anything still pending at the next boot is run   (run_boot_recovery)

Everything here is defensive: recovery must never delay worker registration,
never crash the worker, and never touch a call that is still in progress.
"""
from __future__ import annotations

import rt_obs

import contextlib
import os
import threading
import time

import rt_prefs

_ENABLED = os.getenv("RT_RECOVERY", "1").strip().lower() in ("1", "true", "yes")

_BOOT_DRAIN_LIMIT = int(os.getenv("RT_RECOVERY_BOOT_LIMIT", "5"))
_MAX_ATTEMPTS = int(os.getenv("RT_RECOVERY_MAX_ATTEMPTS", "3"))
_MIN_AGE_SECONDS = int(os.getenv("RT_RECOVERY_MIN_AGE_SEC", "900"))


def enqueue(call_id: str, phone_hash: str, priority: int = 5) -> None:
    """Queue a call for post-call processing. Idempotent; safe to call at pickup."""
    if not _ENABLED or not call_id or not phone_hash:
        return

    def _w() -> None:
        try:
            rt_prefs._req("POST", "rpc/rt_postcall_enqueue", {
                "p_call_id": call_id, "p_hash": phone_hash, "p_priority": priority,
            }, _skip_audit=True)
            rt_obs.obs.event("postcall.queued", call_id=call_id, job_id=call_id,
                             priority=priority)
        except Exception as e:
            print(f"[rt-recovery] enqueue failed (non-fatal): {e}", flush=True)
            rt_obs.obs.caught("rt_recovery.enqueue", e, call_id=call_id)

    threading.Thread(target=_w, daemon=True).start()


def complete(call_id: str, ok: bool, error: str | None = None, skipped: bool = False) -> None:
    """Mark a job terminal. Called by the live path after a clean post-call."""
    if not _ENABLED or not call_id:
        return
    if not ok and not skipped:
        _err = None
        with contextlib.suppress(Exception):
            _err = str(error)[:200] if error else None
        rt_obs.obs.error("postcall.failed", call_id=call_id, job_id=call_id, err=_err)
    try:
        rt_prefs._req("POST", "rpc/rt_postcall_complete", {
            "p_call_id": call_id, "p_ok": bool(ok), "p_error": (error or None)[:500] if error else None,
            "p_skipped": bool(skipped), "p_max_attempts": _MAX_ATTEMPTS,
        }, _skip_audit=True)
    except Exception as e:
        print(f"[rt-recovery] complete failed (non-fatal): {e}", flush=True)
        rt_obs.obs.caught("rt_recovery.complete", e, call_id=call_id)


def _claim() -> dict | None:
    res = rt_prefs._req("POST", "rpc/rt_postcall_claim", {
        "p_max_attempts": _MAX_ATTEMPTS, "p_min_age_seconds": _MIN_AGE_SECONDS,
    }, _skip_audit=True)
    if isinstance(res, dict) and res.get("call_id"):
        return res
    return None


def _transcript_for(call_id: str) -> str:
    res = rt_prefs._req("POST", "rpc/rt_call_transcript", {"p_call_id": call_id},
                        _skip_audit=True)
    return res if isinstance(res, str) else ""


def queue_stats() -> dict:
    try:
        res = rt_prefs._req("POST", "rpc/rt_postcall_queue_stats", {}, _skip_audit=True)
        return res if isinstance(res, dict) else {}
    except Exception as _exc:
        rt_obs.obs.caught("rt_recovery.queue_stats", _exc)
        return {}


def drain(limit: int = _BOOT_DRAIN_LIMIT) -> dict:
    """Run up to `limit` queued post-calls. Returns a tally."""
    done = failed = skipped = 0
    for _ in range(max(0, limit)):
        try:
            job = _claim()
        except Exception as e:
            print(f"[rt-recovery] claim failed: {e}", flush=True)
            rt_obs.obs.caught("rt_recovery.claim", e)
            break
        if not job:
            break
        call_id = job["call_id"]
        _scope = {"call_id": call_id, "job_id": call_id}
        with contextlib.suppress(Exception):
            _attempt = int(job.get("attempts") or 0)
            if _attempt:
                _scope["attempt"] = _attempt
        with rt_obs.obs.scope(**_scope):
            try:
                transcript = _transcript_for(call_id)
                if not (transcript or "").strip():
                    complete(call_id, ok=False, skipped=True, error="no transcript to recover")
                    skipped += 1
                    print(f"[rt-recovery] {call_id}: nothing to extract — skipped", flush=True)
                    continue

                import rt_postcall_worker
                t0 = time.time()
                res = rt_postcall_worker.process_post_call_transcript(
                    None, transcript, phone_hash=job["phone_hash"],
                    call_id=call_id) or {}
                if res.get("status") == "skipped":
                    complete(call_id, ok=False, skipped=True, error=str(res.get("reason"))[:200])
                    skipped += 1
                else:
                    complete(call_id, ok=True)
                    done += 1
                print(f"[rt-recovery] {call_id}: recovered in {time.time()-t0:.1f}s "
                      f"({len(transcript)} chars, attempt {job.get('attempts')})", flush=True)
            except Exception as e:
                complete(call_id, ok=False, error=str(e))
                failed += 1
                print(f"[rt-recovery] {call_id}: FAILED ({e})", flush=True)
    return {"done": done, "failed": failed, "skipped": skipped}


def recover(min_turns: int = 2, lookback_hours: int = 168, limit: int = 50) -> dict:
    """Re-open crashed jobs and queue calls that never got one at all."""
    res = rt_prefs._req("POST", "rpc/rt_postcall_recover", {
        "p_min_turns": min_turns, "p_lookback_hours": lookback_hours, "p_limit": limit,
    }, _skip_audit=True)
    out = res if isinstance(res, dict) else {}
    with contextlib.suppress(Exception):
        _requeued = int(out.get("requeued_running") or 0)
        if _requeued:
            rt_obs.obs.event("job.reclaimed", type="postcall", jobs=_requeued,
                             enqueued_missed=int(out.get("enqueued_missed") or 0))
    return out


def run_boot_recovery() -> None:
    """Heal on the way up. Non-blocking: never delays worker registration.

    Call from __main__ ONLY. agent.py's module body is re-executed in every
    spawned job process, and a recovery sweep per job process would both stampede
    the queue and charge the framework's process-init budget for it.
    """
    if not _ENABLED:
        print("[rt-recovery] disabled (RT_RECOVERY=0)", flush=True)
        return

    def _run() -> None:
        try:
            swept = recover()
            stats = queue_stats()
            print(f"[rt-recovery] boot sweep: {swept} | queue: {stats}", flush=True)
            if (stats.get("pending") or 0):
                tally = drain(_BOOT_DRAIN_LIMIT)
                print(f"[rt-recovery] boot drain: {tally} | queue now: {queue_stats()}", flush=True)
        except Exception as e:
            print(f"[rt-recovery] boot recovery failed (non-fatal): {e}", flush=True)
            rt_obs.obs.caught("rt_recovery.boot", e, level="ERROR")

    threading.Thread(target=_run, daemon=True, name="rt-recovery-boot").start()
