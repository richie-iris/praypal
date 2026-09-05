"""test_doc_drift.py — Automated validation to prevent documentation drift.

Ensures that:
1. Every Python module in worker/ is documented in the README.md module index.
2. Every operator CLI tool in worker/scripts/ is documented in README.md.
3. Every file link or referenced module in README.md exists on disk.
"""
from __future__ import annotations

import re
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WORKER_DIR = ROOT / "worker"
README_FILE = ROOT / "README.md"


def test_readme_exists():
    assert README_FILE.exists(), "README.md is missing from repository root"


def test_all_worker_modules_documented():
    readme_text = README_FILE.read_text(encoding="utf-8")
    worker_py_files = sorted(WORKER_DIR.glob("*.py"))
    assert len(worker_py_files) > 0

    undocumented = []
    for f in worker_py_files:
        module_name = f.name
        # Check if module is mentioned in README
        if module_name not in readme_text:
            undocumented.append(module_name)

    assert not undocumented, (
        f"Documentation drift detected! The following modules in worker/ are not documented in README.md: "
        f"{', '.join(undocumented)}. Please update README.md."
    )


def test_all_scripts_documented():
    readme_text = README_FILE.read_text(encoding="utf-8")
    scripts_dir = WORKER_DIR / "scripts"
    assert scripts_dir.exists()

    script_py_files = sorted(scripts_dir.glob("*.py"))
    undocumented = []
    for f in script_py_files:
        script_name = f.name
        if script_name not in readme_text:
            undocumented.append(f"scripts/{script_name}")

    # Note: If there are utility scripts not meant for top-level, they can be listed or checked
    # For now, ensure all key scripts are referenced
    assert not undocumented, (
        f"Documentation drift detected! The following operator scripts in worker/scripts/ are not mentioned in README.md: "
        f"{', '.join(undocumented)}. Please update README.md."
    )


def test_no_broken_markdown_links():
    readme_text = README_FILE.read_text(encoding="utf-8")
    # Match markdown links to local files [text](path) or [text](file:///path)
    links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", readme_text)

    broken_links = []
    for title, target in links:
        if target.startswith("http://") or target.startswith("https://") or target.startswith("#"):
            continue
        if target.startswith("file://"):
            # Handle absolute file URI if present
            path_str = target.replace("file://", "")
            p = Path(path_str)
        else:
            p = ROOT / target

        if not p.exists():
            broken_links.append(f"[{title}]({target})")

    assert not broken_links, (
        f"Broken local links detected in README.md: {', '.join(broken_links)}"
    )


# ---------------------------------------------------------------------------
# Hardening-era drift checks (G8). These pin facts that rotted once already:
# a stale test count, a security section that omitted the prod pepper gate,
# and the system-prompt viewer shipping from the public sites/ tree.
# ---------------------------------------------------------------------------

DOCS_DIR = ROOT / "docs"
SITES_DIR = ROOT / "sites"
CODEOWNERS_FILE = ROOT / ".github" / "CODEOWNERS"


def test_readme_has_no_hardcoded_test_count():
    # Counts drift on every PR; the suite itself is the source of truth.
    readme_text = README_FILE.read_text(encoding="utf-8")
    hits = re.findall(r"\b\d+\+? tests\b", readme_text)
    assert not hits, f"README.md hard-codes a test count ({hits}); describe the suite, do not count it"


def test_readme_mentions_require_pepper():
    readme_text = README_FILE.read_text(encoding="utf-8")
    assert "RT_REQUIRE_PEPPER" in readme_text, (
        "README.md must document RT_REQUIRE_PEPPER (the prod-lane HMAC pepper gate)"
    )


def test_adr_directory_has_at_least_five_records():
    adr_dir = DOCS_DIR / "adr"
    assert adr_dir.is_dir(), "docs/adr/ is missing"
    records = sorted(p.name for p in adr_dir.glob("*.md"))
    assert len(records) >= 5, f"docs/adr must hold >= 5 ADRs, found {records}"


def test_codeowners_exists():
    assert CODEOWNERS_FILE.is_file(), ".github/CODEOWNERS is missing"


def test_sites_ships_no_prompt_named_file():
    # sites/ is the public static tree; anything named *prompt* there leaks
    # the assistant's system prompt to the internet.
    assert SITES_DIR.is_dir(), "sites/ is missing"
    offenders = sorted(
        str(p.relative_to(ROOT))
        for p in SITES_DIR.rglob("*")
        if p.is_file() and "prompt" in p.name.lower()
    )
    assert not offenders, f"sites/ must not contain prompt-named files: {offenders}"


# ---------------------------------------------------------------------------
# Round-2 drift checks. Each pins a claim the verifiers found rotten: the
# prompt stencil leaking into a public tree, a script flag the script does not
# accept, and a log prefix nothing in worker/ ever prints.
# ---------------------------------------------------------------------------

DEPLOY_DIR = ROOT / "deploy"
RUNBOOK_FILE = DOCS_DIR / "RUNBOOK.md"
SCRIPTS_DIR = WORKER_DIR / "scripts"
PROMPT_MARKERS = ("HONEST LIMITS", "# WHO YOU ARE")


def _text_files(root: Path):
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        try:
            yield p, p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary asset (jpg, svg font) — cannot carry the stencil


def test_no_prompt_stencil_markers_under_sites_or_deploy():
    # sites/ is served publicly and deploy/ is copied to every lane host; the
    # system-prompt stencil (rt_hydrator.STENCIL_TEMPLATE) belongs in neither.
    offenders = []
    for base in (SITES_DIR, DEPLOY_DIR):
        assert base.is_dir(), f"{base.relative_to(ROOT)}/ is missing"
        for p, text in _text_files(base):
            hits = [m for m in PROMPT_MARKERS if m in text]
            if hits:
                offenders.append(f"{p.relative_to(ROOT)}: {hits}")
    assert not offenders, f"prompt-stencil markers found in public trees: {offenders}"


def _script_flag_mentions(text: str):
    # Every `scripts/<name>.py ... --<flag>` on one line. Flags are collected
    # per line so a row like "(--status, --apply, --dry-run)" is fully checked.
    for line in text.splitlines():
        for m in re.finditer(r"scripts/([a-z_]+\.py)", line):
            flags = re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", line[m.end():])
            for flag in flags:
                yield m.group(1), flag


def test_documented_script_flags_exist_in_scripts():
    missing = []
    for doc in (README_FILE, RUNBOOK_FILE):
        for script, flag in _script_flag_mentions(doc.read_text(encoding="utf-8")):
            path = SCRIPTS_DIR / script
            if not path.is_file():
                missing.append(f"{doc.name}: scripts/{script} does not exist")
                continue
            if flag not in path.read_text(encoding="utf-8"):
                missing.append(f"{doc.name}: scripts/{script} does not accept {flag}")
    assert not missing, f"docs mention script flags the scripts do not have: {missing}"


def test_runbook_log_prefixes_exist_in_worker():
    assert RUNBOOK_FILE.is_file(), "docs/RUNBOOK.md is missing"
    quoted = sorted(set(re.findall(r"\[rt(?:-[a-z]+)*\]", RUNBOOK_FILE.read_text(encoding="utf-8"))))
    assert quoted, "RUNBOOK.md should point operators at at least one [rt-...] log prefix"
    worker_src = "\n".join(p.read_text(encoding="utf-8") for p in sorted(WORKER_DIR.glob("*.py")))
    ghosts = [pre for pre in quoted if pre not in worker_src]
    assert not ghosts, f"RUNBOOK.md quotes log prefixes no worker module prints: {ghosts}"


# ---------------------------------------------------------------------------
# Round-3 drift checks. The operator table once listed a script before it
# existed, and the runbook described an erase with no way back.
# ---------------------------------------------------------------------------


def _operator_table_scripts(readme_text: str) -> list[str]:
    # Rows of the "Operator CLI Tooling" table: | [`name.py`](./worker/scripts/name.py) | ... |
    m = re.search(r"(?ms)^## Operator CLI Tooling\n(.*?)^## ", readme_text)
    assert m, "README.md needs an '## Operator CLI Tooling' section"
    return re.findall(r"\]\(\./worker/scripts/([A-Za-z0-9_]+\.py)\)", m.group(1))


def test_readme_operator_table_scripts_exist():
    names = _operator_table_scripts(README_FILE.read_text(encoding="utf-8"))
    assert names, "operator table lists no scripts/*.py"
    assert "scrub_rules.py" in names, "operator table must list scripts/scrub_rules.py"
    ghosts = [n for n in names if not (SCRIPTS_DIR / n).is_file()]
    assert not ghosts, f"README operator table lists scripts that do not exist: {ghosts}"


def test_runbook_documents_restore_after_erase():
    text = RUNBOOK_FILE.read_text(encoding="utf-8")
    assert "rt_restore_caller" in text, "RUNBOOK.md must tell operators how to undo an erase (rt_restore_caller)"
    assert re.search(r"(?m)^##+ .*undo an erase", text, re.I), "RUNBOOK.md needs an 'Undo an erase' section"
