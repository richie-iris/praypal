#!/usr/bin/env python3
"""migrate.py — Automated Database Migration Manager for Phone-Pal.

Tracks schema migrations in `public.schema_migrations`, validates file checksums
to prevent silent edits/drift, and applies pending migrations sequentially.

Usage:
    python scripts/migrate.py --status
    python scripts/migrate.py --dry-run
    python scripts/migrate.py --apply
    python scripts/migrate.py --apply --lane dev
    python scripts/migrate.py --apply --env-file /path/to/lane.env   # that file ONLY
    LANE_ENV=1 python scripts/migrate.py --apply                     # process env ONLY
    python scripts/migrate.py --apply --confirm <project-ref>   # required on a production ref
    python scripts/migrate.py --verify-only

Where the Supabase values come from:
    --env-file <path>   ONLY that file (python-dotenv values). Never worker/.env.local,
                        never worker/.env, never the process environment.
    LANE_ENV=1          ONLY the process environment (a provisioning script that
                        exported the lane's values). Never a worker/.env* file.
    otherwise           worker/.env.local, else worker/.env, else the process
                        environment — the developer's own checkout.
The exclusivity is the point: a lane stand-up that named its values must never
be answered by whatever a developer's .env.local happens to hold (#20).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import config as _config
except Exception:  # noqa: BLE001 - run from anywhere; the fallback is the same tuple
    _config = None

_NUMBERED = re.compile(r"^(\d{2})-")
_REF_SPLIT = re.compile(r"[,\s;]+")
# Same rule as sql_push._is_prod (which cannot be imported here: it resolves
# its ref from os.environ at import, and this runner reads an env FILE).
PROD_AGENT_NAMES: tuple[str, ...] = tuple(
    getattr(_config, "PROD_AGENT_NAMES", ("iris-phone", "phone-pal-prod"))
)


def _norm_ref(ref: str | None) -> str:
    return (ref or "").strip().lower()


def is_prod_ref(ref: str | None, env_cfg: dict | None = None) -> bool:
    """A ref listed in RT_PROD_SUPABASE_REFS, or an AGENT_NAME that is (or
    starts with) a prod name, in the env file or the process env."""
    env_cfg = env_cfg or {}

    def _get(key: str) -> str:
        return (env_cfg.get(key) or os.getenv(key) or "").strip()

    prod_refs = {_norm_ref(r) for r in _REF_SPLIT.split(_get("RT_PROD_SUPABASE_REFS")) if r.strip()}
    if _norm_ref(ref) and _norm_ref(ref) in prod_refs:
        return True
    agent = _get("AGENT_NAME").lower()
    return bool(agent) and any(agent == n or agent.startswith(n) for n in PROD_AGENT_NAMES)


def check_contiguous(files) -> list[int]:
    """Numbers missing from the NN- sequence of `files` (str, Path or MigrationFile).

    A gap is indistinguishable from a lost file, so callers treat any result
    other than [] as an error. Files without a NN- prefix are ignored.
    """
    nums: set[int] = set()
    for f in files:
        name = Path(str(getattr(f, "filename", f))).name
        m = _NUMBERED.match(name)
        if m:
            nums.add(int(m.group(1)))
    if not nums:
        return []
    return [n for n in range(min(nums), max(nums) + 1) if n not in nums]


@dataclass
class MigrationFile:
    filename: str
    path: Path
    checksum: str
    sql: str


@dataclass
class MigrationRecord:
    version: str
    checksum: str
    applied_at: str


def lane_env_requested() -> bool:
    """LANE_ENV=1 in the process environment: a provisioning script exported
    the lane's Supabase values and wants them used to the exclusion of any
    worker/.env* file on this machine."""
    return os.environ.get("LANE_ENV", "").strip() == "1"


_URL_REF = re.compile(r"^https?://([a-z0-9]{20})\.supabase\.co/?$", re.I)


def ref_from_url(url: str | None) -> str:
    """The project ref carried by a SUPABASE_URL (https://<ref>.supabase.co), or ''."""
    m = _URL_REF.match((url or "").strip())
    return m.group(1).lower() if m else ""


class MigrationRunner:
    def __init__(self, env_file: Path | None = None, *, exclusive: bool | None = None):
        """Where the Supabase ref / token / key come from — and from nowhere
        else — is decided here, once:

        * env_file with exclusive=True (--env-file): that file only. A file
          that does not exist is an error, not a fallback.
        * exclusive=True without a file (LANE_ENV=1): the process environment
          only.
        * otherwise (a developer's own checkout): env_file, else ROOT/.env.local,
          else ROOT/.env, with the process environment filling gaps.

        exclusive=None means "exclusive iff --env-file was given or LANE_ENV=1".
        The values never come from two sources at once when a source was named:
        a lane stand-up that named its values must never be answered by a
        developer's .env.local (#20)."""
        if exclusive is None:
            exclusive = env_file is not None or lane_env_requested()
        self.exclusive = bool(exclusive)
        self.env_file: Path = env_file if env_file is not None else ROOT / ".env.local"

        if self.exclusive and env_file is not None:
            if not self.env_file.is_file():
                raise FileNotFoundError(f"--env-file {self.env_file} does not exist")
            env_cfg = dict(dotenv_values(self.env_file))
            self.source = f"env-file {self.env_file}"
            fallback: dict[str, str] = {}
        elif self.exclusive:
            env_cfg = {k: v for k, v in os.environ.items()}
            self.source = "process environment (LANE_ENV=1)"
            fallback = {}
        else:
            if env_file is None and not self.env_file.exists():
                # Fallback to .env if .env.local doesn't exist
                alt = ROOT / ".env"
                if alt.exists():
                    self.env_file = alt
            env_cfg = dict(dotenv_values(self.env_file)) if self.env_file.exists() else {}
            self.source = self.env_file.name
            fallback = dict(os.environ)
        self.env_cfg = env_cfg

        def _get(key: str) -> str:
            return (env_cfg.get(key) or fallback.get(key) or "").strip()

        # The ref and the URL are read as a PAIR from the first source that
        # names either, so a file's URL is never checked against an
        # environment's ref: two names for the target must agree, and when they
        # do not, nothing is guessed.
        pair = env_cfg if (env_cfg.get("SUPABASE_PROJECT_REF") or env_cfg.get("SUPABASE_URL")) else fallback
        self.ref = _norm_ref(pair.get("SUPABASE_PROJECT_REF") or "")
        url_ref = ref_from_url(pair.get("SUPABASE_URL") or "")
        if self.ref and url_ref and url_ref != self.ref:
            raise ValueError(
                f"refused: SUPABASE_PROJECT_REF {self.ref!r} and SUPABASE_URL ref {url_ref!r} "
                f"disagree in {self.source}"
            )
        self.ref = self.ref or url_ref
        self.pat = _get("SUPABASE_ACCESS_TOKEN")
        self.svc = _get("SUPABASE_SERVICE_ROLE_KEY")
        self.base_url = f"https://{self.ref}.supabase.co"

    def is_prod(self) -> bool:
        return is_prod_ref(self.ref, self.env_cfg)

    def refuse_prod_apply(self, confirm: str | None) -> str | None:
        """Why --apply may not run on this ref (None = it may): production
        needs --confirm <ref> retyped; nothing is sent when refused."""
        if not self.is_prod():
            return None
        if _norm_ref(confirm) != _norm_ref(self.ref) or not self.ref:
            return f"refused: {self.ref!r} is a production ref; --apply needs --confirm {self.ref}"
        return None

    def _execute_sql(self, sql: str) -> Any:
        """Executes raw SQL via the Supabase Management API or direct endpoint."""
        if not self.pat and not self.ref:
            raise RuntimeError(
                f"Missing SUPABASE_ACCESS_TOKEN or SUPABASE_PROJECT_REF in {self.source}"
            )
        req = urllib.request.Request(
            f"https://api.supabase.com/v1/projects/{self.ref}/database/query",
            data=json.dumps({"query": sql}).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.pat}",
                "Content-Type": "application/json",
                "User-Agent": "phone-pal-migrator/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:  # noqa: S310 - fixed https Supabase URL
                data = resp.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Database query failed (HTTP {exc.code}): {err_body}") from exc

    def ensure_ledger(self) -> None:
        """Ensure the public.schema_migrations table exists."""
        ddl = """
        CREATE TABLE IF NOT EXISTS public.schema_migrations (
            version TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        COMMENT ON TABLE public.schema_migrations IS 'Tracks applied schema migrations and checksums';
        """
        self._execute_sql(ddl)

    def get_local_migrations(self) -> list[MigrationFile]:
        """Load and sort all migration scripts in worker/sql/."""
        if not SQL_DIR.exists():
            return []
        files = sorted(SQL_DIR.glob("*.sql"))
        migrations = []
        for f in files:
            content = f.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
            migrations.append(
                MigrationFile(
                    filename=f.name,
                    path=f,
                    checksum=checksum,
                    sql=content,
                )
            )
        return migrations

    def get_applied_migrations(self) -> dict[str, MigrationRecord]:
        """Fetch all recorded migrations from the database."""
        self.ensure_ledger()
        sql = "SELECT version, checksum, applied_at::text FROM public.schema_migrations ORDER BY version ASC;"
        res = self._execute_sql(sql)
        applied: dict[str, MigrationRecord] = {}
        if isinstance(res, list):
            for row in res:
                v = row.get("version")
                if v:
                    applied[v] = MigrationRecord(
                        version=v,
                        checksum=row.get("checksum", ""),
                        applied_at=row.get("applied_at", ""),
                    )
        return applied

    def status(self) -> tuple[int, int, int]:
        """Prints migration status. Returns (applied_count, pending_count, modified_count)."""
        local_files = self.get_local_migrations()
        applied_map = self.get_applied_migrations()

        print(f"\nMigration Status for project: {self.ref or '(unknown)'} (using {self.source})")
        print("=" * 80)
        print(f"{'Migration':<45} {'Status':<12} {'Checksum (SHA256)':<18}")
        print("-" * 80)

        applied_count = 0
        pending_count = 0
        modified_count = 0

        for mig in local_files:
            if mig.filename in applied_map:
                record = applied_map[mig.filename]
                if record.checksum and record.checksum != mig.checksum:
                    status = "MODIFIED"
                    modified_count += 1
                else:
                    status = "APPLIED"
                    applied_count += 1
            else:
                status = "PENDING"
                pending_count += 1

            short_hash = mig.checksum[:16]
            print(f"{mig.filename:<45} {status:<12} {short_hash}")

        print("=" * 80)
        print(f"Total: {len(local_files)} | Applied: {applied_count} | Pending: {pending_count} | Modified: {modified_count}")
        gaps = check_contiguous(local_files)
        if gaps:
            print(f"sequence: GAPS — missing {', '.join(f'{n:02d}' for n in gaps)}\n")
        else:
            print("sequence: contiguous\n")
        return applied_count, pending_count, modified_count

    def apply(self, dry_run: bool = False) -> bool:
        """Applies pending migrations sequentially.

        Refuses on a gap in the NN- sequence before touching the database: a
        missing number is indistinguishable from a lost file, and applying
        around it would record the checkout as current when it is not.
        """
        local_files = self.get_local_migrations()
        gaps = check_contiguous(local_files)
        if gaps:
            print(f"sequence: GAPS — missing {', '.join(f'{n:02d}' for n in gaps)}", file=sys.stderr)
            print("refused: fix the sql/ numbering before applying", file=sys.stderr)
            return False
        applied_map = self.get_applied_migrations()

        pending: list[MigrationFile] = []
        for mig in local_files:
            if mig.filename not in applied_map:
                pending.append(mig)
            elif applied_map[mig.filename].checksum != mig.checksum:
                print(
                    f"WARNING: Migration {mig.filename} was modified after being applied!",
                    file=sys.stderr,
                )

        if not pending:
            print("Database is up to date. No pending migrations.")
            return True

        print(f"Found {len(pending)} pending migration(s):")
        for mig in pending:
            print(f"  - {mig.filename}")

        if dry_run:
            print("\n[Dry Run] No migrations were applied.")
            return True

        print("\nApplying migrations...")
        for mig in pending:
            print(f"Applying {mig.filename}...", end=" ", flush=True)
            try:
                # Wrap migration execution and ledger update in transaction if possible
                wrapped_sql = f"""
                BEGIN;
                {mig.sql}
                INSERT INTO public.schema_migrations (version, checksum, applied_at)
                VALUES ('{mig.filename}', '{mig.checksum}', NOW())
                ON CONFLICT (version) DO UPDATE SET checksum = EXCLUDED.checksum, applied_at = NOW();
                COMMIT;
                """  # noqa: S608 - filename/checksum come from the repo, not user input
                self._execute_sql(wrapped_sql)
                print("SUCCESS")
            except Exception as exc:
                print(f"FAILED!\nError applying {mig.filename}: {exc}", file=sys.stderr)
                return False

        print(f"\nSuccessfully applied {len(pending)} migration(s).")
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Phone-Pal Database Migration Manager")
    parser.add_argument("--status", action="store_true", help="Show migration status")
    parser.add_argument("--dry-run", action="store_true", help="Preview migrations to be applied")
    parser.add_argument("--apply", action="store_true", help="Apply all pending migrations")
    parser.add_argument("--verify-only", action="store_true", help="Exit 1 if any migrations are pending")
    parser.add_argument("--lane", choices=["dev", "test", "local", "prod"], default=None, help="Target environment lane")
    parser.add_argument("--env-file", type=str, default=None,
                        help="Read the Supabase values from this file ONLY (never worker/.env* or the environment)")
    parser.add_argument("--confirm", type=str, default=None, help="Retype the project ref to --apply on production")

    args = parser.parse_args()

    # Exactly one source of Supabase values. --env-file and LANE_ENV=1 are
    # exclusive by contract; --lane and the default keep the developer's
    # file-then-environment lookup.
    try:
        if args.env_file:
            runner = MigrationRunner(Path(args.env_file), exclusive=True)
        elif lane_env_requested():
            if args.lane:
                print("refused: LANE_ENV=1 (process environment only) and --lane both name a source; "
                      "pick one", file=sys.stderr)
                sys.exit(2)
            runner = MigrationRunner(None, exclusive=True)
        elif args.lane:
            runner = MigrationRunner(ROOT / f".env.{args.lane}", exclusive=False)
        else:
            runner = MigrationRunner(None, exclusive=False)
    except (FileNotFoundError, ValueError) as exc:
        print(f"refused: {exc}" if not str(exc).startswith("refused") else str(exc), file=sys.stderr)
        sys.exit(2)

    if args.verify_only:
        try:
            _, pending, modified = runner.status()
            if pending > 0 or modified > 0 or check_contiguous(runner.get_local_migrations()):
                sys.exit(1)
            sys.exit(0)
        except Exception as e:
            print(f"Verification failed: {e}", file=sys.stderr)
            sys.exit(1)

    if args.apply and not args.dry_run:
        why = runner.refuse_prod_apply(args.confirm)
        if why:
            print(why, file=sys.stderr)
            sys.exit(2)

    if args.apply or args.dry_run:
        ok = runner.apply(dry_run=args.dry_run)
        sys.exit(0 if ok else 1)

    # Default to status
    try:
        runner.status()
    except Exception as e:
        print(f"Could not connect to database: {e}", file=sys.stderr)
        print(f"Tip: Ensure SUPABASE_PROJECT_REF and SUPABASE_ACCESS_TOKEN are configured in {runner.source}.")
    # A gap in the sequence is reported even when the database is unreachable:
    # it is a property of the checkout, not the connection.
    gaps = check_contiguous(runner.get_local_migrations())
    if gaps:
        print(f"sequence: GAPS — missing {', '.join(f'{n:02d}' for n in gaps)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
