"""CI pipeline contract: the workflow keeps its required steps and the lint
rule set stays strict.  Parses the YAML/TOML rather than grepping so a
reordered or renamed step still counts, but a dropped one fails."""

import pathlib
import tomllib

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "worker" / "pyproject.toml"
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"

# Each entry: every substring must appear in ONE step's `run` script.
REQUIRED_STEPS = {
    "ruff": ("ruff check",),
    "pre-commit": ("pre-commit run --all-files",),
    # Audit runs against the exported lock, not the editable project.
    "pip-audit": ("pip-audit", "-r /tmp/req.txt", "uv export", "--no-emit-project"),
    "pytest": ("pytest tests",),
    "migrations": ("test_migrations.py",),
    "replay": ("replay_suite.py --fixtures",),
}


def _run_scripts():
    wf = yaml.safe_load(CI_YML.read_text())
    steps = []
    for job in wf["jobs"].values():
        for step in job.get("steps", []):
            if "run" in step:
                steps.append(step["run"])
    return steps


def test_ci_yaml_parses_with_one_test_job():
    wf = yaml.safe_load(CI_YML.read_text())
    assert "jobs" in wf and wf["jobs"]
    assert all("steps" in job for job in wf["jobs"].values())


def test_ci_has_every_required_step():
    scripts = _run_scripts()
    for label, needles in REQUIRED_STEPS.items():
        assert any(all(n in s for n in needles) for s in scripts), f"missing CI step: {label} {needles}"


def test_ci_install_uses_the_lock():
    # The uv cache key is derived from uv.lock; an unlocked install would make it lie.
    scripts = _run_scripts()
    assert any("uv sync --extra dev --frozen" in s for s in scripts)
    assert not any("pip install --system -e" in s for s in scripts)


def test_ci_pip_audit_ignore_parser_iterates_lines():
    audit = next(s for s in _run_scripts() if "pip-audit" in s)
    assert "while IFS= read -r line" in audit
    assert "--ignore-vuln" in audit
    assert "for id in $(" not in audit


def test_ruff_rule_set_is_strict():
    cfg = tomllib.loads(PYPROJECT.read_text())
    lint = cfg["tool"]["ruff"]["lint"]
    assert {"E", "F", "W", "B", "S"} <= set(lint["select"])
    assert not {"E722", "F401", "F841"} & set(lint.get("ignore", []))


def test_ruff_version_matches_between_dev_extra_and_pre_commit_hook():
    cfg = tomllib.loads(PYPROJECT.read_text())
    dev = cfg["project"]["optional-dependencies"]["dev"]
    ruff_spec = next(d for d in dev if d.startswith("ruff"))
    hooks = yaml.safe_load(PRE_COMMIT.read_text())
    hook = next(r for r in hooks["repos"] if r["repo"].endswith("ruff-pre-commit"))
    hook_minor = ".".join(hook["rev"].lstrip("v").split(".")[:2])
    assert f">={hook_minor}" in ruff_spec, f"dev extra {ruff_spec!r} vs hook {hook['rev']}"
    assert "<" in ruff_spec, "dev extra must cap ruff so it cannot drift past the hook"
