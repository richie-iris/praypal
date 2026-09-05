"""test_migrations.py — Unit tests for database migration files and runner logic."""
from __future__ import annotations

import re
from scripts.migrate import MigrationRunner, SQL_DIR


def test_sql_files_exist_and_ordered():
    assert SQL_DIR.exists(), f"SQL directory {SQL_DIR} does not exist"
    sql_files = sorted(SQL_DIR.glob("*.sql"))
    assert len(sql_files) >= 15, f"Expected at least 15 migration files, found {len(sql_files)}"

    pattern = re.compile(r"^(\d{2})-[a-z0-9\-_]+\.sql$")
    seen_numbers = set()

    for f in sql_files:
        match = pattern.match(f.name)
        assert match is not None, f"File {f.name} does not match naming convention 'NN-description.sql'"
        num = int(match.group(1))
        assert num not in seen_numbers, f"Duplicate migration number: {num:02d} ({f.name})"
        seen_numbers.add(num)
        # Check non-empty
        content = f.read_text(encoding="utf-8").strip()
        assert len(content) > 0, f"Migration file {f.name} is empty"


def test_migration_runner_file_discovery():
    runner = MigrationRunner()
    migrations = runner.get_local_migrations()
    assert len(migrations) > 0

    # Ensure checksums are valid sha256 hex strings
    for m in migrations:
        assert len(m.checksum) == 64
        assert m.filename.endswith(".sql")
        assert len(m.sql) > 0
