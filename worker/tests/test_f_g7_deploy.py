"""test_f_g7_deploy.py — Contract tests for hardening group G7: deploy + env templates.

Findings pinned here:
  #4  RT_PHONE_HASH_PEPPER / RT_REQUIRE_PEPPER in all four env templates, parsed the
      way the code parses them (python-dotenv): an inline comment on an EMPTY value
      is read as the value, so `RT_PHONE_HASH_PEPPER=   # REQUIRED` was a public pepper
  #9  compose worker healthcheck -> /ready, autoheal sidecar + labels, health env docs
  #10 every image in docker-compose.yml and the Dockerfile carries an explicit
      version tag (no :latest, no bare image); dependabot covers docker/pip/actions
  #11 Dockerfile runs as a non-root uid 10001 user and its HEALTHCHECK hits /ready;
      worker + scheduler are read_only / tmpfs / cap_drop / no-new-privileges / limited
  #20 worker/.env.example hygiene (no real numbers, no real ref, new vars documented);
      the same scan over all three templates; env<->code cross-check both ways
  #8  stand-up-lane.sh applies migrations with a real command and counts right,
      through the worker venv (python-dotenv is a worker dependency, not a host one)
  #20 (round 3) render-config.sh forwards the pepper + switch and refuses an empty
      pepper while the switch is on; every lane.env name is read by a provisioning
      script; every worker.env.example name is read by code
  #16 GOOGLE_API_KEY comment tells the operator to restrict the key by API and IP

These are file/config assertions plus one bash run of render-config.sh in OUT_DIR
mode (no ssh, no network, no DB, no agent import).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "worker"
DEPLOY_DIR = ROOT / "deploy"

DOCKERFILE = WORKER_DIR / "Dockerfile"
COMPOSE = DEPLOY_DIR / "docker-compose.yml"
WORKER_ENV = DEPLOY_DIR / "worker.env.example"
LANE_ENV = DEPLOY_DIR / "provisioning" / "lane.env.example"
LOCAL_ENV = WORKER_DIR / ".env.example"
TEST_ENV = WORKER_DIR / ".env.test.example"
ALL_TEMPLATES = [LOCAL_ENV, TEST_ENV, WORKER_ENV, LANE_ENV]
RENDER_CONFIG = DEPLOY_DIR / "provisioning" / "render-config.sh"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
AGENT_PY = WORKER_DIR / "agent.py"


def _rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


# ─── helpers: env templates ───────────────────────────────────────────────────

# Only the NAME= lines, for line numbers. VALUES come from python-dotenv below —
# the parser config.py / migrate.py actually use. The old regex stripped inline
# comments and so certified `KEY=   # REQUIRED` as empty when dotenv reads it as
# the string "# REQUIRED".
_ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_INLINE_ON_EMPTY = re.compile(r"^([A-Z][A-Z0-9_]*)=\s*#")


def _read(p: Path) -> str:
    assert p.exists(), f"{_rel(p)} is missing"
    return p.read_text(encoding="utf-8")


def _env_values(path: Path) -> dict[str, str | None]:
    """What the worker would see: python-dotenv's reading of the template."""
    _read(path)
    return dict(dotenv_values(path))


def _env_entries(path: Path) -> dict[str, tuple[str, int]]:
    """KEY -> (dotenv-parsed value, 0-based line index of the first NAME= line)."""
    values = _env_values(path)
    out: dict[str, tuple[str, int]] = {}
    for i, line in enumerate(_read(path).splitlines()):
        m = _ENV_LINE.match(line)
        if m and m.group(1) not in out and m.group(1) in values:
            out[m.group(1)] = (values[m.group(1)] or "", i)
    return out


def _comment_window(path: Path, key: str, before: int = 12, after: int = 3) -> str:
    """Lower-cased text around KEY's line — where its explanatory comment lives."""
    lines = _read(path).splitlines()
    _, idx = _env_entries(path)[key]
    return "\n".join(lines[max(0, idx - before): idx + after + 1]).lower()


# ─── helpers: Dockerfile ──────────────────────────────────────────────────────

def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, args) with backslash continuations joined and comment lines dropped."""
    out: list[tuple[str, str]] = []
    buf = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        buf += line
        parts = buf.split(None, 1)
        out.append((parts[0].upper(), parts[1] if len(parts) > 1 else ""))
        buf = ""
    return out


def _final_stage_instructions() -> list[tuple[str, str]]:
    instrs = _dockerfile_instructions(_read(DOCKERFILE))
    froms = [i for i, (ins, _) in enumerate(instrs) if ins == "FROM"]
    assert froms, "Dockerfile has no FROM"
    return instrs[froms[-1]:]


def _dockerfile_images() -> list[str]:
    """Every external image the Dockerfile pulls: FROM lines plus COPY/ADD --from=<image>."""
    stages: set[str] = set()
    images: list[str] = []
    for instr, args in _dockerfile_instructions(_read(DOCKERFILE)):
        if instr == "FROM":
            toks = [t for t in args.split() if not t.startswith("--")]
            images.append(toks[0])
            if len(toks) >= 3 and toks[1].upper() == "AS":
                stages.add(toks[2])
        elif instr in ("COPY", "ADD"):
            for tok in args.split():
                if tok.startswith("--from="):
                    ref = tok[len("--from="):]
                    if ref not in stages and not ref.isdigit():
                        images.append(ref)
    return images


# ─── helpers: image refs ──────────────────────────────────────────────────────

_VERSION_TAG = re.compile(r"\d+\.\d+")


def _split_ref(ref: str) -> tuple[str, str | None, str | None]:
    """'ghcr.io/astral-sh/uv:0.8.4' -> ('ghcr.io/astral-sh/uv', '0.8.4', None)."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    head, _, last = ref.rpartition("/")
    if ":" in last:
        name, tag = last.split(":", 1)
    else:
        name, tag = last, None
    repo = f"{head}/{name}" if head else name
    if repo.startswith("docker.io/library/"):
        repo = repo[len("docker.io/library/"):]
    elif repo.startswith("docker.io/"):
        repo = repo[len("docker.io/"):]
    return repo, tag, digest


def _assert_pinned(ref: str, where: str) -> None:
    """An explicit version tag (or a digest). '7-alpine' / '2-alpine' are floating majors, not versions."""
    _, tag, digest = _split_ref(ref)
    if digest:
        return
    assert tag, f"{where}: image {ref!r} has no tag (that is an implicit :latest)"
    assert tag != "latest", f"{where}: image {ref!r} uses :latest"
    assert _VERSION_TAG.search(tag), (
        f"{where}: image {ref!r} tag {tag!r} is not an explicit version "
        f"(want something like 7.4-alpine, v1.9.1, 0.8.4)"
    )


# ─── helpers: docker-compose ──────────────────────────────────────────────────

def _compose() -> dict:
    data = yaml.safe_load(_read(COMPOSE))
    assert isinstance(data, dict) and isinstance(data.get("services"), dict), "docker-compose.yml has no services:"
    return data


def _svc(name: str) -> dict:
    services = _compose()["services"]
    assert name in services, f"docker-compose.yml has no {name!r} service (have: {sorted(services)})"
    return services[name] or {}


def _compose_images() -> dict[str, str]:
    return {
        name: str(svc["image"])
        for name, svc in _compose()["services"].items()
        if isinstance(svc, dict) and svc.get("image")
    }


def _kv_map(v) -> dict[str, str]:
    """labels / environment in either list ('K=V') or mapping form -> {'K': 'V'}."""
    out: dict[str, str] = {}
    if isinstance(v, dict):
        for k, val in v.items():
            out[str(k)] = "" if val is None else str(val)
    else:
        for item in v or []:
            k, _, val = str(item).partition("=")
            out[k] = val
    return out


def _mounts(svc: dict) -> list[tuple[str, str, bool]]:
    """volumes in short or long syntax -> (source, target, read_only)."""
    out: list[tuple[str, str, bool]] = []
    for v in svc.get("volumes") or []:
        if isinstance(v, dict):
            out.append((str(v.get("source", "")), str(v.get("target", "")), bool(v.get("read_only", False))))
        else:
            parts = str(v).split(":")
            src = parts[0]
            tgt = parts[1] if len(parts) > 1 else ""
            opts = parts[2].split(",") if len(parts) > 2 else []
            out.append((src, tgt, "ro" in opts))
    return out


def _healthcheck_cmd(svc: dict, name: str) -> str:
    hc = svc.get("healthcheck") or {}
    test = hc.get("test")
    assert test, f"compose service {name!r} has no healthcheck.test"
    return " ".join(str(t) for t in test) if isinstance(test, list) else str(test)


def _as_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


# ═════════════════════════════════════════════════════════════════════════════
# #4 — pepper documented in every env template; REQUIRED in the lane templates
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=_rel)
def test_f04_pepper_documented_as_one_way_door(path: Path):
    values = _env_values(path)
    assert "RT_PHONE_HASH_PEPPER" in values, f"{_rel(path)}: no RT_PHONE_HASH_PEPPER= line"
    # dotenv's reading, exactly: "" — not None (missing), not "# REQUIRED" (a public key).
    assert values["RT_PHONE_HASH_PEPPER"] == "", (
        f"{_rel(path)}: dotenv reads RT_PHONE_HASH_PEPPER as {values['RT_PHONE_HASH_PEPPER']!r}; a template must ship it exactly empty"
    )
    ctx = _comment_window(path, "RT_PHONE_HASH_PEPPER")
    assert "one-way door" in ctx, f"{_rel(path)}: comment near RT_PHONE_HASH_PEPPER must call it a one-way door"
    assert "once" in ctx, f"{_rel(path)}: comment must say the pepper is set once"
    assert "rotat" in ctx, f"{_rel(path)}: comment must warn never to rotate it"
    assert re.search(r"re-?hash", ctx), f"{_rel(path)}: comment must mention a re-hash plan"


@pytest.mark.parametrize("path", [WORKER_ENV, LANE_ENV, TEST_ENV], ids=_rel)
def test_f04_require_pepper_on_in_lane_templates(path: Path):
    values = _env_values(path)
    assert "RT_REQUIRE_PEPPER" in values, f"{_rel(path)}: no RT_REQUIRE_PEPPER= line"
    assert values["RT_REQUIRE_PEPPER"] == "1", (
        f"{_rel(path)}: dotenv must read RT_REQUIRE_PEPPER as exactly '1' in a lane template, got {values['RT_REQUIRE_PEPPER']!r}"
    )


@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=_rel)
def test_f04_no_inline_comment_on_an_empty_value(path: Path):
    """`KEY=   # note` is KEY="# note" to dotenv: non-empty, so it satisfies every
    'is it set' guard. No template may leave a comment on an empty-valued line, and
    dotenv's reading of every line must be what the eye reads (inline comments after
    a real value are fine — dotenv drops those)."""
    text = _read(path)
    offenders = [f"{path.name}:{i}: {l.rstrip()}" for i, l in enumerate(text.splitlines(), 1) if _INLINE_ON_EMPTY.match(l)]
    assert not offenders, "inline comment on an EMPTY value (dotenv reads the comment as the value):\n" + "\n".join(offenders)
    values = _env_values(path)
    for key, val in values.items():
        assert val is not None, f"{_rel(path)}: {key} has no '=' (dotenv reads None)"
        assert not val.lstrip().startswith("#"), f"{_rel(path)}: dotenv reads {key} as a comment string {val!r}"
    # Every NAME= line's eye-reading (value up to an inline ' #') is dotenv's reading.
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        eye = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        assert values.get(key) == eye, f"{_rel(path)}: {key} reads as {eye!r} but dotenv gives {values.get(key)!r}"


# ═════════════════════════════════════════════════════════════════════════════
# #9 — compose healthcheck on /ready, autoheal sidecar, health env documented
# ═════════════════════════════════════════════════════════════════════════════

def _assert_probe_follows_bind(cmd: str, where: str) -> None:
    """The probe reads RT_DOCKER_HEALTH_HOST/PORT inside the python -c, with rt_health's
    defaults (127.0.0.1 / 8080), and targets /ready. A hardcoded URL passed the old check and
    turned every port move into a permanently unhealthy container."""
    assert "/ready" in cmd, f"{where} must probe /ready, got: {cmd}"
    assert "/health" not in cmd, f"{where} still probes the liveness endpoint: {cmd}"
    assert "http://127.0.0.1:8080/ready" not in cmd, f"{where} hardcodes the bind instead of following it: {cmd}"
    for var, default in (("RT_DOCKER_HEALTH_HOST", "127.0.0.1"), ("RT_DOCKER_HEALTH_PORT", "8080")):
        assert re.search(rf"os\.getenv\(\s*['\"]{var}['\"]", cmd), f"{where} must read {var} inside python -c: {cmd}"
        assert f"'{default}'" in cmd or f'"{default}"' in cmd, f"{where} must default {var} to {default!r}: {cmd}"


def test_f09_compose_worker_healthcheck_hits_ready():
    cmd = _healthcheck_cmd(_svc("worker"), "worker")
    _assert_probe_follows_bind(cmd, "compose worker healthcheck")


def test_f09_compose_autoheal_service_pinned_labelled_and_socket_ro():
    svc = _svc("autoheal")
    image = str(svc.get("image") or "")
    repo, _, _ = _split_ref(image)
    assert repo == "willfarrell/autoheal", f"autoheal image must be willfarrell/autoheal, got {image!r}"
    _assert_pinned(image, "compose service 'autoheal'")

    env = _kv_map(svc.get("environment"))
    assert env.get("AUTOHEAL_CONTAINER_LABEL") == "autoheal", f"autoheal environment: {env}"

    sock = [m for m in _mounts(svc) if m[0] == "/var/run/docker.sock"]
    assert sock, "autoheal must mount /var/run/docker.sock"
    for src, tgt, ro in sock:
        assert tgt == "/var/run/docker.sock", f"docker.sock mounted at {tgt!r}"
        assert ro, "docker.sock must be mounted read-only (:ro)"


@pytest.mark.parametrize("name", ["worker", "scheduler"])
def test_f09_compose_autoheal_label_on_worker_and_scheduler(name: str):
    labels = _kv_map(_svc(name).get("labels"))
    assert labels.get("autoheal", "").lower() == "true", f"{name} labels: {labels}"


@pytest.mark.parametrize("path", [WORKER_ENV, LOCAL_ENV], ids=_rel)
@pytest.mark.parametrize("key,value", [
    ("RT_DOCKER_HEALTH_HOST", "127.0.0.1"),
    ("RT_DOCKER_HEALTH_PORT", "8080"),
    ("RT_HEALTH_PORT", "8082"),
    ("RT_WORKER_HTTP_HOST", "127.0.0.1"),
])
def test_f09_worker_env_documents_health_ports(path: Path, key: str, value: str):
    entries = _env_entries(path)
    assert key in entries, f"{_rel(path)} does not document {key}"
    assert entries[key][0] == value, f"{_rel(path)}: {key} should be {value!r}, got {entries[key][0]!r}"


# ═════════════════════════════════════════════════════════════════════════════
# #10 — no :latest / bare images anywhere; dependabot watches them
# ═════════════════════════════════════════════════════════════════════════════

def test_f10_compose_images_all_pinned():
    images = _compose_images()
    assert images, "docker-compose.yml declares no image: lines"
    for name, ref in images.items():
        _assert_pinned(ref, f"compose service {name!r}")


def test_f10_compose_declares_every_expected_image():
    repos = {_split_ref(ref)[0] for ref in _compose_images().values()}
    for want in ("redis", "livekit/livekit-server", "livekit/sip", "caddy", "willfarrell/autoheal"):
        assert want in repos, f"docker-compose.yml has no service using image {want!r} (have {sorted(repos)})"


def test_f10_dockerfile_images_all_pinned():
    images = _dockerfile_images()
    repos = {_split_ref(ref)[0] for ref in images}
    assert "python" in repos, f"Dockerfile base image is not python (saw {images})"
    assert "ghcr.io/astral-sh/uv" in repos, f"Dockerfile no longer copies uv from ghcr.io/astral-sh/uv (saw {images})"
    for ref in images:
        _assert_pinned(ref, "Dockerfile")
    # Dependabot's docker ecosystem only parses FROM: an image pinned inline on
    # `COPY --from=<image>` is one it never bumps. Every third-party image must
    # therefore be introduced by a FROM line (and copied from that stage).
    from_images = {
        [t for t in args.split() if not t.startswith("--")][0]
        for ins, args in _dockerfile_instructions(_read(DOCKERFILE)) if ins == "FROM"
    }
    inline = sorted(set(images) - from_images)
    assert not inline, f"Dockerfile pulls images outside FROM (Dependabot cannot see them): {inline}"


@pytest.mark.parametrize("path", [DOCKERFILE, COMPOSE], ids=_rel)
def test_f10_no_latest_literal_in_deploy_files(path: Path):
    offenders = []
    for i, line in enumerate(_read(path).splitlines(), 1):
        code = line.split("#", 1)[0]
        if ":latest" in code:
            offenders.append(f"{path.name}:{i}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_f10_dependabot_covers_docker_pip_and_actions():
    data = yaml.safe_load(_read(DEPENDABOT))
    assert isinstance(data, dict) and data.get("version") == 2, "dependabot.yml must be version: 2"
    updates = data.get("updates") or []
    assert updates, "dependabot.yml has no updates:"
    pairs: set[tuple[str, str]] = set()
    for u in updates:
        eco = u.get("package-ecosystem")
        assert eco, f"update entry without package-ecosystem: {u}"
        assert (u.get("schedule") or {}).get("interval"), f"{eco} entry needs schedule.interval"
        dirs = u.get("directories") or ([u.get("directory")] if u.get("directory") else [])
        assert dirs, f"{eco} entry needs directory/directories"
        for d in dirs:
            pairs.add((eco, str(d).rstrip("/") or "/"))
    # The docker ecosystem reads Dockerfiles only; compose image: lines need docker-compose.
    for want in (("docker-compose", "/deploy"), ("docker", "/worker"), ("pip", "/worker")):
        assert want in pairs, f"dependabot.yml missing {want}; has {sorted(pairs)}"
    assert ("docker", "/deploy") not in pairs, "dependabot.yml: /deploy has no Dockerfile; docker there watches nothing"
    assert any(eco == "github-actions" for eco, _ in pairs), f"dependabot.yml missing github-actions; has {sorted(pairs)}"


# ═════════════════════════════════════════════════════════════════════════════
# #11 — non-root image, /ready HEALTHCHECK, hardened worker + scheduler services
# ═════════════════════════════════════════════════════════════════════════════

_ALLOWED_AFTER_USER = {"CMD", "ENTRYPOINT", "HEALTHCHECK", "ENV", "EXPOSE", "WORKDIR", "LABEL", "STOPSIGNAL", "ARG", "VOLUME"}


def test_f11_dockerfile_runs_as_non_root_uid_10001():
    final = _final_stage_instructions()
    runs = " ; ".join(args for ins, args in final if ins == "RUN")
    created = re.search(r"\b(useradd|adduser)\b[^;&|]*\b10001\b", runs)
    assert created, "final stage must create a user with uid 10001 (useradd/adduser ... 10001)"

    users = [(i, args.strip()) for i, (ins, args) in enumerate(final) if ins == "USER"]
    assert users, "Dockerfile final stage has no USER instruction — the worker still runs as root"
    last_idx, last_user = users[-1]
    who = last_user.split(":")[0].strip()
    assert who not in ("", "root", "0"), f"final USER is {last_user!r}; must be the uid-10001 user"
    if who.isdigit():
        assert who == "10001", f"final USER is uid {who}, expected 10001"
    else:
        assert re.search(rf"\b{re.escape(who)}\b", created.group(0)), (
            f"final USER {who!r} is not the user created by: {created.group(0)!r}"
        )

    tail = [ins for ins, _ in final[last_idx + 1:]]
    assert set(tail) <= _ALLOWED_AFTER_USER, (
        f"Dockerfile must END with USER {who}; found {sorted(set(tail) - _ALLOWED_AFTER_USER)} after it"
    )


def test_f11_dockerfile_healthcheck_targets_ready():
    hcs = [args for ins, args in _dockerfile_instructions(_read(DOCKERFILE)) if ins == "HEALTHCHECK"]
    assert hcs, "Dockerfile has no HEALTHCHECK"
    _assert_probe_follows_bind(hcs[-1], "Dockerfile HEALTHCHECK")


def test_f11_greet_clip_cache_lives_under_tmpdir():
    # _clip_wav/_write_wav_atomic write into _GREET_CACHE; with the image read-only
    # and /tmp a tmpfs, that cache must be rooted at the tmp dir, not under /app.
    src = _read(AGENT_PY)
    m = re.search(r"^_GREET_CACHE\s*=\s*(.+)$", src, re.M)
    assert m, "agent.py no longer defines _GREET_CACHE"
    rhs = m.group(1).strip()
    assert "tempfile.gettempdir()" in rhs or rhs.startswith(('"/tmp', "'/tmp")), (
        f"_GREET_CACHE must live under the tmp dir, got {rhs}"
    )


@pytest.mark.parametrize("name", ["worker", "scheduler"])
def test_f11_compose_service_read_only_with_tmpfs(name: str):
    svc = _svc(name)
    assert svc.get("read_only") is True, f"{name}: read_only must be true"
    tmpfs = [t.split(":")[0] for t in _as_list(svc.get("tmpfs"))]
    assert "/tmp" in tmpfs, f"{name}: tmpfs must include /tmp (clip cache, scheduler heartbeat), got {tmpfs}"


@pytest.mark.parametrize("name", ["worker", "scheduler"])
def test_f11_compose_service_drops_caps_and_privileges(name: str):
    svc = _svc(name)
    caps = [c.upper() for c in _as_list(svc.get("cap_drop"))]
    assert "ALL" in caps, f"{name}: cap_drop must be [ALL], got {caps}"
    sec = _as_list(svc.get("security_opt"))
    assert "no-new-privileges:true" in sec, f"{name}: security_opt must include no-new-privileges:true, got {sec}"


@pytest.mark.parametrize("name", ["worker", "scheduler"])
def test_f11_compose_service_has_resource_limits(name: str):
    svc = _svc(name)
    limits = (((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {})
    assert svc.get("mem_limit") or limits.get("memory"), f"{name}: mem_limit not set"
    assert svc.get("cpus") or limits.get("cpus"), f"{name}: cpus not set"


# ═════════════════════════════════════════════════════════════════════════════
# #20 — worker/.env.example hygiene
# ═════════════════════════════════════════════════════════════════════════════

def test_f20_max_searches_alias_removed():
    entries = _env_entries(LOCAL_ENV)
    assert "MAX_SEARCHES_PER_CALL" not in entries, "MAX_SEARCHES_PER_CALL is read by nothing; remove it"
    assert "RT_MAX_SEARCHES_PER_CALL" in entries, "RT_MAX_SEARCHES_PER_CALL (the one agent.py reads) must stay documented"


def test_f20_no_real_phone_numbers_in_template():
    text = _read(LOCAL_ENV)
    assert "+19736069515" not in text, "real dev number leaked in worker/.env.example"
    assert "+18887261924" not in text, "real toll-free number leaked in worker/.env.example"
    leaked = re.findall(r"\+1\d{10}\b", text)
    assert not leaked, f"real-looking phone numbers in worker/.env.example: {leaked}"
    entries = _env_entries(LOCAL_ENV)
    assert entries.get("RT_PUBLIC_NUMBER", ("",))[0] == "+1XXXXXXXXXX"
    assert entries.get("TWILIO_FROM_NUMBER", ("",))[0] == "+1XXXXXXXXXX"


def test_f20_allowed_supabase_ref_value_emptied_guidance_kept():
    text = _read(LOCAL_ENV)
    entries = _env_entries(LOCAL_ENV)
    assert "RT_ALLOWED_SUPABASE_REF" in entries
    assert entries["RT_ALLOWED_SUPABASE_REF"][0] == "", (
        f"RT_ALLOWED_SUPABASE_REF must ship empty, got {entries['RT_ALLOWED_SUPABASE_REF'][0]!r}"
    )
    assert "ojoppcyvkxwfuwzjjxbw" not in text, "the real dev project ref must not appear anywhere in the template"
    ctx = _comment_window(LOCAL_ENV, "RT_ALLOWED_SUPABASE_REF")
    assert re.search(r"#.*(project|ref)", ctx), "guidance comment for RT_ALLOWED_SUPABASE_REF was dropped"


def test_f20_max_call_seconds_marked_not_yet_enforced():
    lines = [l for l in _read(LOCAL_ENV).splitlines() if l.startswith("RT_MAX_CALL_SECONDS=")]
    assert lines, "worker/.env.example has no RT_MAX_CALL_SECONDS line"
    line = lines[0]
    assert "NOT YET ENFORCED" in line, f"RT_MAX_CALL_SECONDS line must say NOT YET ENFORCED: {line}"
    assert "finding #5" in line, f"RT_MAX_CALL_SECONDS line must point at finding #5: {line}"


@pytest.mark.parametrize("key", [
    "RT_DOCKER_HEALTH_PORT",
    "RT_WORKER_HTTP_HOST",
    "RT_LOG_TRANSCRIPT",
    "RT_TRANSCRIPT_RETENTION_DAYS",
    "RT_REQUIRE_PEPPER",
    "RT_PHONE_HASH_PEPPER",
    "RT_PROD_SUPABASE_REFS",
])
def test_f20_new_runtime_vars_documented(key: str):
    entries = _env_entries(LOCAL_ENV)
    assert key in entries, f"worker/.env.example does not document {key}"


# ═════════════════════════════════════════════════════════════════════════════
# #9 (round 2) — probes follow the bind; /ready wording; scheduler heartbeat path
# ═════════════════════════════════════════════════════════════════════════════

def test_f09_scheduler_heartbeat_env_pinned_and_probe_reads_it():
    svc = _svc("scheduler")
    env = _kv_map(svc.get("environment"))
    path = env.get("RT_SCHEDULER_HEARTBEAT_FILE", "")
    assert path.startswith("/tmp/"), f"scheduler must pin RT_SCHEDULER_HEARTBEAT_FILE under /tmp, got {path!r}"
    cmd = _healthcheck_cmd(svc, "scheduler")
    assert "RT_SCHEDULER_HEARTBEAT_FILE" in cmd, f"scheduler healthcheck must read RT_SCHEDULER_HEARTBEAT_FILE: {cmd}"
    assert path in cmd, f"scheduler healthcheck default {path!r} must match the pinned env: {cmd}"


def test_f09_ready_described_as_misconfiguration_not_outage():
    # /ready's checks are config / db credentials / key / pepper — none of them
    # is "Supabase is down". A comment that says otherwise sends the on-call to
    # restart a container an outage did not break.
    for path, key in ((WORKER_ENV, "RT_DOCKER_HEALTH_PORT"), (LOCAL_ENV, "RT_DOCKER_HEALTH_PORT")):
        ctx = _comment_window(path, key, before=2, after=10)
        assert "misconfiguration" in ctx, f"{_rel(path)}: RT_DOCKER_HEALTH_PORT comment must say /ready is 503 on misconfiguration"
        assert "unusable" not in ctx, f"{_rel(path)}: RT_DOCKER_HEALTH_PORT comment still blames an unreachable dependency"
    compose_comments = " ".join(
        l.split("#", 1)[1].lower() for l in _read(COMPOSE).splitlines() if "#" in l
    )
    assert "misconfiguration" in compose_comments, "docker-compose.yml healthcheck comment must say /ready is 503 on misconfiguration"
    assert "while supabase or livekit is unusable" not in compose_comments, "docker-compose.yml still describes /ready as an outage signal"


# ═════════════════════════════════════════════════════════════════════════════
# #20 (round 2) — all three templates scanned; env <-> code cross-check
# ═════════════════════════════════════════════════════════════════════════════

_REAL_DEV_NUMBER = "+19736069515"
_REAL_TOLLFREE = "+18887261924"
_REAL_DEV_REF = "ojoppcyvkxwfuwzjjxbw"
_REAL_BOX_IP = "173.255.235.130"
_IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")


def _is_doc_ip(ip: str) -> bool:
    """Loopback, unspecified, and the RFC 5737 documentation blocks only."""
    return ip in ("127.0.0.1", "0.0.0.0") or ip.startswith(("192.0.2.", "198.51.100.", "203.0.113."))


@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=_rel)
def test_f20_no_real_number_ip_or_ref_in_any_template(path: Path):
    text = _read(path)
    for real in (_REAL_DEV_NUMBER, _REAL_TOLLFREE, _REAL_DEV_REF, _REAL_BOX_IP):
        assert real not in text, f"{_rel(path)}: real value {real!r} leaked into a committed template"
    leaked = re.findall(r"\+1\d{10}\b", text)
    assert not leaked, f"{_rel(path)}: real-looking phone numbers: {leaked}"
    ips = [m.group(0) for m in _IPV4.finditer(text)]
    bad = [ip for ip in ips if not _is_doc_ip(ip)]
    assert not bad, f"{_rel(path)}: non-documentation IPv4 addresses: {bad}"
    entries = _env_entries(path)
    for key in ("SUPABASE_PROJECT_REF", "RT_ALLOWED_SUPABASE_REF", "SUPABASE_URL", "RT_PHONE_HASH_PEPPER"):
        if key in entries:
            value = entries[key][0]
            # .env.test.example alone ships a shaped placeholder URL (https://<test-ref>...),
            # which is not a ref; everything else must be exactly empty.
            placeholder = path == TEST_ENV and key == "SUPABASE_URL" and re.fullmatch(r"https://<[a-z-]+>\.supabase\.co", value)
            assert value == "" or placeholder, f"{_rel(path)}: {key} must ship empty, got {value!r}"
    for key in ("NUMBER", "RT_PUBLIC_NUMBER", "TWILIO_FROM_NUMBER"):
        if key in entries and entries[key][0]:
            assert entries[key][0] == "+1XXXXXXXXXX", f"{_rel(path)}: {key} must be the +1XXXXXXXXXX placeholder"
    assert not re.search(r"^BOX_HOST=\S*@(?!203\.0\.113\.)", text, re.M), f"{_rel(path)}: BOX_HOST points at a real host"


def test_f20_lane_env_has_no_unread_db_url():
    entries = _env_entries(LANE_ENV)
    assert "SUPABASE_DB_URL" not in entries, "lane.env.example: SUPABASE_DB_URL is read by nothing; drop it"
    assert "SUPABASE_ACCESS_TOKEN" in entries, "lane.env.example: stand-up-lane.sh step 2b needs SUPABASE_ACCESS_TOKEN"


# Names in worker/.env.example that are consumed by something other than our
# os.getenv calls: the LiveKit SDK reads LIVEKIT_*, SUPABASE_PROJECT_REF is a
# sql_push/migrate convenience, RT_MAX_CALL_SECONDS is kept only as a signpost.
_NOT_READ_BY_CODE_PREFIXES = ("LIVEKIT_",)
_NOT_READ_BY_CODE = {"SUPABASE_PROJECT_REF", "RT_MAX_CALL_SECONDS"}
# `\bgetenv(` rather than `os.getenv(`: rt_facts.py reads through __import__("os").getenv(...).
_GETENV_CALL = re.compile(r"(?:\bgetenv|os\.environ\.get)\(([^)]*)\)")
_ENVIRON_INDEX = re.compile(r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]")
_ENV_LITERAL = re.compile(r"['\"]([A-Z][A-Z0-9_]*)['\"]")


def _code_files() -> list[Path]:
    out: list[Path] = []
    for base in (WORKER_DIR, DEPLOY_DIR):
        for p in base.rglob("*.py"):
            if ".venv" in p.parts or "node_modules" in p.parts or "tests" in p.parts:
                continue
            if p.name.startswith("test_") or p.name.startswith("conftest"):
                continue
            out.append(p)
    return out


def _names_read_in(src: str) -> set[str]:
    names: set[str] = set()
    for m in _GETENV_CALL.finditer(src):
        names.update(_ENV_LITERAL.findall(m.group(1)))
    names.update(_ENVIRON_INDEX.findall(src))
    return names


def test_f20_every_documented_var_is_read_by_code():
    documented = set(_env_entries(LOCAL_ENV))
    read: set[str] = set()
    for p in _code_files():
        read |= _names_read_in(p.read_text(encoding="utf-8", errors="replace"))
    unread = sorted(
        k for k in documented
        if k not in read and k not in _NOT_READ_BY_CODE and not k.startswith(_NOT_READ_BY_CODE_PREFIXES)
    )
    assert not unread, f"worker/.env.example documents variables nothing under worker/ or deploy/ reads: {unread}"


# Read by code but deliberately NOT a NAME= line: the template explains, in a
# comment, that setting it outside the harness disables the agent's autonomous side.
_DOCUMENTED_AS_FORBIDDEN = {"RT_HARNESS_TEST_MODE"}


def test_f20_every_rt_var_read_by_worker_modules_is_documented():
    text = _read(LOCAL_ENV)
    documented = set(_env_entries(LOCAL_ENV))
    read: set[str] = set()
    for p in sorted(WORKER_DIR.glob("*.py")):
        if p.name.startswith("test_"):
            continue
        read |= {n for n in _names_read_in(p.read_text(encoding="utf-8", errors="replace")) if n.startswith("RT_")}
    assert read, "no os.getenv('RT_...') reads found in worker/*.py — the scan is broken"
    for name in _DOCUMENTED_AS_FORBIDDEN:
        assert name in text, f"worker/.env.example must at least mention {name}"
    missing = sorted(read - documented - _DOCUMENTED_AS_FORBIDDEN)
    assert not missing, f"worker/*.py reads RT_ variables worker/.env.example does not document: {missing}"


@pytest.mark.parametrize("key", [
    "RT_CALL_HARD_CAP_MINS", "RT_CALL_SOFT_WRAP_MINS", "RT_CALL_FIRM_WRAP_MINS",
    "RT_DAILY_MINUTES_CAP", "RT_INBOUND_NUMBER", "RT_GREET_SEED_TIMEOUT", "RT_MEDIA_WAIT",
    "RT_GREET_CACHE", "RT_HISTORY_BUDGET", "RT_OURS_BUDGET", "RT_SELF_BUDGET",
    "RT_SCHEDULER_INTERVAL_SECONDS", "RT_SCHEDULER_HEARTBEAT_FILE", "RT_DOCKER_HEALTH_HOST",
    "RT_ENV", "RT_LANGUAGE", "RT_LOG_LEVEL", "LOG_LEVEL", "LOG_SHIPPER_URL", "LOG_SHIPPER_API_KEY",
    "SENTRY_DSN", "SENTRY_ENVIRONMENT", "SENTRY_TRACES_SAMPLE_RATE",
])
def test_f20_round2_runtime_vars_documented(key: str):
    entries = _env_entries(LOCAL_ENV)
    assert key in entries, f"worker/.env.example does not document {key}"


@pytest.mark.parametrize("path", [LOCAL_ENV, WORKER_ENV], ids=_rel)
def test_f20_max_call_seconds_marked_superseded_by_hard_cap(path: Path):
    entries = _env_entries(path)
    assert "RT_MAX_CALL_SECONDS" in entries, f"{_rel(path)}: RT_MAX_CALL_SECONDS line must stay (as a signpost)"
    assert "RT_CALL_HARD_CAP_MINS" in entries, f"{_rel(path)}: the enforced cap RT_CALL_HARD_CAP_MINS must be documented"
    ctx = _comment_window(path, "RT_MAX_CALL_SECONDS", before=0, after=2)
    assert "supersed" in ctx, f"{_rel(path)}: RT_MAX_CALL_SECONDS must say it is superseded"
    assert "rt_call_hard_cap_mins" in ctx, f"{_rel(path)}: RT_MAX_CALL_SECONDS must point at RT_CALL_HARD_CAP_MINS"


def test_f20_inventory_live_var_list_drops_max_call_seconds():
    text = _read(DEPLOY_DIR / "provisioning" / "dev-inventory.md")
    assert not re.search(r"^RT_MAX_CALL_SECONDS\s*$", text, re.M), "dev-inventory.md still lists RT_MAX_CALL_SECONDS as a live var"
    assert re.search(r"^RT_PUBLIC_NUMBER\s*$", text, re.M), "dev-inventory.md live-var list went missing"


# ═════════════════════════════════════════════════════════════════════════════
# #8 — stand-up-lane.sh: a real migration command, the right counts
# ═════════════════════════════════════════════════════════════════════════════

STAND_UP = DEPLOY_DIR / "provisioning" / "stand-up-lane.sh"


def _highest_migration() -> int:
    nums = [int(m.group(1)) for p in (WORKER_DIR / "sql").glob("*.sql") if (m := re.match(r"^(\d{2})-", p.name))]
    assert nums, "worker/sql has no NN- migrations"
    return max(nums)


def test_f08_stand_up_lane_applies_migrations_with_a_real_command():
    text = _read(STAND_UP)
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "sql_push.py\" --apply" not in code and "sql_push.py --apply" not in code, "sql_push.py has no --apply flag"
    assert re.search(r"scripts/migrate\.py\"?\s+--apply", code) or re.search(r"sql_push\.py\"?\s+--file", code), (
        "stand-up-lane.sh must apply migrations via scripts/migrate.py --apply (or sql_push.py --file per file)"
    )


def test_f08_stand_up_lane_migration_range_matches_sql_dir():
    text = _read(STAND_UP)
    m = re.search(r"\(01→(\d{2})\)", text)
    assert m, "stand-up-lane.sh step 2b must state the migration range as (01→NN)"
    assert int(m.group(1)) == _highest_migration(), (
        f"stand-up-lane.sh says migrations run 01→{m.group(1)} but worker/sql tops out at {_highest_migration():02d}"
    )


def test_f08_stand_up_lane_counts_all_compose_services():
    text = _read(STAND_UP)
    n = len(_compose()["services"])
    words = {6: "six", 7: "seven", 8: "eight", 9: "nine"}
    assert f"the {words[n]} containers" in text, f"stand-up-lane.sh must say 'the {words[n]} containers' (compose has {n} services)"
    assert "six containers" not in text or n == 6, "stand-up-lane.sh still says six containers"
    assert "network_mode: none" in _read(COMPOSE) and "autoheal" in text, "stand-up-lane.sh must note autoheal (network_mode: none)"


# ═════════════════════════════════════════════════════════════════════════════
# #16 — GOOGLE_API_KEY: restrict by API and by IP
# ═════════════════════════════════════════════════════════════════════════════

def test_f16_google_key_comment_says_restrict_by_api_and_ip():
    ctx = _comment_window(LOCAL_ENV, "GOOGLE_API_KEY", before=6, after=1)
    assert "generative language" in ctx, "GOOGLE_API_KEY comment must say to restrict the key to the Generative Language API"
    # rt_directory.places() falls back to GOOGLE_API_KEY, so a key restricted to
    # Generative Language alone silently kills directory lookups.
    assert "places api" in ctx, "GOOGLE_API_KEY comment must also allow the Places API (rt_directory falls back to this key)"
    assert "rt_directory" in ctx, "GOOGLE_API_KEY comment must say WHY Places is needed (rt_directory)"
    assert re.search(r"\bby ip\b", ctx), "GOOGLE_API_KEY comment must say to restrict the key by IP"
    assert "google cloud" in ctx, "GOOGLE_API_KEY comment must say where (Google Cloud) the restriction is set"


# ═════════════════════════════════════════════════════════════════════════════
# #20 (round 3) — render-config.sh forwards the pepper and refuses an empty one
# ═════════════════════════════════════════════════════════════════════════════

_FAKE_PEPPER = "a" * 64  # what `openssl rand -hex 32` yields, shape-wise


def _lane_env_from_template(overrides: dict[str, str]) -> str:
    """lane.env.example with NAME= lines replaced; comments and the rest untouched,
    so the file exercised is the one an operator would actually fill in."""
    out = []
    for line in _read(LANE_ENV).splitlines():
        m = _ENV_LINE.match(line)
        if m and m.group(1) in overrides:
            out.append(f"{m.group(1)}={overrides[m.group(1)]}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


_FILLED_LANE = {
    "GOOGLE_API_KEY": "gk-test",
    "SUPABASE_URL": "https://abcdefghijklmnopqrst.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "srk-test",
    "SUPABASE_PROJECT_REF": "abcdefghijklmnopqrst",
    "LIVEKIT_API_KEY": "APItestkey",
    "LIVEKIT_API_SECRET": "lk-secret-test",
    "RT_PHONE_HASH_PEPPER": _FAKE_PEPPER,
}


def _run_render(lane: Path, out: Path) -> subprocess.CompletedProcess:
    """render-config.sh in OUT_DIR mode: no ssh, no box, a fixed argv of our own script."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not on PATH")
    # Anything the developer's shell exports for these names would leak into the
    # render and mask what lane.env says.
    env = {k: v for k, v in os.environ.items() if k not in _FILLED_LANE and k not in ("RT_REQUIRE_PEPPER", "OUT_DIR", "APPLY", "FORCE")}
    env.update({"ENV_FILE": str(lane), "OUT_DIR": str(out)})
    return subprocess.run(  # noqa: S603 - fixed argv, our own tracked script
        [bash, str(RENDER_CONFIG)], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=60,
    )


def _render(tmp_path: Path, overrides: dict[str, str]) -> tuple[subprocess.CompletedProcess, Path]:
    """Run render-config.sh against a lane.env built from the template."""
    lane = tmp_path / "lane.env"
    lane.write_text(_lane_env_from_template(overrides), encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    return _run_render(lane, out), out


def test_f20_render_config_substitution_map_names_pepper_and_switch():
    src = _read(RENDER_CONFIG)
    m = re.search(r"m = \{k: os\.environ\.get\(k, \"\"\) for k in \((.*?)\)\}", src, re.S)
    assert m, "render-config.sh no longer builds the lane.env -> worker.env key map"
    keys = set(re.findall(r'"([A-Z_]+)"', m.group(1)))
    for want in ("RT_PHONE_HASH_PEPPER", "RT_REQUIRE_PEPPER"):
        assert want in keys, f"render-config.sh does not forward {want} from lane.env into worker.env: {sorted(keys)}"
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert re.search(r"\$\{RT_PHONE_HASH_PEPPER:\?", code), "render-config.sh needs a ${RT_PHONE_HASH_PEPPER:?...} guard"
    for opt_out in ("0", "false", "no", "off"):
        assert re.search(rf"(^|[|)\s]){opt_out}[|)]", code, re.M), f"the pepper guard must let RT_REQUIRE_PEPPER={opt_out} opt out"


def test_f20_render_config_lands_pepper_in_worker_env(tmp_path: Path):
    proc, out = _render(tmp_path, _FILLED_LANE)
    assert proc.returncode == 0, f"render-config.sh failed:\n{proc.stdout}\n{proc.stderr}"
    rendered = out / "worker.env"
    assert rendered.exists(), f"no worker.env rendered; stdout:\n{proc.stdout}"
    values = dict(dotenv_values(rendered))
    assert values.get("RT_PHONE_HASH_PEPPER") == _FAKE_PEPPER, f"pepper not forwarded: {values.get('RT_PHONE_HASH_PEPPER')!r}"
    assert values.get("RT_REQUIRE_PEPPER") == "1", f"RT_REQUIRE_PEPPER must render as '1': {values.get('RT_REQUIRE_PEPPER')!r}"
    assert values.get("LIVEKIT_API_KEY") == "APItestkey"
    assert values.get("RT_ALLOWED_SUPABASE_REF") == "abcdefghijklmnopqrst"
    assert _FAKE_PEPPER not in proc.stdout + proc.stderr, "render-config.sh printed the pepper"
    assert (rendered.stat().st_mode & 0o777) == 0o600, "rendered worker.env must be mode 600"
    for f in ("livekit.yaml", "sip.yaml", "Caddyfile"):
        assert (out / f).exists(), f"{f} not rendered"


def test_f20_render_config_refuses_empty_pepper_when_required(tmp_path: Path):
    proc, out = _render(tmp_path, {**_FILLED_LANE, "RT_PHONE_HASH_PEPPER": ""})
    assert proc.returncode != 0, f"an empty pepper with RT_REQUIRE_PEPPER=1 must fail the render:\n{proc.stdout}"
    assert "RT_PHONE_HASH_PEPPER" in proc.stderr, f"the refusal must name the variable:\n{proc.stderr}"
    assert not (out / "worker.env").exists(), "nothing may be rendered after the refusal"


def test_f20_render_config_empty_switch_still_requires_pepper(tmp_path: Path):
    # lane.env with RT_REQUIRE_PEPPER left blank keeps the template's `=1`, so the
    # rendered lane WOULD require a pepper: the guard must fail closed here too.
    proc, out = _render(tmp_path, {**_FILLED_LANE, "RT_PHONE_HASH_PEPPER": "", "RT_REQUIRE_PEPPER": ""})
    assert proc.returncode != 0, "blank RT_REQUIRE_PEPPER + empty pepper must be refused (fail closed)"
    assert not (out / "worker.env").exists()


def test_f20_render_config_explicit_opt_out_allows_empty_pepper(tmp_path: Path):
    proc, out = _render(tmp_path, {**_FILLED_LANE, "RT_PHONE_HASH_PEPPER": "", "RT_REQUIRE_PEPPER": "0"})
    assert proc.returncode == 0, f"RT_REQUIRE_PEPPER=0 is the documented dev-only opt-out:\n{proc.stderr}"
    values = dict(dotenv_values(out / "worker.env"))
    assert values.get("RT_REQUIRE_PEPPER") == "0", "the opt-out must reach worker.env, or the lane still dies at boot"
    assert values.get("RT_PHONE_HASH_PEPPER") == ""


def test_f20_render_config_refuses_out_dir_inside_the_tree(tmp_path: Path):
    lane = tmp_path / "lane.env"
    lane.write_text(_lane_env_from_template(_FILLED_LANE), encoding="utf-8")
    inside = WORKER_DIR / ".pytest-render-out"
    inside.mkdir(exist_ok=True)
    try:
        proc = _run_render(lane, inside)
        assert proc.returncode != 0, "a rendered secret must never land inside the working tree"
        assert not any(inside.iterdir()), "files were written into the tree before the refusal"
    finally:
        shutil.rmtree(inside, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════════
# #20 (round 3) — lane.env names are read by provisioning; worker.env names by code
# ═════════════════════════════════════════════════════════════════════════════

def _shell_code(paths) -> str:
    return "\n".join(
        l for p in paths for l in p.read_text(encoding="utf-8", errors="replace").splitlines()
        if not l.lstrip().startswith("#")
    )


def test_f20_every_lane_env_var_is_read_by_a_provisioning_script():
    scripts = sorted(DEPLOY_DIR.rglob("*.sh"))
    assert scripts, "deploy/ has no shell scripts — the scan is broken"
    code = _shell_code(scripts)
    documented = set(_env_values(LANE_ENV))
    unread = sorted(k for k in documented if not re.search(rf"\b{re.escape(k)}\b", code))
    assert not unread, f"lane.env.example documents names no deploy/**/*.sh reads (outside comments): {unread}"


# worker.env.example names consumed by something other than our getenv calls:
# LIVEKIT_* by the SDK; RT_MAX_CALL_SECONDS is kept only as a signpost.
_WORKER_ENV_NOT_READ_BY_CODE = {"RT_MAX_CALL_SECONDS"}


def test_f20_every_worker_env_var_is_read_by_code():
    documented = set(_env_values(WORKER_ENV))
    read: set[str] = set()
    for p in _code_files():
        read |= _names_read_in(p.read_text(encoding="utf-8", errors="replace"))
    assert read, "no getenv reads found — the scan is broken"
    unread = sorted(
        k for k in documented
        if k not in read and k not in _WORKER_ENV_NOT_READ_BY_CODE and not k.startswith(_NOT_READ_BY_CODE_PREFIXES)
    )
    assert not unread, f"deploy/worker.env.example documents variables nothing under worker/ or deploy/ reads: {unread}"
    for want in ("RT_PHONE_HASH_PEPPER", "RT_REQUIRE_PEPPER"):
        assert want in documented and want in read, f"{want} must be both documented in worker.env.example and read by code"


def test_f20_worker_env_header_no_longer_claims_nothing_is_aspirational():
    head = "\n".join(_read(WORKER_ENV).splitlines()[:20]).lower()
    assert "nothing here is aspirational" not in head, "worker.env.example header still claims every name was captured from the box"
    assert "capture" in head, "worker.env.example header must say where the captured names came from"
    assert "rt_phone_hash_pepper" in head, "worker.env.example header must name the pepper as the post-capture addition"


def test_f20_inventory_says_key_list_is_regenerated_by_capture():
    text = _read(DEPLOY_DIR / "provisioning" / "dev-inventory.md")
    note = text.lower()
    assert "regenerated by `capture-lane.sh`" in note, "dev-inventory.md must say the worker.env key list is regenerated by capture-lane.sh"
    assert "rt_phone_hash_pepper" in note and "rt_require_pepper" in note, "dev-inventory.md must say the pepper keys appear on the next capture"
    m = re.search(r"^## worker\.env — keys present.*?```\n(.*?)```", text, re.S | re.M)
    assert m, "dev-inventory.md lost its worker.env key list"
    listed = set(m.group(1).split())
    assert listed <= set(_env_values(WORKER_ENV)), (
        f"dev-inventory.md lists worker.env keys the template no longer carries: {sorted(listed - set(_env_values(WORKER_ENV)))}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# #8 (round 3) — migrate.py runs through the worker venv (dotenv is a worker dep)
# ═════════════════════════════════════════════════════════════════════════════

def test_f08_stand_up_lane_runs_migrations_through_uv():
    code = "\n".join(l for l in _read(STAND_UP).splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"python3\s+\"?\$\{REPO\}/worker/scripts/", code), (
        "stand-up-lane.sh still calls worker scripts with the host python3, which has no python-dotenv"
    )
    uv_prefix = re.search(r"uv run --frozen --project \"?\$\{REPO\}/worker\"? python", code)
    assert uv_prefix, "stand-up-lane.sh must run worker scripts via `uv run --frozen --project ${REPO}/worker python`"
    for script in ("migrate.py", "check_migrations.py"):
        assert re.search(rf"(UV_PY\[@\]\}}\"|uv run --frozen --project \"?\$\{{REPO\}}/worker\"? python)\s+\"?\$\{{REPO\}}/worker/scripts/{script}", code), (
            f"{script} must be invoked through the uv prefix"
        )


# ═════════════════════════════════════════════════════════════════════════════
# #9 (round 3) — probe comments are truthful about env and the pepper
# ═════════════════════════════════════════════════════════════════════════════

def _comments_of(path: Path) -> str:
    return " ".join(l.split("#", 1)[1].strip().lower() for l in _read(path).splitlines() if "#" in l)


@pytest.mark.parametrize("path", [COMPOSE, DOCKERFILE], ids=_rel)
def test_f09_probe_comment_says_pepper_dies_at_boot_not_on_ready(path: Path):
    # A required-but-missing pepper kills agent.py before rt_health ever binds, so
    # /ready cannot be what reports it; a comment listing "pepper" among the 503
    # causes sends the on-call to a probe that never fires.
    comments = _comments_of(path)
    assert "key or pepper" not in comments and "key, pepper" not in comments, f"{_rel(path)}: still lists the pepper as a /ready 503 cause"
    assert "pepper" in comments and "boot" in comments, f"{_rel(path)}: must say a required-but-missing pepper dies at boot"
    assert "rt_docker_health_host/port" in comments, f"{_rel(path)}: must say the probe reads RT_DOCKER_HEALTH_HOST/PORT from the env"
    assert "_resolve_bind" not in comments, f"{_rel(path)}: names a private helper the probe does not call"


@pytest.mark.parametrize("path", [WORKER_ENV, LOCAL_ENV], ids=_rel)
def test_f09_health_port_comment_says_pepper_dies_at_boot(path: Path):
    ctx = _comment_window(path, "RT_DOCKER_HEALTH_PORT", before=2, after=10)
    assert "key or pepper" not in ctx and "key, pepper" not in ctx and "key,\n" not in ctx.replace("key,\n#", ""), (
        f"{_rel(path)}: RT_DOCKER_HEALTH_PORT still lists the pepper as a /ready 503 cause"
    )
    assert "pepper" in ctx and "boot" in ctx, f"{_rel(path)}: must say a required-but-missing pepper dies at boot, before /ready exists"


# ═════════════════════════════════════════════════════════════════════════════
# #20 (round 4) — stand-up-lane.sh step 2b: lane values via --env-file, and the
# verify is a gate, not a notice
# ═════════════════════════════════════════════════════════════════════════════

CAPTURE_LANE = DEPLOY_DIR / "provisioning" / "capture-lane.sh"
_SUPABASE_LANE_KEYS = ("SUPABASE_PROJECT_REF", "SUPABASE_ACCESS_TOKEN", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
_STAND_UP_LANE = {
    "BOX_HOST": "root@203.0.113.9",
    "AGENT_NAME": "pal-test",
    "NUMBER": "+15550000000",
    "GOOGLE_API_KEY": "gk-test",
    "SUPABASE_URL": "https://abcdefghijklmnopqrst.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "srk-test",
    "SUPABASE_PROJECT_REF": "abcdefghijklmnopqrst",
    "SUPABASE_ACCESS_TOKEN": "sbp-test-token",
    "LIVEKIT_API_KEY": "APItestkey",
    "LIVEKIT_API_SECRET": "lk-secret-test",
    "RT_PHONE_HASH_PEPPER": _FAKE_PEPPER,
}


def _step_2b(code: str) -> str:
    """The non-comment lines of step 2b only, so a hit elsewhere cannot satisfy it."""
    m = re.search(r'step "2b"(.*?)\nstep 3 ', code, re.S)
    assert m, "stand-up-lane.sh lost its step 2b / step 3 structure"
    return m.group(1)


def test_f20_stand_up_step_2b_passes_env_file_to_both_scripts():
    code = "\n".join(l for l in _read(STAND_UP).splitlines() if not l.lstrip().startswith("#"))
    block = _step_2b(code)
    for script in ("migrate.py", "check_migrations.py"):
        assert re.search(rf"scripts/{re.escape(script)}\"[^\n]*--env-file \"\$LANE_TMP\"", block), (
            f"step 2b must call {script} with --env-file \"$LANE_TMP\" so it cannot read worker/.env.local"
        )
    assert re.search(r"scripts/migrate\.py\"\s+--apply\s+--env-file", block), "migrate.py must be --apply --env-file"
    assert "umask 077" in block and "mktemp" in block, "the lane values must go through a mktemp file created under umask 077"
    assert re.search(r"trap 'rm -f \"\$LANE_TMP\"' EXIT", block), "the temp file must be removed on exit"
    for key in _SUPABASE_LANE_KEYS:
        assert re.search(rf"\b{key}\b", block), f"step 2b does not write {key} into the --env-file"


def test_f20_stand_up_step_2b_has_no_soft_fallbacks():
    code = "\n".join(l for l in _read(STAND_UP).splitlines() if not l.lstrip().startswith("#"))
    block = _step_2b(code)
    assert "completed or skipped" not in _read(STAND_UP), "the 'completed or skipped' notice let a failed apply pass"
    assert "verify DB migrations manually" not in _read(STAND_UP), "the check_migrations fallback let a BEHIND lane pass"
    assert "skipped — Supabase credentials not set" not in _read(STAND_UP), "step 2b must not be skippable by leaving lane.env blank"
    for bad in (r"\|\|\s*echo", r"\|\|\s*true"):
        assert not re.search(bad, block), f"step 2b still swallows a failure with {bad!r}"
    assert re.search(r"check_migrations\.py\"[^\n]*\n\s*\|\| \{[^\n]*exit 1", block), (
        "a non-zero check_migrations.py must exit the stand-up non-zero"
    )
    for key in ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_PROJECT_REF"):
        assert re.search(rf'\$\{{{key}:\?', block), f"step 2b must refuse to run without {key} (fail closed, not skip)"


def _fake_bin(tmp_path: Path) -> Path:
    """Stand-ins for everything stand-up-lane.sh would reach the outside world with.

    ssh/scp/rsync succeed silently; `uv` records its argv plus a copy (and mode) of
    any --env-file it was handed, applies cleanly, and reports the lane BEHIND on
    check_migrations.py — the case step 2b exists to stop.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("ssh", "scp", "rsync"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    log = tmp_path / "uv.log"
    (bin_dir / "uv").write_text(
        "#!/bin/sh\n"
        f'printf \'ARGV: %s\\n\' "$*" >> "{log}"\n'
        "prev=\"\"; envf=\"\"\n"
        'for a in "$@"; do [ "$prev" = "--env-file" ] && envf="$a"; prev="$a"; done\n'
        'if [ -n "$envf" ]; then\n'
        f'  printf \'MODE: %s\\n\' "$(stat -f %Lp "$envf" 2>/dev/null || stat -c %a "$envf")" >> "{log}"\n'
        f'  sed "s/^/ENV: /" "$envf" >> "{log}"\n'
        "fi\n"
        'case "$*" in *check_migrations.py*) echo "  --> BEHIND"; exit 1 ;; esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    return bin_dir


def _run_stand_up(tmp_path: Path, apply: bool, overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not on PATH")
    lane = tmp_path / "lane.env"
    lane.write_text(_lane_env_from_template({**_STAND_UP_LANE, **(overrides or {})}), encoding="utf-8")
    scrub = set(_STAND_UP_LANE) | {"RT_REQUIRE_PEPPER", "APPLY", "FORCE", "OUT_DIR", "ENV_FILE", "TARGET", "LANE", "TMPDIR"}
    env = {k: v for k, v in os.environ.items() if k not in scrub}
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    env.update({
        "ENV_FILE": str(lane), "APPLY": "1" if apply else "0", "TMPDIR": str(tmpdir),
        "PATH": f"{_fake_bin(tmp_path)}:{env.get('PATH', '/usr/bin:/bin')}",
    })
    return subprocess.run(  # noqa: S603 - fixed argv, our own tracked script, every external command stubbed
        [bash, str(STAND_UP)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )


def test_f20_stand_up_dry_run_shows_env_file_for_both_scripts_and_leaves_no_file(tmp_path: Path):
    proc = _run_stand_up(tmp_path, apply=False)
    assert proc.returncode == 0, f"dry run must succeed:\n{proc.stdout}\n{proc.stderr}"
    lines = [l for l in proc.stdout.splitlines() if "would run:" in l and "/worker/scripts/" in l]
    assert len(lines) == 2, f"expected exactly one migrate.py and one check_migrations.py plan line:\n{proc.stdout}"
    for script in ("migrate.py", "check_migrations.py"):
        line = next((l for l in lines if script in l), None)
        assert line, f"no plan line for {script}"
        m = re.search(r"--env-file (\S+)", line)
        assert m, f"{script} plan line has no --env-file: {line}"
        assert not Path(m.group(1)).exists(), "the lane temp file must be gone once step 2b is over"
    assert "--apply --env-file" in next(l for l in lines if "migrate.py" in l)
    for secret in ("sbp-test-token", "srk-test"):
        assert secret not in proc.stdout + proc.stderr, "a lane secret was printed"
    assert "completed or skipped" not in proc.stdout


def test_f20_stand_up_apply_fails_when_check_migrations_fails(tmp_path: Path):
    proc = _run_stand_up(tmp_path, apply=True)
    assert proc.returncode != 0, f"a BEHIND lane must fail the stand-up:\n{proc.stdout}\n{proc.stderr}"
    assert "FAILED" in proc.stderr and "check_migrations" in proc.stderr, f"the failure must say why:\n{proc.stderr}"
    assert "Firewall" not in proc.stdout, "the stand-up carried on past a failed verify"
    log = (tmp_path / "uv.log").read_text(encoding="utf-8")
    argv = [l for l in log.splitlines() if l.startswith("ARGV: ")]
    assert len(argv) == 2 and "migrate.py --apply --env-file" in argv[0] and "check_migrations.py --env-file" in argv[1], (
        f"expected migrate.py --apply then check_migrations.py, both with --env-file:\n{log}"
    )
    modes = {l.split()[1] for l in log.splitlines() if l.startswith("MODE: ")}
    assert modes == {"600"}, f"the --env-file must be mode 600: {modes}"
    env_lines = {l[len("ENV: "):] for l in log.splitlines() if l.startswith("ENV: ")}
    for key in _SUPABASE_LANE_KEYS:
        assert f"{key}={_STAND_UP_LANE[key]}" in env_lines, f"--env-file is missing {key}:\n{log}"
    assert not any(k.startswith("LIVEKIT_") or k.startswith("GOOGLE_") for k in env_lines), "the --env-file carries more than the Supabase values"
    env_files = {l.split()[-1] for l in argv}
    assert len(env_files) == 1 and not Path(env_files.pop()).exists(), "the lane temp file must be removed even on failure"
    for secret in ("sbp-test-token", "srk-test"):
        assert secret not in proc.stdout + proc.stderr, "a lane secret was printed"


def test_f20_stand_up_refuses_a_lane_without_supabase_ref(tmp_path: Path):
    proc = _run_stand_up(tmp_path, apply=False, overrides={"SUPABASE_PROJECT_REF": ""})
    assert proc.returncode != 0, "step 2b must fail closed, not skip, when the lane has no project ref"
    assert "SUPABASE_PROJECT_REF" in proc.stderr
    assert "Firewall" not in proc.stdout


# ═════════════════════════════════════════════════════════════════════════════
# #20 (round 4) — render-config.sh judges the pepper the way config.pepper_problem does
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("pepper, why", [
    ("changeme-changeme-changeme", "placeholder"),
    ("REPLACE_WITH_A_REAL_PEPPER_VALUE", "placeholder"),
    ("123456789012", "too short"),
    ("#" + "b" * 40, "starts with #"),
], ids=["changeme", "REPLACE-upper", "12-chars", "comment"])
def test_f20_render_config_refuses_an_unusable_pepper(tmp_path: Path, pepper: str, why: str):
    proc, out = _render(tmp_path, {**_FILLED_LANE, "RT_PHONE_HASH_PEPPER": pepper})
    assert proc.returncode != 0, f"pepper {pepper!r} must be refused:\n{proc.stdout}"
    assert "RT_PHONE_HASH_PEPPER" in proc.stderr and why in proc.stderr, f"the refusal must name the variable and why:\n{proc.stderr}"
    assert "unless the box already has one" in proc.stderr, "the refusal must warn against minting a second pepper for a live box"
    assert pepper not in proc.stdout + proc.stderr, "render-config.sh printed the rejected pepper"
    assert not (out / "worker.env").exists(), "nothing may be rendered after the refusal"


def test_f20_render_config_accepts_a_32_char_pepper(tmp_path: Path):
    pepper = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    proc, out = _render(tmp_path, {**_FILLED_LANE, "RT_PHONE_HASH_PEPPER": pepper})
    assert proc.returncode == 0, f"a 32-char pepper is usable:\n{proc.stderr}"
    assert dict(dotenv_values(out / "worker.env")).get("RT_PHONE_HASH_PEPPER") == pepper


def test_f20_render_config_pepper_rule_matches_config():
    # The shell rule must not drift from config.pepper_problem(): same floor, same stub words.
    import config  # noqa: PLC0415 - the worker package is importable from tests

    code = "\n".join(l for l in _read(RENDER_CONFIG).splitlines() if not l.lstrip().startswith("#"))
    assert re.search(rf'-ge {config.PEPPER_MIN_LEN}\b', code), f"render-config.sh must enforce >= {config.PEPPER_MIN_LEN} chars"
    for mark in config._PEPPER_PLACEHOLDERS:  # noqa: SLF001 - the shared contract lives in config
        assert f"*{mark}*" in code, f"render-config.sh does not reject the {mark!r} placeholder"
    assert "unless the box already has one" in _read(RENDER_CONFIG)


# ═════════════════════════════════════════════════════════════════════════════
# #20 (round 4) — capture-lane.sh reads the real key pair, not the template comment
# ═════════════════════════════════════════════════════════════════════════════

_FIXTURE_LIVEKIT_YAML = """\
port: 7880
rtc:
  tcp_port: 7881
keys:
  # <API_KEY>: <API_SECRET>

  APIfixturekey: fixture-secret-value
turn:
  enabled: false
"""


def _sha12(s: str) -> str:
    import hashlib

    return hashlib.sha256(s.encode()).hexdigest()[:12]


def test_f20_capture_lane_key_extraction_skips_commented_keys(tmp_path: Path):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not on PATH")
    box = tmp_path / "box" / "deploy"
    box.mkdir(parents=True)
    (box / "livekit.yaml").write_text(_FIXTURE_LIVEKIT_YAML, encoding="utf-8")
    (box / "worker.env").write_text("AGENT_NAME=pal\nRT_PUBLIC_NUMBER=+15550000000\n", encoding="utf-8")
    # `docker` must not be reachable: the lk CLI stub fails fast instead of pulling an image.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    out = tmp_path / "inventory.md"
    env = {k: v for k, v in os.environ.items() if k not in ("HOST", "TARGET", "LANE", "OUT", "CAPTURE_LOCAL")}
    env.update({
        "CAPTURE_LOCAL": "1", "HOST": "test@localhost", "TARGET": str(tmp_path / "box"),
        "LANE": "fixture", "OUT": str(out), "PATH": f"{bin_dir}:{env.get('PATH', '/usr/bin:/bin')}",
    })
    proc = subprocess.run(  # noqa: S603 - fixed argv, our own tracked script, docker stubbed
        [bash, str(CAPTURE_LANE)], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"capture-lane.sh failed:\n{proc.stdout}\n{proc.stderr}"
    text = out.read_text(encoding="utf-8")
    assert f"fingerprint `{_sha12('APIfixturekey')}`" in text, f"the key fingerprint is not the real key's:\n{text}"
    assert _sha12("#<API_KEY>") not in text and _sha12("") not in text, "capture-lane.sh fingerprinted the template comment (or nothing)"
    for secret in ("APIfixturekey", "fixture-secret-value"):
        assert secret not in text and secret not in proc.stdout + proc.stderr, f"capture-lane.sh wrote the {secret!r} value"
    assert not (DEPLOY_DIR / "provisioning" / "fixture-inventory.md").exists(), "OUT override was ignored; a fixture inventory landed in the tree"


def test_f20_capture_lane_awk_programs_skip_comment_and_blank_lines():
    code = "\n".join(l for l in _read(CAPTURE_LANE).splitlines() if not l.lstrip().startswith("#"))
    for var in ("KEY_AWK", "SECRET_AWK"):
        m = re.search(rf"^{var}='(.*)'$", code, re.M)
        assert m, f"capture-lane.sh must define {var} once and reuse it"
        assert r"(#|$)" in m.group(1) and "continue" in m.group(1), f"{var} does not skip comment/blank lines under keys:"
    assert code.count("${KEY_AWK}") == 1 and code.count("${SECRET_AWK}") == 2, "every livekit.yaml read must go through the shared awk"
    assert not re.search(r"/\^keys:/\{getline;", code), "a verbatim `getline` after keys: still reads the template comment"


# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-03, found standing up the trial-pal lane. provision-box.sh copies
# worker.env.example / livekit.yaml.example / sip.yaml.example onto a bare box.
# render-config.sh then saw files at those paths, said "Leaving them alone" and
# exited 0 — and stand-up-lane.sh reads 0 as success, so it carried on to the
# firewall, the carrier and the stack. The lane would have come up on the
# example's placeholder values with nothing in the output that looked wrong.
# An unfilled example holds no credentials, so it is not a config to protect.
# ─────────────────────────────────────────────────────────────────────────────

_DOC_BOX = {"BOX_IP": "198.51.100.7", "BOX_HOST": "root@198.51.100.7"}


def _run_render_over_ssh(tmp_path: Path, box_files: dict[str, str]) -> subprocess.CompletedProcess:
    """render-config.sh in ssh mode against a stub ssh that runs the remote
    command locally, with CONF pointed at a directory standing in for the box."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not on PATH")
    if not shutil.which("openssl"):
        pytest.skip("openssl is not on PATH")

    target = tmp_path / "box"
    conf = target / "deploy"
    conf.mkdir(parents=True)
    for name, body in box_files.items():
        (conf / name).write_text(body, encoding="utf-8")

    binz = tmp_path / "bin"
    binz.mkdir()
    stub = binz / "ssh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# Stub: discard ssh's flags and host, run the remote command right here.\n"
        'exec bash -c "${!#}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    lane = tmp_path / "lane.env"
    lane.write_text(_lane_env_from_template({**_FILLED_LANE, **_DOC_BOX}), encoding="utf-8")

    env = {k: v for k, v in os.environ.items()
           if k not in _FILLED_LANE and k not in ("RT_REQUIRE_PEPPER", "OUT_DIR", "APPLY", "FORCE", "TARGET")}
    env.update({
        "ENV_FILE": str(lane),
        "TARGET": str(target),
        "PATH": f"{binz}{os.pathsep}{os.environ.get('PATH', '')}",
    })
    return subprocess.run(  # noqa: S603 - fixed argv, our own tracked script
        [bash, str(RENDER_CONFIG)], cwd=str(ROOT), env=env,
        capture_output=True, text=True, timeout=60,
    )


def test_f21_render_config_renders_over_an_unfilled_example(tmp_path: Path):
    """The exact shape provision-box.sh leaves behind must not stop the render."""
    proc = _run_render_over_ssh(tmp_path, {"worker.env": _read(WORKER_ENV)})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"render-config.sh failed:\n{out}"
    assert "Leaving them alone" not in out, (
        "a pristine worker.env.example on the box is not a configured lane; "
        f"render-config.sh bailed out of the first stand-up:\n{out}")
    assert "still the unfilled example" in out, f"the skip was not reported:\n{out}"


def test_f21_render_config_still_refuses_a_filled_config(tmp_path: Path):
    """The protection itself has to survive: a real config is never clobbered."""
    filled = _read(WORKER_ENV) + "\nLIVEKIT_API_KEY=APIliveandinuse\n"
    proc = _run_render_over_ssh(tmp_path, {"worker.env": filled})
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"render-config.sh failed:\n{out}"
    assert "Leaving them alone" in out, (
        f"a filled worker.env on the box must stop the render without FORCE=1:\n{out}")


# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-03. stand-up-lane.sh rsyncs deploy/ to the box and excluded
# 'provisioning/lane.env' — a pattern with a slash, which rsync anchors to the
# transfer root. trial-pal's lane.env lives at provisioning/trial-pal/lane.env,
# one directory deeper, because .gitignore matches on the FILENAME and a lane
# needs its own copy. So it did not match, and the first stand-up of the second
# lane would have copied LINODE_TOKEN and SUPABASE_ACCESS_TOKEN onto the box —
# the two credentials that are deliberately NOT in worker.env, and that can
# rebuild the host and administer the database. lane.env is carried in a
# password manager; it never lands on a box. The pattern is now unanchored.
# ─────────────────────────────────────────────────────────────────────────────

STAND_UP = DEPLOY_DIR / "provisioning" / "stand-up-lane.sh"


def _deploy_rsync_excludes() -> list[str]:
    """The --exclude patterns on the rsync that ships deploy/ to the box."""
    src = _read(STAND_UP)
    m = re.search(r"rsync -az((?:[^\n]|\\\n)*?)\"\$\{REPO\}/deploy/\"", src)
    assert m, "stand-up-lane.sh no longer has an rsync of deploy/ to the box"
    return re.findall(r"--exclude '([^']+)'", m.group(1))


def test_f21_stand_up_never_ships_a_lane_env_to_the_box(tmp_path: Path):
    """No file named lane.env transfers, at any depth under deploy/."""
    if not shutil.which("rsync"):
        pytest.skip("rsync is not on PATH")
    src = tmp_path / "deploy"
    (src / "provisioning" / "trial-pal").mkdir(parents=True)
    (src / "provisioning" / "lane.env").write_text("TOKEN=root-of-the-lane\n", encoding="utf-8")
    (src / "provisioning" / "trial-pal" / "lane.env").write_text("LINODE_TOKEN=nested\n", encoding="utf-8")
    (src / "provisioning" / "lane.env.example").write_text("LINODE_TOKEN=\n", encoding="utf-8")

    argv = [shutil.which("rsync"), "-azn", "--out-format=%n"]
    for pat in _deploy_rsync_excludes():
        argv += ["--exclude", pat]
    argv += [f"{src}/", str(tmp_path / "box") + "/"]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)  # noqa: S603 - argv from our own tracked script
    assert proc.returncode == 0, proc.stderr
    shipped = [l.strip() for l in proc.stdout.splitlines() if l.strip()]

    leaked = [f for f in shipped if Path(f).name == "lane.env"]
    assert not leaked, (
        "stand-up-lane.sh would copy a lane.env onto the box. It holds "
        f"LINODE_TOKEN and SUPABASE_ACCESS_TOKEN, which worker.env deliberately "
        f"does not: {leaked}")
    assert any(Path(f).name == "lane.env.example" for f in shipped), (
        "the tracked lane.env.example template must still ship")


def test_f21_lane_env_exclusion_is_not_anchored_to_one_path():
    """A slash in the pattern anchors it; every lane keeps its file one deeper."""
    pats = _deploy_rsync_excludes()
    assert "lane.env" in pats, f"deploy/ rsync must exclude a bare 'lane.env': {pats}"
    anchored = [p for p in pats if p.endswith("/lane.env")]
    assert not anchored, (
        f"an anchored lane.env exclude only matches one lane's file: {anchored}")


# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-03, standing up trial-pal. recreate-routing.sh read the LiveKit pair
# out of livekit.yaml with a single `getline` after /^keys:/. The tracked
# template opens that block with a comment — "# <API_KEY>: <API_SECRET>" — so
# getline read the comment, and the pair became key "#<API_KEY>", secret
# "<API_SECRET>". Both non-empty, so the script's own guard passed. LiveKit
# answered 401 Unauthorized and the lane could not be routed. render-config.sh
# already skips comments writing that block; this reads it back the same way.
# ─────────────────────────────────────────────────────────────────────────────

RECREATE_ROUTING = DEPLOY_DIR / "provisioning" / "recreate-routing.sh"
LIVEKIT_TEMPLATE = DEPLOY_DIR / "livekit.yaml.example"


def _parse_livekit_pair(yaml_path: Path) -> tuple[str, str]:
    """Run recreate-routing.sh's OWN key-parsing block against a livekit.yaml."""
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash is not on PATH")
    src = _read(RECREATE_ROUTING)
    m = re.search(r'(read -r KEY SECRET <<<"\$\(awk .*?\}\' "\$LK_YAML"\)")', src, re.S)
    assert m, "recreate-routing.sh no longer parses the pair with a read/awk block"
    script = f'LK_YAML={yaml_path}\n{m.group(1)}\nprintf "%s|%s" "$KEY" "$SECRET"\n'
    proc = subprocess.run([bash, "-c", script], capture_output=True, text=True, timeout=30)  # noqa: S603 - our own tracked script
    assert proc.returncode == 0, proc.stderr
    key, _, secret = proc.stdout.partition("|")
    return key, secret


def test_f21_routing_reads_the_key_past_the_template_comment(tmp_path: Path):
    """The fixture is the tracked template, so every lane renders this shape."""
    rendered = _read(LIVEKIT_TEMPLATE).replace(
        "APIxxxxxxxxxxxx: replace-me-with-the-generated-secret",
        "APIyFRNzIQP5EWC: FBnEt0Xk9uTnKzQ2mVwLpR7sYd3JhCgAe1ZxNqMvB4s",
    )
    lk = tmp_path / "livekit.yaml"
    lk.write_text(rendered, encoding="utf-8")

    key, secret = _parse_livekit_pair(lk)
    assert key == "APIyFRNzIQP5EWC", (
        f"recreate-routing.sh read {key!r} as the LiveKit API key. The keys: block "
        "opens with a comment line, and a key that is not the real one authenticates "
        "against nothing — LiveKit answers 401 and the lane cannot be routed.")
    assert secret == "FBnEt0Xk9uTnKzQ2mVwLpR7sYd3JhCgAe1ZxNqMvB4s", f"secret read as {secret!r}"
    assert not key.startswith("#"), "a comment was read as the key"


def test_f21_routing_refuses_a_keys_block_it_cannot_read(tmp_path: Path):
    """Comment-only block: better to fail loudly than authenticate as nothing."""
    lk = tmp_path / "livekit.yaml"
    lk.write_text("keys:\n  # <API_KEY>: <API_SECRET>\nturn:\n  enabled: false\n", encoding="utf-8")
    key, secret = _parse_livekit_pair(lk)
    assert not key and not secret, f"a keys: block with no key must parse as empty, got {key!r}"


# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-03, trial-pal. The dispatch rule was written flat — name, trunk_ids,
# rule, room_config at the top level — which is the deprecated half of
# CreateSIPDispatchRuleRequest. lk 2.18.5 reads the nested half and rejected it
# with "missing rule" while a top-level "rule" key was sitting right there.
# trunk.json was already wrapped in "trunk"; the rule now wraps the same way.
# ─────────────────────────────────────────────────────────────────────────────

def _routing_json_block(marker: str) -> dict:
    """The JSON heredoc recreate-routing.sh writes, with its shell vars stubbed."""
    src = _read(RECREATE_ROUTING)
    m = re.search(rf'cat > "\$TMP/{marker}" <<JSON\n(.*?)\nJSON', src, re.S)
    assert m, f"recreate-routing.sh no longer writes {marker} as a JSON heredoc"
    body = m.group(1)
    for var, val in (("${LANE}", "trial-pal"), ("${TRUNK_ID}", "ST_test"),
                     ("${ROOM_PREFIX}", "phone-"), ("${AGENT}", "trial-pal"),
                     ("${NUMBER}", "+19086571294")):
        body = body.replace(var, val)
    body = re.sub(r"\$\{TWILIO_CIDRS\}|\$TWILIO_CIDRS", '["0.0.0.0/0"]', body)
    return json.loads(body)


def test_f21_dispatch_rule_uses_the_nested_request_shape():
    rule = _routing_json_block("rule.json")
    assert "dispatch_rule" in rule, (
        "the flat CreateSIPDispatchRuleRequest is the deprecated half; lk 2.18.5 "
        f"rejects it with 'missing rule'. Got top-level keys: {sorted(rule)}")
    inner = rule["dispatch_rule"]
    assert inner.get("trunkIds") == ["ST_test"], f"trunkIds missing: {sorted(inner)}"
    assert "dispatchRuleIndividual" in inner.get("rule", {}), f"rule missing: {inner.get('rule')}"
    agents = inner.get("roomConfig", {}).get("agents", [])
    assert agents and agents[0].get("agentName") == "trial-pal", (
        f"the rule must dispatch to the lane's agent, got {agents}")


def test_f21_trunk_and_rule_are_wrapped_the_same_way():
    """Both requests nest their object; neither is flat."""
    assert "trunk" in _routing_json_block("trunk.json")
    assert "dispatch_rule" in _routing_json_block("rule.json")


# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-03, first boot of the trial-pal lane. Every provisioning step passed,
# all seven containers came up, and phone-pal-worker-1 crash-looped on
# ValueError: invalid literal for int() with base 10: ''. The Google plugin runs
# int(os.getenv("LK_GOOGLE_DEBUG", 0)) at import; the default applies only when
# the key is ABSENT, and worker.env.example shipped it present and empty. Every
# lane rendered from the template was born crash-looping. Dev never showed it
# because someone had set the value to 1 by hand months earlier.
# ─────────────────────────────────────────────────────────────────────────────

def test_f21_worker_env_template_never_ships_a_blank_lk_google_debug():
    for line in _read(WORKER_ENV).splitlines():
        if line.startswith("LK_GOOGLE_DEBUG="):
            val = line.split("=", 1)[1].split("#")[0].strip()
            assert val, (
                "worker.env.example ships LK_GOOGLE_DEBUG with an empty value. The "
                "plugin int()s it at import and an empty string is not the default, "
                "it is a crash — every lane rendered from this template crash-loops.")
            int(val)  # must be parseable by the plugin
            break
    else:
        pytest.fail("worker.env.example no longer declares LK_GOOGLE_DEBUG")


def test_f21_agent_unsets_a_blank_lk_google_debug_before_importing_the_plugin():
    """Order matters: the guard is useless if the import runs first."""
    src = _read(AGENT_PY)
    guard = src.find('os.environ.pop("LK_GOOGLE_DEBUG"')
    imp = src.find("from livekit.plugins import google")
    assert guard != -1, (
        "agent.py must treat a blank LK_GOOGLE_DEBUG as unset; a rendered worker.env "
        "or a hand-edited one can still carry the empty value that crashes the import")
    assert imp != -1, "agent.py no longer imports the google plugin"
    assert guard < imp, (
        "the LK_GOOGLE_DEBUG guard must come BEFORE the plugin import — the plugin "
        "reads the variable at import time, so a guard after it never runs")


def test_f21_blank_lk_google_debug_would_crash_the_plugin_expression():
    """The exact expression the plugin evaluates, so this test states the why."""
    with pytest.raises(ValueError):
        int(os.environ.get("_ABSENT_ON_PURPOSE", "") or "")
    env = {"LK_GOOGLE_DEBUG": ""}
    if not (env.get("LK_GOOGLE_DEBUG") or "").strip():
        env.pop("LK_GOOGLE_DEBUG", None)
    assert int(env.get("LK_GOOGLE_DEBUG", 0)) == 0, "the guard must restore the plugin's default"
