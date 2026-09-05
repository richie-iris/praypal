#!/usr/bin/env python3
"""reset_caller.py — wipe ONE caller so a phone number reads as brand-new.

For testing from one phone: reset your own number to make the next call take
the brand-new-caller path.

Deletes the caller's rows from rt.callers, rt.account_schema_registry,
rt.reminders, and rt.audit_log — nothing else, nobody else. The cached
personalized greeting clip on the worker host is keyed off DB state, so
after the row is gone the next call takes the unknown-caller path.

Defaults to the lane .env.dev names. --test uses the lane .env.test names
and makes you retype its ref first.

Reads .env.local and deploy/worker.env (the file the box actually boots
from) so the hash pepper here is the one the worker used to write the row;
with a different pepper the hash misses and nothing is reset.

Usage:
    python scripts/reset_caller.py +15551234567
    python scripts/reset_caller.py +15551234567 --test
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

# First file wins for a key set in both: the operator's .env.local overrides
# the deployed worker.env.
ENV_FILES = (ROOT / ".env.local", ROOT.parent / "deploy" / "worker.env")
for _env in ENV_FILES:
    load_dotenv(_env)

try:
    import config as _config
except Exception:  # noqa: BLE001 - same truthiness rule inline below
    _config = None


def pepper_required() -> bool:
    fn = getattr(_config, "pepper_required", None)
    if callable(fn):
        return bool(fn())
    return os.getenv("RT_REQUIRE_PEPPER", "").strip().lower() in ("1", "true", "yes")


def refuse_without_pepper() -> None:
    """Exit before hashing when the lane demands a pepper and none is set: an
    unpeppered hash never matches a peppered row, so the reset would silently
    do nothing — or, on a lane that hashes bare, hit a different caller."""
    if pepper_required() and not os.getenv("RT_PHONE_HASH_PEPPER", "").strip():
        raise SystemExit("refused: RT_REQUIRE_PEPPER is set but RT_PHONE_HASH_PEPPER is empty — "
                         "set the lane's pepper (deploy/worker.env) before resetting a caller")


def _ref_of(env_file: str) -> str:
    from dotenv import dotenv_values
    return (dotenv_values(ROOT / env_file) or {}).get("SUPABASE_PROJECT_REF", "").strip()


DEV_REF = _ref_of(".env.dev")
TEST_REF = _ref_of(".env.test")

from rt_prefs import phone_hash


def mgmt_query(ref: str, sql: str) -> list:
    pat = os.getenv("SUPABASE_ACCESS_TOKEN", "")
    if not pat:
        raise SystemExit("SUPABASE_ACCESS_TOKEN not set (.env.local)")
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=json.dumps({"query": sql}).encode(),
        headers={"Authorization": f"Bearer {pat}",
                 "Content-Type": "application/json",
                 "User-Agent": "iris-realtime-admin/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 - fixed https Supabase URL
        return json.loads(r.read())


def main() -> None:
    refuse_without_pepper()
    args = [a for a in sys.argv[1:]]
    other = "--test" in args or "--prod" in args
    nums = [a for a in args if not a.startswith("--")]
    if len(nums) != 1:
        raise SystemExit(__doc__)
    h = phone_hash(nums[0])
    if not h:
        raise SystemExit(f"could not normalize {nums[0]} to E.164")

    ref = TEST_REF if other else DEV_REF
    if not ref:
        raise SystemExit("no SUPABASE_PROJECT_REF in the chosen env file")
    if other:
        typed = input(f"Reset {nums[0]} on the TEST lane ({ref}). Type the ref to confirm: ").strip()
        if typed != TEST_REF:
            raise SystemExit("ref mismatch — nothing done")

    sql = f"SELECT public.rt_forget_caller('{h}') AS forgotten"
    out = mgmt_query(ref, sql)
    print(f"[reset] {'test' if other else 'dev'} lane ({ref}) {nums[0]} → {out[0] if out else out}")


if __name__ == "__main__":
    main()
