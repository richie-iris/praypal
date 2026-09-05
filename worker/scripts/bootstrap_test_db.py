#!/usr/bin/env python3
"""bootstrap_test_db.py — Automated Database Schema Initializer for Test/Staging Instances.

Pushes every sql/NN-*.sql file, in order, to the target Supabase project and
records each in public.schema_migrations with the same (version, checksum)
ledger scripts/migrate.py and sql_push.py --file use.

Everything goes through sql_push: a production ref is refused before anything
is sent, every file passes sql_push's destructive guard (migrations contain
CREATE OR REPLACE / GRANT, so `--confirm <ref>` is needed), and the ledger row
comes from sql_push.ledger_sql.

Usage:
    python3 scripts/bootstrap_test_db.py [.env.test] --confirm <project-ref>
"""
from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _load_env(env_file: str) -> None:
    if os.path.exists(env_file):
        load_dotenv(env_file)
    else:
        load_dotenv(".env.local")
        load_dotenv(".env")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    confirm = None
    if "--confirm" in args:
        idx = args.index("--confirm")
        if idx + 1 >= len(args) or args[idx + 1].startswith("--"):
            print("usage: --confirm needs a value", file=sys.stderr)
            return 2
        confirm = args[idx + 1]
        del args[idx:idx + 2]
    env_file = args[0] if args else ".env.test"
    _load_env(env_file)

    import sql_push  # after the env is loaded: it resolves the ref at import

    ref = sql_push.REF
    if sql_push._is_prod(ref):
        print(f"refused: {ref!r} is a production ref; this script only bootstraps test/staging", file=sys.stderr)
        return 2

    sql_files = sorted(glob.glob(str(ROOT / "sql" / "*.sql")))
    if not sql_files:
        print("❌ No SQL migration files found in sql/ directory!", file=sys.stderr)
        return 1

    # Validate every file before sending any: a refusal half-way through
    # would leave the schema partially applied.
    batch: list[tuple[str, str]] = []
    for fpath in sql_files:
        fname = os.path.basename(fpath)
        with open(fpath, "r", encoding="utf-8") as f:
            sql_content = f.read()
        sql_push.guard_destructive(ref, sql_content, confirm)
        batch.append((fname, sql_push.ledger_sql(fname, sql_content)))

    print(f"🚀 Bootstrapping database schema with {len(batch)} migration files...")
    print(f"   Target Ref: {ref}")
    applied = 0
    for fname, payload in batch:
        try:
            sql_push.mgmt_query(payload, confirm=confirm)
            applied += 1
            print(f"  [✓] {fname} applied successfully.")
        except Exception as e:  # noqa: BLE001 - report and keep going; the ledger shows what landed
            print(f"  [❌] {fname} failed: {e}", file=sys.stderr)

    print(f"🎉 Database bootstrap complete: {applied}/{len(batch)} migrations applied.")
    return 0 if applied == len(batch) else 1


if __name__ == "__main__":
    sys.exit(main())
