"""test_f_g8_docs.py — Contract tests for hardening group G8 (docs).

Findings covered:
  #22  sites/system_prompt_viewer.html moves to docs/internal/ and README points at the new home.
  #25  CODEOWNERS, RUNBOOK, five ADRs, and README content fixes (no hard-coded test count,
       RT_REQUIRE_PEPPER in the security section, autoheal in the restart sentence, migrations
       catalog lists 10-reserved/18/19/20, module index complete, /ready + two ports documented),
       plus the new checks that tests/test_doc_drift.py must gain.

These are pure filesystem/text tests: no network, no DB, no agent imports.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
WORKER_DIR = ROOT / "worker"
README = ROOT / "README.md"
DOCS = ROOT / "docs"
ADR_DIR = DOCS / "adr"
SITES = ROOT / "sites"
CODEOWNERS = ROOT / ".github" / "CODEOWNERS"
VIEWER_NEW = DOCS / "internal" / "system_prompt_viewer.html"
VIEWER_OLD = SITES / "system_prompt_viewer.html"
DOC_DRIFT_TEST = Path(__file__).resolve().parent / "test_doc_drift.py"

ADR_FILES = [
    "0001-host-networking.md",
    "0002-phone-hash-pepper.md",
    "0003-fail-closed-guards.md",
    "0004-tcpa-calling-windows.md",
    "0005-email-recipient-policy.md",
]
ADR_HEADINGS = ["Status", "Context", "Decision", "Consequences"]
NEW_MIGRATIONS = [
    "10-reserved.sql",
    "18-pin-search-path.sql",
    "19-forget-me-soft-delete.sql",
    "20-job-caps-and-retention.sql",
]


def _readme() -> str:
    assert README.exists(), "README.md missing at repo root"
    return README.read_text(encoding="utf-8")


def _section(text: str, heading_pattern: str) -> str:
    """Return the body of the first level-2 (## ) section whose heading matches heading_pattern."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and re.search(heading_pattern, line, re.I):
            start = i
            break
    if start is None:
        pytest.fail(f"README has no '## ' section whose heading matches /{heading_pattern}/i")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def _has_heading(text: str, word: str) -> bool:
    return re.search(rf"(?im)^#{{1,6}}\s*{re.escape(word)}\b", text) is not None


# ---------------------------------------------------------------------------
# #22 — system prompt viewer relocated out of sites/ into docs/internal/
# ---------------------------------------------------------------------------

def test_f22_viewer_removed_from_sites():
    assert not VIEWER_OLD.exists(), (
        "sites/system_prompt_viewer.html must no longer exist (moved to docs/internal/)"
    )


def test_f22_sites_contains_no_prompt_named_file():
    assert SITES.exists()
    offenders = sorted(
        str(p.relative_to(ROOT)) for p in SITES.rglob("*") if p.is_file() and "prompt" in p.name.lower()
    )
    assert not offenders, f"sites/ must not ship any file whose name contains 'prompt': {offenders}"


def test_f22_viewer_lives_in_docs_internal():
    assert VIEWER_NEW.exists(), "docs/internal/system_prompt_viewer.html must exist"
    body = VIEWER_NEW.read_text(encoding="utf-8")
    assert "<html" in body.lower(), "moved viewer must be the real HTML page, not an empty stub"
    assert "system prompt" in body.lower(), "moved viewer must still be the system-prompt viewer page"


def test_f22_readme_references_new_viewer_location():
    readme = _readme()
    assert "sites/system_prompt_viewer.html" not in readme, (
        "README must not point at the old sites/ location of the viewer"
    )
    assert "docs/internal/system_prompt_viewer.html" in readme, (
        "README must reference the viewer at docs/internal/system_prompt_viewer.html"
    )


# ---------------------------------------------------------------------------
# #25 — CODEOWNERS
# ---------------------------------------------------------------------------

def test_f25_codeowners_exists_with_richie_iris():
    assert CODEOWNERS.exists(), ".github/CODEOWNERS must exist"
    rules = [
        ln.strip() for ln in CODEOWNERS.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert any(re.fullmatch(r"\*\s+@richie-iris", r) for r in rules), (
        f"CODEOWNERS must contain the rule '* @richie-iris'; got rules={rules}"
    )


# ---------------------------------------------------------------------------
# #25 — RUNBOOK
# ---------------------------------------------------------------------------

def test_f25_runbook_silent_line_section_check_order():
    runbook = DOCS / "RUNBOOK.md"
    assert runbook.exists(), "docs/RUNBOOK.md must exist"
    text = runbook.read_text(encoding="utf-8")
    lines = text.splitlines()

    heading_idx = None
    heading_level = 0
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m and re.search(r"silent line at 3\s*am", m.group(2), re.I):
            heading_idx, heading_level = i, len(m.group(1))
            break
    assert heading_idx is not None, "RUNBOOK.md needs a 'Silent line at 3am' heading"

    end = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+", lines[j])
        if m and len(m.group(1)) <= heading_level:
            end = j
            break
    section = "\n".join(lines[heading_idx:end])

    # Check order: carrier -> LiveKit SIP -> worker /ready -> Supabase -> Gemini
    order = [r"carrier", r"livekit\s+sip", r"/ready", r"supabase", r"gemini"]
    positions = []
    for pat in order:
        m = re.search(pat, section, re.I)
        assert m, f"'Silent line at 3am' section must mention /{pat}/i"
        positions.append(m.start())
    assert positions == sorted(positions) and len(set(positions)) == len(positions), (
        "check order must be carrier -> LiveKit SIP -> worker /ready -> Supabase -> Gemini; "
        f"first-mention offsets were {dict(zip(order, positions))}"
    )


# ---------------------------------------------------------------------------
# #25 — ADRs
# ---------------------------------------------------------------------------

def test_f25_adr_directory_has_at_least_five_records():
    assert ADR_DIR.exists(), "docs/adr/ must exist"
    md = sorted(p.name for p in ADR_DIR.glob("*.md"))
    assert len(md) >= 5, f"docs/adr must hold >= 5 ADR files, found {md}"


@pytest.mark.parametrize("name", ADR_FILES)
def test_f25_adr_exists_with_required_headings(name: str):
    path = ADR_DIR / name
    assert path.exists(), f"docs/adr/{name} must exist"
    text = path.read_text(encoding="utf-8")
    missing = [h for h in ADR_HEADINGS if not _has_heading(text, h)]
    assert not missing, f"docs/adr/{name} is missing markdown headings: {missing}"


def test_f25_adr_0002_pepper_is_one_way_door():
    path = ADR_DIR / "0002-phone-hash-pepper.md"
    assert path.exists(), "docs/adr/0002-phone-hash-pepper.md must exist"
    text = path.read_text(encoding="utf-8")
    assert re.search(r"one[\s-]way door", text, re.I), "ADR 0002 must state the pepper is a one-way door"
    assert "RT_PHONE_HASH_PEPPER" in text, "ADR 0002 must name RT_PHONE_HASH_PEPPER"


def test_f25_adr_0003_guards_fail_closed_telemetry_fails_open():
    path = ADR_DIR / "0003-fail-closed-guards.md"
    assert path.exists(), "docs/adr/0003-fail-closed-guards.md must exist"
    text = path.read_text(encoding="utf-8")
    assert re.search(r"fail[\s-]+closed", text, re.I), "ADR 0003 must state guards fail closed"
    assert re.search(r"telemetry", text, re.I), "ADR 0003 must mention telemetry"
    assert re.search(r"fails?[\s-]+open", text, re.I), "ADR 0003 must state telemetry fails open"


# ---------------------------------------------------------------------------
# #25 — README content
# ---------------------------------------------------------------------------

def test_f25_readme_has_no_hardcoded_test_count():
    readme = _readme()
    hits = re.findall(r"\b\d+\+? tests\b", readme)
    assert not hits, f"README must not hard-code a unit test count; found {hits}"


def test_f25_readme_security_section_requires_pepper_in_prod():
    section = _section(_readme(), r"security")
    assert "RT_REQUIRE_PEPPER" in section, "README security section must mention RT_REQUIRE_PEPPER"
    assert re.search(r"\brequired\b", section, re.I), (
        "README security section must say the HMAC pepper is REQUIRED (via RT_REQUIRE_PEPPER)"
    )
    assert re.search(r"\bprod", section, re.I), "README security section must tie the requirement to prod lanes"


def test_f25_readme_restart_sentence_mentions_autoheal():
    readme = _readme()
    restart_lines = [ln for ln in readme.splitlines() if re.search(r"restart", ln, re.I)]
    assert restart_lines, "README must still describe automatic container restart"
    assert any("autoheal" in ln.lower() for ln in restart_lines), (
        "the README sentence about automatic container restart must mention autoheal; "
        f"restart lines were: {restart_lines}"
    )


def test_f25_readme_migrations_catalog_lists_new_migrations():
    section = _section(_readme(), r"migration")
    missing = [m for m in NEW_MIGRATIONS if m not in section]
    assert not missing, f"README migrations catalog must list {missing}"


def test_f25_readme_module_index_documents_every_worker_module():
    readme = _readme()
    modules = sorted(p.name for p in WORKER_DIR.glob("*.py"))
    assert "rt_cadence.py" in modules
    undocumented = [m for m in modules if m not in readme]
    assert not undocumented, f"README module index must document every worker module; missing {undocumented}"
    assert "rt_cadence.py" in readme


def test_f25_readme_health_section_documents_ready_and_two_ports():
    readme = _readme()
    assert "/ready" in readme, "README must document the /ready endpoint"
    assert re.search(r"\b503\b|degraded", readme, re.I), (
        "README must describe /ready semantics (200 when all checks pass, 503/degraded otherwise)"
    )
    assert "8080" in readme, "README must document the Docker health port 8080"
    assert "8082" in readme, "README must document the LiveKit worker HTTP port 8082"


# ---------------------------------------------------------------------------
# #25 — tests/test_doc_drift.py gains the new drift checks
# ---------------------------------------------------------------------------

def test_f25_doc_drift_suite_gains_new_checks():
    src = DOC_DRIFT_TEST.read_text(encoding="utf-8")
    missing = []
    if r"\d+\+? tests\b" not in src:
        missing.append("hard-coded test count regex r\"\\b\\d+\\+? tests\\b\"")
    if "RT_REQUIRE_PEPPER" not in src:
        missing.append("README mentions RT_REQUIRE_PEPPER")
    if "adr" not in src:
        missing.append("docs/adr has >= 5 files")
    if "CODEOWNERS" not in src:
        missing.append(".github/CODEOWNERS exists")
    if "prompt" not in src:
        missing.append("sites/ contains no file whose name contains 'prompt'")
    assert not missing, f"tests/test_doc_drift.py must gain these checks: {missing}"


# ---------------------------------------------------------------------------
# Round 2 — verifier-refuted claims, each pinned to the code fact it must match.
# ---------------------------------------------------------------------------

RUNBOOK = DOCS / "RUNBOOK.md"
RETENTION = DOCS / "RETENTION.md"
COMPOSE = ROOT / "deploy" / "docker-compose.yml"
ENV_EXAMPLE = WORKER_DIR / ".env.example"
AGENT_PY = WORKER_DIR / "agent.py"
DEPLOY = ROOT / "deploy"


def _compose_services() -> list[str]:
    text = COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^services:\n(.*?)^(?:volumes|networks):", text)
    assert m, "docker-compose.yml must have a services: block"
    return re.findall(r"(?m)^  ([a-z_-]+):\s*$", m.group(1))


# --- #25 README ------------------------------------------------------------

def test_f25r2_readme_has_no_yahoo_finance_service_row():
    section = _section(_readme(), r"external services")
    rows = [ln for ln in section.splitlines() if ln.startswith("|")]
    assert not any(re.search(r"\*\*yahoo", ln, re.I) for ln in rows), (
        "README External Services must not list Yahoo Finance as a service: agent.py makes no direct call"
    )
    assert re.search(r"no direct call", section, re.I), (
        "README should say grounded search names its own source and the worker makes no direct finance-API call"
    )


def test_f25r2_readme_precommit_sentence_matches_hook_config():
    section = _section(_readme(), r"ci/cd")
    assert re.search(r"`ruff` lint", section), "README must say pre-commit runs ruff LINT"
    assert re.search(r"ruff-format.*manual", section, re.I | re.S), (
        "README must say ruff-format is a manual-stage hook, not run on commit"
    )
    assert not re.search(r"runs `ruff` format checks", section), "stale 'ruff format checks' sentence must go"


def test_f25r2_readme_diagram_uses_compose_service_names():
    services = _compose_services()
    assert len(services) == 7, f"compose must define seven services, found {services}"
    for name in ("livekit", "sip", "autoheal"):
        assert name in services
    section = _section(_readme(), r"architecture overview")
    for name in services:
        assert re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", section), (
            f"architecture diagram must name compose service `{name}`"
        )
    assert "livekit-sip" not in section and "livekit-server`" not in section, (
        "diagram must use the compose service names (livekit, sip), not image names"
    )
    assert re.search(r"seven", section, re.I), "architecture diagram must say the stack is seven services"
    assert "network_mode: none" in section, "diagram must call out autoheal on network_mode: none"


def test_f25r2_readme_infra_line_and_autoheal_network_mode():
    section = _section(_readme(), r"infrastructure stack")
    assert re.search(r"redis → livekit → sip → caddy → worker → scheduler", section), (
        "infra chain must use compose service names in compose order"
    )
    assert "network_mode: none" in section
    compose = COMPOSE.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^  autoheal:\n(.*?)(?=^  [a-z_-]+:|^volumes:)", compose)
    assert m and "network_mode: none" in m.group(1), "compose autoheal must run with network_mode: none"


def test_f25r2_restart_count_claims_use_docker_inspect():
    for doc in (README, RUNBOOK):
        text = doc.read_text(encoding="utf-8")
        # Markdown wraps sentences, so scope each claim to its paragraph.
        for para in re.split(r"\n\s*\n", text):
            if re.search(r"restart count", para, re.I):
                assert "docker inspect" in para or "RestartCount" in para, (
                    f"{doc.name}: a restart-count claim must point at docker inspect, got: {para.strip()[:200]}"
                )
        assert "RestartCount" in text, f"{doc.name} must show the docker inspect --format '{{{{.RestartCount}}}}' form"
        assert not re.search(r"`docker compose ps`[^\n]*restart count", text), (
            f"{doc.name} must not claim `docker compose ps` shows the restart count"
        )


# --- #16 README security ----------------------------------------------------

def test_f16_readme_security_google_key_restriction_with_console_link():
    section = _section(_readme(), r"security")
    assert re.search(r"GOOGLE_API_KEY", section), "security section must name GOOGLE_API_KEY"
    assert re.search(r"restrict", section, re.I), "security section must say the key must be restricted"
    assert re.search(r"\bAPI\b", section) and re.search(r"\bIP\b", section), (
        "security section must say the key is restricted by API and by IP"
    )
    assert "https://console.cloud.google.com/apis/credentials" in section, (
        "security section must link the Google Cloud console credentials page"
    )


# --- #25 RUNBOOK --------------------------------------------------------------

def test_f25r2_runbook_check_migrations_lane_is_positional():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "check_migrations.py --lane" not in text, "check_migrations.py takes the lane positionally, not --lane"
    assert re.search(r"check_migrations\.py <lane>", text), "RUNBOOK must show `check_migrations.py <lane>`"
    script = (WORKER_DIR / "scripts" / "check_migrations.py").read_text(encoding="utf-8")
    assert "--lane" not in script and "sys.argv[1:]" in script


def test_f25r2_runbook_quotes_only_real_log_prefixes():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "[rt-live]" not in text, "no worker module prints [rt-live]"
    worker_src = "\n".join(p.read_text(encoding="utf-8") for p in sorted(WORKER_DIR.glob("*.py")))
    for pre in ("[rt]", "[rt-guard]", "[rt-bridge]", "[rt-scheduler]"):
        assert pre in text, f"RUNBOOK must point operators at {pre}"
        assert pre in worker_src, f"{pre} must actually be printed by a worker module"
    for pre in set(re.findall(r"\[rt(?:-[a-z]+)*\]", text)):
        assert pre in worker_src, f"RUNBOOK quotes {pre} but no worker module prints it"


def test_f25r2_runbook_reset_caller_usage_matches_script():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "reset_caller.py --wipe" not in text, "reset_caller.py has no --wipe flag"
    assert re.search(r"reset_caller\.py <number>", text), "RUNBOOK must show `reset_caller.py <number>`"
    assert "--test" in text
    script = (WORKER_DIR / "scripts" / "reset_caller.py").read_text(encoding="utf-8")
    assert "--wipe" not in script and '"--test"' in script


def test_f25r2_runbook_pepper_section_names_boot_failure_and_ready_check():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "missing_config()" in text, "RUNBOOK must say config.missing_config() lists the pepper (boot fails loudly)"
    assert re.search(r"`pepper` readiness check", text), "RUNBOOK must name the `pepper` readiness check"
    assert "[rt-startup]" in text


# --- ADRs ---------------------------------------------------------------------

def test_f25r2_adr_0001_names_seven_services_and_autoheal_network_none():
    text = (ADR_DIR / "0001-host-networking.md").read_text(encoding="utf-8")
    assert "seven" in text.lower() and "six services" not in text.lower()
    for name in _compose_services():
        assert f"`{name}`" in text, f"ADR 0001 must list compose service `{name}`"
    assert "network_mode: none" in text


def test_f25r2_adr_0002_dev_lane_guidance_matches_env_example():
    text = (ADR_DIR / "0002-phone-hash-pepper.md").read_text(encoding="utf-8")
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert re.search(r"RT_REQUIRE_PEPPER[^\n]*every deploy lane sets it", env), (
        "worker/.env.example must still say every deploy lane sets RT_REQUIRE_PEPPER"
    )
    assert re.search(r"every deploy lane", text, re.I), "ADR 0002 must say every deploy lane sets RT_REQUIRE_PEPPER=1"
    assert "RT_REQUIRE_PEPPER=1" in text
    assert "Dev lanes may run" not in text, "stale 'dev lanes may run without the requirement' wording must go"
    assert re.search(r"WARNING", text), "ADR 0002 must describe the unpeppered warning for local checkouts"
    assert "config.pepper_required()" in text and "`pepper` readiness check" in text


def test_f25r2_adr_0003_documented_exceptions_section():
    text = (ADR_DIR / "0003-fail-closed-guards.md").read_text(encoding="utf-8")
    assert _has_heading(text, "Documented exceptions"), "ADR 0003 needs a 'Documented exceptions' heading"
    body = text.split("## Documented exceptions", 1)[1].split("## Consequences", 1)[0]
    assert re.search(r"minute budget", body, re.I) and re.search(r"best[- ]effort", body, re.I)
    assert re.search(r"finding #5", body, re.I), "minute-budget exception must cite finding #5 as pending"
    assert re.search(r"bridge[- ]ledger", body, re.I) and re.search(r"cross-session race", body, re.I)
    assert re.search(r"accepted residual", body, re.I)
    # Both exceptions must describe real code paths.
    agent = AGENT_PY.read_text(encoding="utf-8")
    assert "admitting the call uncapped" in agent
    assert "check_and_record_dial" in (WORKER_DIR / "rt_bridge.py").read_text(encoding="utf-8")


def test_f25r2_adr_0005_send_email_gated_by_verified_email_kwarg():
    text = (ADR_DIR / "0005-email-recipient-policy.md").read_text(encoding="utf-8")
    assert "verified_email=" in text, "ADR 0005 must show the verified_email kwarg on rt_email.send_email"
    assert "rt_email.send_email" in text
    assert "recipient not verified" in text, "ADR 0005 must quote the refusal message"
    assert "is_allowed_recipient(to_email, verified_email)" in text


# --- RETENTION.md ---------------------------------------------------------------

def test_f25r2_retention_doc_exists_and_matches_code():
    assert RETENTION.exists(), "docs/RETENTION.md must exist"
    text = RETENTION.read_text(encoding="utf-8")
    for needle in ("rt.calls.transcript", "rt.call_events", "rt.callers.last_transcript",
                   "50 MB x 5", "Sentry", "RT_TRANSCRIPT_RETENTION_DAYS",
                   "rt_purge_old_transcripts", "rt_purge_forgotten", "rt_restore_caller",
                   "rt.forgotten_archive"):
        assert needle in text, f"RETENTION.md must mention {needle}"
    assert re.search(r"default \*\*30\*\*|default 30", text), "RETENTION.md must state the 30-day default"
    assert re.search(r"hourly|3600", text), "RETENTION.md must describe the hourly purge"
    assert re.search(r"24[- ]hour", text), "RETENTION.md must describe the 24h forget-me archive"
    # Pin the numbers to their sources.
    compose = COMPOSE.read_text(encoding="utf-8")
    assert 'max-size: "50m"' in compose and 'max-file: "5"' in compose
    assert "RT_TRANSCRIPT_RETENTION_DAYS=30" in ENV_EXAMPLE.read_text(encoding="utf-8")
    sched = (WORKER_DIR / "rt_scheduler.py").read_text(encoding="utf-8")
    assert "_HOUSEKEEPING_EVERY_S = 3600" in sched
    assert "interval '24 hours'" in (WORKER_DIR / "sql" / "19-forget-me-soft-delete.sql").read_text(encoding="utf-8")
    assert "docs/RETENTION.md" in _readme(), "README must link docs/RETENTION.md"


# --- #22 viewer -------------------------------------------------------------------

def test_f22r2_viewer_badges_match_code():
    body = VIEWER_NEW.read_text(encoding="utf-8")
    env = ENV_EXAMPLE.read_text(encoding="utf-8")
    model = re.search(r"(?m)^GEMINI_LIVE_MODEL=(\S+)", env).group(1)
    assert f"Model: {model}" in body, f"viewer model badge must read the .env.example model ({model})"
    assert "Gemini 2.0" not in body, "stale Gemini 2.0 badge must go"
    assert "20m/30m/35m" in body, "cadence badge must read 20m/30m/35m"
    assert "15m/25m/35m" not in body
    n_tools = AGENT_PY.read_text(encoding="utf-8").count("@function_tool")
    assert n_tools >= 15
    assert f"Active Function Tools ({n_tools})" in body, (
        f"viewer tool count must equal the number of @function_tool in agent.py ({n_tools})"
    )
    assert "manage_goals" in body, "viewer tool registry must list manage_goals"
    assert "<div clas " not in body, "broken `<div clas` tag must be repaired"


def test_f22r2_viewer_header_notes_repo_visibility_and_git_history():
    body = VIEWER_NEW.read_text(encoding="utf-8")
    head = body.split("<!-- Tools Card -->", 1)[0]
    assert re.search(r"repo-visible", head, re.I), "viewer header must say the tree is repo-visible"
    assert re.search(r"not access-controlled", head, re.I)
    assert re.search(r"git history", head, re.I) and "sites/system_prompt_viewer.html" in head, (
        "viewer header must note git history retains the old sites/ copy"
    )


# --- test_doc_drift.py gains the round-2 checks ---------------------------------

def test_f22r2_doc_drift_suite_gains_marker_flag_and_prefix_checks():
    src = DOC_DRIFT_TEST.read_text(encoding="utf-8")
    missing = []
    if "HONEST LIMITS" not in src or "# WHO YOU ARE" not in src:
        missing.append("sites/ and deploy/ contain no 'HONEST LIMITS' / '# WHO YOU ARE' markers")
    if "deploy" not in src:
        missing.append("marker check covers deploy/")
    if not re.search(r"scripts/\(\[a-z_\]\+\\\.py\)", src):
        missing.append("every scripts/<name>.py --<flag> mention corresponds to a flag in the script")
    if r"\[rt(?:-[a-z]+)*\]" not in src:
        missing.append("every [rt-...] prefix quoted in RUNBOOK appears in some worker/*.py")
    assert not missing, f"tests/test_doc_drift.py must gain these checks: {missing}"


def test_f22r2_no_prompt_markers_under_sites_or_deploy():
    offenders = []
    for base in (SITES, DEPLOY):
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "HONEST LIMITS" in text or "# WHO YOU ARE" in text:
                offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"prompt stencil markers must not appear under sites/ or deploy/: {offenders}"


# ---------------------------------------------------------------------------
# Round 3 — re-verification refuted round 2. Each claim below is pinned to the
# code that makes it true, so the docs cannot outrun the worker again.
# ---------------------------------------------------------------------------

ADR_0005 = ADR_DIR / "0005-email-recipient-policy.md"
RT_EMAIL_PY = WORKER_DIR / "rt_email.py"
RT_SHIELD_PY = WORKER_DIR / "rt_shield.py"
RT_HYDRATOR_PY = WORKER_DIR / "rt_hydrator.py"
CONFIG_PY = WORKER_DIR / "config.py"
SQL_PUSH_PY = WORKER_DIR / "sql_push.py"
BOOTSTRAP_PY = WORKER_DIR / "scripts" / "bootstrap_test_db.py"
SCRUB_RULES_PY = WORKER_DIR / "scripts" / "scrub_rules.py"
RENDER_CONFIG_SH = DEPLOY / "provisioning" / "render-config.sh"


def _paragraph_with(text: str, needle: str) -> str:
    for para in re.split(r"\n\s*\n", text):
        if needle in para:
            return para
    pytest.fail(f"no paragraph contains {needle!r}")


# --- #1 ADR 0005 describes the real rule ----------------------------------------

def test_r3_adr_0005_recipient_rule_is_caller_line_not_readback():
    text = ADR_0005.read_text(encoding="utf-8")
    assert "`caller:`" in text, "ADR 0005 must say the address must appear on a caller: transcript line"
    assert "email_spoken_by_caller" in text, "ADR 0005 must name rt_shield.email_spoken_by_caller"
    assert "save_email" in text and "rt_set_caller_email" in text
    assert not re.search(r"reading it back", text), "no read-back confirmation flow exists; the claim must go"
    assert "no read-back confirmation flow" in text
    # The old 'third-party numbers are rejected' implied a recipient argument; there is none.
    assert "no recipient parameter" in text
    assert "Third-party numbers are rejected" not in text


def test_r3_adr_0005_caller_line_rule_matches_code():
    shield = RT_SHIELD_PY.read_text(encoding="utf-8")
    agent = AGENT_PY.read_text(encoding="utf-8")
    assert "def email_spoken_by_caller(" in shield
    assert 'startswith("caller:")' in shield or "_caller_lines(" in shield
    # save_email refuses an address the caller never said, before the RPC write.
    save = agent.split("async def save_email(", 1)[1].split("@function_tool", 1)[0]
    assert save.index("_email_spoken_by_caller(") < save.index("rpc/rt_set_caller_email")
    assert "def _email_spoken_by_caller(" in (WORKER_DIR / "rt_postcall_worker.py").read_text(encoding="utf-8")
    # send_sms takes no recipient.
    m = re.search(r"async def send_sms\(self, context: RunContext, ([^)]*)\)", agent)
    assert m and "to" not in [a.split(":")[0].strip() for a in m.group(1).split(",")]


def test_r3_adr_0005_output_encoding_and_idempotency_claims():
    text = ADR_0005.read_text(encoding="utf-8")
    assert "html.escape(body_text" in text, "ADR 0005 must say the body is html-escaped in the HTML part"
    assert "html.escape(subject" not in text, "the subject never enters the HTML part; do not claim it is escaped"
    assert "RFC 5545" in text and "CR/LF" in text, "ADR 0005 must describe the ICS escaping rule"
    assert "`ORGANIZER`" in text
    assert "Idempotency-Key" in text and "uuid4" in text and "retries=0" in text


def test_r3_adr_0005_encoding_claims_match_rt_email():
    src = RT_EMAIL_PY.read_text(encoding="utf-8")
    assert "html.escape(body_text" in src, "rt_email must html-escape the model-written body in the HTML part"
    assert "Idempotency-Key" in src, "rt_email.send_email must send an Idempotency-Key header"
    assert "uuid4" in src
    assert "retries=0" in src, "rt_email.send_email must not retry the POST"
    assert "ORGANIZER" in src, "generate_ics must emit a hard-coded ORGANIZER"


def test_r3_adr_0005_refusal_telemetry_claim_matches_code():
    text = ADR_0005.read_text(encoding="utf-8")
    src = RT_EMAIL_PY.read_text(encoding="utf-8")
    assert 'guard="send_email"' in text and "recipient_not_verified" in text
    assert 'guard="send_email"' in src and 'reason="recipient_not_verified"' in src
    assert 'guard="send_sms"' not in text, "nothing emits guard=send_sms; the claim must go"


# --- #6 RUNBOOK ------------------------------------------------------------------

def test_r3_runbook_pepper_boot_message_matches_config():
    text = RUNBOOK.read_text(encoding="utf-8")
    para = _paragraph_with(text, "STARTUP CONFIGURATION ERRORS")
    assert "[config]" in para and "RT_PHONE_HASH_PEPPER" in para
    assert "validate_startup_config" in para
    assert re.search(r"does not\s+exit", para), "RUNBOOK must say validate_startup_config does not exit"
    assert "exits at boot" not in text, "config.validate_startup_config never exits; stale wording must go"
    assert re.search(r"raises? `RuntimeError`\s+at import", para), (
        "RUNBOOK must say prod-named agents raise RuntimeError at import"
    )
    assert "PROD_AGENT_NAMES" in para
    cfg = CONFIG_PY.read_text(encoding="utf-8")
    assert "STARTUP CONFIGURATION ERRORS" in cfg and "does not exit" in cfg
    assert 'missing.append("RT_PHONE_HASH_PEPPER")' in cfg
    agent = AGENT_PY.read_text(encoding="utf-8")
    guard = agent.split("def _assert_lane_is_declared(", 1)[1].split("\n_assert_lane_is_declared()", 1)[0]
    assert "RT_PHONE_HASH_PEPPER" in guard and "raise RuntimeError" in guard
    assert "\n_assert_lane_is_declared()" in agent, "the lane guard must run at import"


def test_r3_runbook_undo_erase_section():
    text = RUNBOOK.read_text(encoding="utf-8")
    m = re.search(r"(?ms)^## Undo an erase within 24h\n(.*?)(?=^## |\Z)", text)
    assert m, "RUNBOOK needs an '## Undo an erase within 24h' section"
    body = m.group(1)
    assert "SELECT rt_restore_caller('<hash>')" in body
    assert re.search(r"service.role", body, re.I)
    assert "scripts/reset_caller.py" in body and "phone_hash(" in body
    assert ".env.local" in body and "deploy/worker.env" in body
    assert "newest" in body
    # The hash recipe must match what reset_caller.py actually loads, in order.
    script = (WORKER_DIR / "scripts" / "reset_caller.py").read_text(encoding="utf-8")
    assert 'ROOT / ".env.local", ROOT.parent / "deploy" / "worker.env"' in script
    assert "from rt_prefs import phone_hash" in script
    sql21 = (WORKER_DIR / "sql" / "21-forget-atomic-restore-newest-retention-all.sql").read_text(encoding="utf-8")
    assert "GRANT EXECUTE ON FUNCTION public.rt_restore_caller(TEXT)       TO service_role" in sql21
    assert "RETURNS INT" in sql21.split("rt_restore_caller(p_hash TEXT)", 1)[1][:200]


# --- #8 sql_push / bootstrap / scrub_rules -------------------------------------------

def test_r3_readme_and_runbook_document_prod_sql_guard():
    for doc in (README, RUNBOOK):
        text = doc.read_text(encoding="utf-8")
        paras = [pp for pp in re.split(r"\n\s*\n", text) if "--i-know-this-is-prod" in pp]
        assert paras, f"{doc.name}: must document the --i-know-this-is-prod override"
        # One paragraph must carry the whole rule, not fragments spread over the doc.
        assert any(
            "sql_push.py" in pp and "--confirm <ref>" in pp and re.search(r"read-only", pp)
            and "bootstrap_test_db.py" in pp and "refuses prod refs" in pp
            for pp in paras
        ), f"{doc.name}: no single paragraph says sql_push is read-only on prod without --confirm <ref> --i-know-this-is-prod and bootstrap_test_db.py refuses prod refs"
        assert "wipe" in "".join(paras).lower(), f"{doc.name}: must say a wipe is never allowed on prod"
        # G6's sql_push must actually spell the flag, or the docs are advertising a no-op.
        assert 'PROD_ACK_FLAG = "--i-know-this-is-prod"' in SQL_PUSH_PY.read_text(encoding="utf-8")


def test_r3_prod_sql_guard_claims_match_scripts():
    sp = SQL_PUSH_PY.read_text(encoding="utf-8")
    assert "--i-know-this-is-prod" in sp, "sql_push.py must accept --i-know-this-is-prod"
    assert "--confirm" in sp
    boot = BOOTSTRAP_PY.read_text(encoding="utf-8")
    assert re.search(r"prod", boot, re.I), "bootstrap_test_db.py must refuse prod refs"
    assert "--i-know-this-is-prod" not in boot, "bootstrap_test_db.py has no prod override"


def test_r3_readme_operator_table_lists_scrub_rules():
    section = _section(_readme(), r"operator cli")
    row = next((ln for ln in section.splitlines() if "scrub_rules.py" in ln and ln.startswith("|")), None)
    assert row, "README operator table must have a scrub_rules.py row"
    assert "./worker/scripts/scrub_rules.py" in row
    assert "rule_text_allowed" in row
    assert SCRUB_RULES_PY.is_file(), "scripts/scrub_rules.py must exist"


# --- #20 render-config forwards the pepper ---------------------------------------------

def test_r3_readme_provisioning_render_config_pepper():
    section = _section(_readme(), r"standing up a lane")
    row = next((ln for ln in section.splitlines() if "render-config.sh" in ln and ln.startswith("|")), None)
    assert row, "provisioning table must have a render-config.sh row"
    assert "RT_PHONE_HASH_PEPPER" in row and "refuses" in row
    sh = RENDER_CONFIG_SH.read_text(encoding="utf-8")
    assert "RT_PHONE_HASH_PEPPER" in sh, "render-config.sh must forward RT_PHONE_HASH_PEPPER"


# --- #3 rules re-validated at render time ------------------------------------------------

def test_r3_readme_security_rules_revalidated_at_render():
    section = _section(_readme(), r"security")
    assert re.search(r"re-validated", section, re.I) and "rule_text_allowed" in section
    assert re.search(r"render", section, re.I)
    assert "def rule_text_allowed(" in RT_SHIELD_PY.read_text(encoding="utf-8")
    assert "rule_text_allowed" in RT_HYDRATOR_PY.read_text(encoding="utf-8"), (
        "rt_hydrator must re-run rule_text_allowed when rendering stored rules/directives"
    )


# --- test_doc_drift.py gains the round-3 checks ----------------------------------------------

def test_r3_doc_drift_suite_gains_script_existence_and_restore_checks():
    src = DOC_DRIFT_TEST.read_text(encoding="utf-8")
    assert "scrub_rules.py" in src and "Operator CLI Tooling" in src, (
        "test_doc_drift.py must check every scripts/<name>.py in the README operator table exists"
    )
    assert "rt_restore_caller" in src, "test_doc_drift.py must check RUNBOOK mentions rt_restore_caller"
