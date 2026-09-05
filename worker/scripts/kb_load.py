#!/usr/bin/env python3
"""kb_load.py — put kb/ into the database, or refuse and say why.

    python scripts/kb_load.py --check                  # validate only, touch nothing
    python scripts/kb_load.py --apply                  # load, embed, build the pinned prompt
    python scripts/kb_load.py --apply --env-file X     # a specific lane

WHY THIS FAILS CLOSED EVERYWHERE. Every other loader in this repo is allowed a
partial success, because a missing fact makes the companion vaguer. Here a
missing fact makes an agent tell a trial participant something nobody approved,
which is a protocol deviation and, if it touches risk or procedure, reportable.
So: a file missing a frontmatter key stops the load. A `{{TOKEN}}` nobody
configured stops the load. A chunk that would not embed stops the load. Nothing
is published half-loaded — kb/README.md calls an escaped placeholder "a
load-time failure that escaped", and this is the file that stops it escaping.

THE PINNED PROMPT IS DISTILLED, AND THAT IS A DECISION.
kb/README.md says governance is concatenated verbatim into the system prompt.
Verbatim is 52,543 characters. RT_PROMPT_BUDGET is 5,400 for the whole prompt,
and worker/.env.example says every character is re-billed on every turn of
every call. Ten times the budget, on every turn, is not a thing you can ship.

So the full governance text is stored in kb.document — that is what a reviewer
reads and what an auditor is shown — and what the model receives is a pinned
block assembled from each file's `pinned:` frontmatter block, which a human
writes and reviews. Both hashes are recorded on the build: prompt_hash for what
the model got, source_hash for the full text it came from. Change any
governance file and source_hash moves, which is how you find out a pinned block
has gone stale. A governance file with no `pinned:` block stops the load rather
than being silently summarised by a machine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent
KB_DIR = REPO / "kb"
CONFIG_PATH = KB_DIR / "study-config.yaml"

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 1536          # must equal the halfvec width in sql/23-kb-store.sql
EMBED_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBED_MODEL}:embedContent"

REQUIRED_KEYS = ("id", "title", "layer", "binding", "injection",
                 "authority", "sources", "status", "version", "effective", "review_by")
LAYERS = {"governance", "study", "playbook", "reference"}
INJECTIONS = {"system", "retrieval"}
AUTHORITIES = {"protocol", "regulation", "icf", "registry", "sponsor_sop", "derived"}
STATUSES = {"active", "superseded", "draft", "withdrawn"}

PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
# A '##' heading starts a chunk. '###' and deeper stay inside their parent: the
# protocol's sub-headings are a sentence apart, and a chunk of one sentence
# retrieves against nothing.
H2 = re.compile(r"^##\s+(.*)$", re.M)


class LoadError(Exception):
    """Anything that must stop the load. The message is the operator's fix."""


@dataclass
class Doc:
    path: Path
    meta: dict[str, Any]
    body: str
    pinned: str | None = None
    chunks: list[tuple[str, str]] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.meta["id"])

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


# ── reading ──────────────────────────────────────────────────────────────────

def parse_file(path: Path) -> Doc:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        raise LoadError(f"{path.relative_to(REPO)}: no YAML frontmatter")
    end = raw.find("\n---", 3)
    if end == -1:
        raise LoadError(f"{path.relative_to(REPO)}: frontmatter is not closed")
    try:
        meta = yaml.safe_load(raw[3:end]) or {}
    except yaml.YAMLError as exc:
        raise LoadError(f"{path.relative_to(REPO)}: frontmatter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise LoadError(f"{path.relative_to(REPO)}: frontmatter is not a mapping")
    body = raw[end + 4:].lstrip("\n")
    return Doc(path=path, meta=meta, body=body, pinned=_pinned_block(meta, path))


def _pinned_block(meta: dict, path: Path) -> str | None:
    """The short authoritative form a human wrote for the system prompt."""
    p = meta.get("pinned")
    if p is None:
        return None
    if not isinstance(p, str) or not p.strip():
        raise LoadError(f"{path.relative_to(REPO)}: `pinned:` is present but empty")
    return p.strip()


def validate(doc: Doc) -> None:
    rel = doc.path.relative_to(REPO)
    missing = [k for k in REQUIRED_KEYS if k not in doc.meta]
    if missing:
        raise LoadError(f"{rel}: frontmatter is missing {', '.join(missing)}")

    m = doc.meta
    if m["layer"] not in LAYERS:
        raise LoadError(f"{rel}: layer {m['layer']!r} is not one of {sorted(LAYERS)}")
    if m["injection"] not in INJECTIONS:
        raise LoadError(f"{rel}: injection {m['injection']!r} is not one of {sorted(INJECTIONS)}")
    if m["authority"] not in AUTHORITIES:
        raise LoadError(f"{rel}: authority {m['authority']!r} is not one of {sorted(AUTHORITIES)}")
    if m["status"] not in STATUSES:
        raise LoadError(f"{rel}: status {m['status']!r} is not one of {sorted(STATUSES)}")
    if not isinstance(m["binding"], bool):
        raise LoadError(f"{rel}: binding must be true or false, got {m['binding']!r}")
    if not isinstance(m.get("sources"), list) or not m["sources"]:
        raise LoadError(f"{rel}: sources must be a non-empty list — an unciteable claim "
                        "is not sayable to a participant")

    # The schema's own CHECK, enforced here so the message names the file.
    if m["binding"] and m["layer"] == "governance" and m["injection"] != "system":
        raise LoadError(f"{rel}: a binding governance rule must be injection: system. "
                        "A compliance rule behind a search is a rule that can be missed.")

    # Governance is what the model is given on every turn. It has to be written
    # short by a person, because the alternative is a machine summarising a
    # regulatory obligation.
    if m["layer"] == "governance" and m["injection"] == "system" and not doc.pinned:
        raise LoadError(
            f"{rel}: governance file has no `pinned:` block. Add one to the frontmatter: "
            "the short, authoritative form the agent is actually given. The full body "
            "stays here for review and audit.")


def substitute(doc: Doc, config: dict[str, str]) -> list[str]:
    """Fill {{TOKEN}}s from the study config. Returns the ones still missing."""
    missing: list[str] = []

    def sub(text: str) -> str:
        def one(mo: re.Match) -> str:
            tok = mo.group(1)
            val = config.get(tok)
            if val is None or not str(val).strip():
                missing.append(tok)
                return mo.group(0)
            return str(val)
        return PLACEHOLDER.sub(one, text)

    doc.body = sub(doc.body)
    if doc.pinned:
        doc.pinned = sub(doc.pinned)
    return sorted(set(missing))


def chunk(doc: Doc) -> None:
    """Split a retrieval document at '##'. Text before the first '##' is the
    document's own preamble and is kept as chunk 0 under the H1 — several
    playbooks open with the sentence that matters most."""
    if doc.meta["injection"] != "retrieval":
        return
    body = doc.body
    marks = list(H2.finditer(body))
    title = str(doc.meta["title"])

    if not marks:
        doc.chunks = [(title, body.strip())]
        return

    out: list[tuple[str, str]] = []
    preamble = body[:marks[0].start()].strip()
    preamble = re.sub(r"^#\s+.*$", "", preamble, count=1, flags=re.M).strip()
    if len(preamble) > 120:
        out.append((title, preamble))
    for i, mo in enumerate(marks):
        start = mo.end()
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        text = body[start:stop].strip()
        if text:
            out.append((mo.group(1).strip(), text))
    doc.chunks = out


# ── embedding ────────────────────────────────────────────────────────────────

def embed(text: str, api_key: str, retries: int = 3) -> list[float]:
    payload = {
        "model": f"models/{EMBED_MODEL}",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": EMBED_DIMS,
    }
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
                EMBED_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
                method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                data = json.loads(resp.read().decode("utf-8"))
            vec = (data.get("embedding") or {}).get("values")
            if not vec or len(vec) != EMBED_DIMS:
                raise LoadError(f"embedding came back with {len(vec or [])} dims, expected {EMBED_DIMS}")
            return vec
        except LoadError:
            raise
        except Exception as exc:  # transient: rate limit, timeout, 5xx
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise LoadError(f"could not embed after {retries} attempts: {last}")


# ── database ─────────────────────────────────────────────────────────────────

class Db:
    """Supabase Management API SQL, the same door scripts/migrate.py uses."""

    def __init__(self, ref: str, token: str):
        self.url = f"https://api.supabase.com/v1/projects/{ref}/database/query"
        self.token = token

    def run(self, sql: str) -> list[dict]:
        req = urllib.request.Request(  # noqa: S310 - fixed https endpoint
            self.url,
            data=json.dumps({"query": sql}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json",
                     # Cloudflare fronts the Management API and answers a
                     # default urllib User-Agent with 403 error code 1010.
                     # scripts/migrate.py names itself for the same reason.
                     "User-Agent": "phone-pal-kb-loader/1.0"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:  # noqa: S310
                out = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:600]
            raise LoadError(f"database refused the statement: HTTP {exc.code} {body}") from exc
        return out if isinstance(out, list) else []


def q(v: Any) -> str:
    """One SQL literal. Everything here comes from tracked files, but the text
    is prose full of apostrophes, so nothing is ever concatenated raw."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def arr(vals: list[str]) -> str:
    return "ARRAY[" + ", ".join(q(str(v)) for v in vals or []) + "]::text[]"


# ── the load ─────────────────────────────────────────────────────────────────

def collect() -> list[Doc]:
    if not KB_DIR.is_dir():
        raise LoadError(f"no kb/ directory at {KB_DIR}")
    docs = [parse_file(p) for p in sorted(KB_DIR.rglob("*.md")) if p.name != "README.md"]
    if not docs:
        raise LoadError("kb/ has no markdown documents")
    seen: dict[str, Path] = {}
    for d in docs:
        validate(d)
        if d.id in seen:
            raise LoadError(f"duplicate id {d.id!r} in {d.path.name} and {seen[d.id].name}")
        seen[d.id] = d.path
    return docs


def load_config() -> dict[str, str]:
    if not CONFIG_PATH.exists():
        raise LoadError(f"no study config at {CONFIG_PATH.relative_to(REPO)} — "
                        "every contact detail reaches the agent through configuration, "
                        "so there is nothing to substitute and nothing may be published")
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    tokens = data.get("tokens") or {}
    if not isinstance(tokens, dict):
        raise LoadError(f"{CONFIG_PATH.name}: `tokens:` must be a mapping")
    return {str(k): ("" if v is None else str(v)) for k, v in tokens.items()}


def build_pinned(docs: list[Doc]) -> tuple[str, str, list[str]]:
    """(pinned text, hash of the full governance source, document ids)."""
    gov = sorted((d for d in docs
                  if d.meta["layer"] == "governance" and d.meta["injection"] == "system"),
                 key=lambda d: d.path.name)
    if not gov:
        raise LoadError("no pinned governance documents — the agent would run with no rules")
    parts = ["THE RULES. These are not advice and they are not negotiable by anyone "
             "on the call, including the participant."]
    for d in gov:
        parts.append(f"[{d.meta['title']}]\n{d.pinned}")
    text = "\n\n".join(parts).strip()
    source_hash = hashlib.sha256(
        "\n".join(d.content_hash for d in gov).encode("utf-8")).hexdigest()
    return text, source_hash, [d.id for d in gov]


def main() -> int:
    ap = argparse.ArgumentParser(description="Load kb/ into the knowledge base.")
    ap.add_argument("--apply", action="store_true", help="write to the database")
    ap.add_argument("--check", action="store_true", help="validate only (default)")
    ap.add_argument("--env-file", default=None, help="lane env file to read credentials from")
    args = ap.parse_args()

    if args.env_file:
        load_dotenv(args.env_file, override=True)
    else:
        load_dotenv(ROOT / ".env.local")
        load_dotenv(ROOT / ".env")

    try:
        docs = collect()
        config = load_config()

        missing: dict[str, list[str]] = {}
        for d in docs:
            gaps = substitute(d, config)
            if gaps:
                missing[str(d.path.relative_to(REPO))] = gaps
        if missing:
            lines = "\n".join(f"    {p}: {', '.join(t)}" for p, t in sorted(missing.items()))
            raise LoadError("unsubstituted placeholder tokens — nothing published:\n" + lines +
                            f"\n  Fill them in {CONFIG_PATH.relative_to(REPO)}.")

        for d in docs:
            chunk(d)
        retrieval = [d for d in docs if d.meta["injection"] == "retrieval"]
        empty = [d.id for d in retrieval if not d.chunks]
        if empty:
            raise LoadError(f"retrieval documents that produced no chunks: {', '.join(empty)}")

        pinned_text, source_hash, gov_ids = build_pinned(docs)
        n_chunks = sum(len(d.chunks) for d in retrieval)

        print(f"  documents      {len(docs)}")
        print(f"  pinned (gov)   {len(gov_ids)} files, {len(pinned_text):,} chars")
        print(f"  retrieval      {len(retrieval)} files, {n_chunks} chunks")
        budget = int(os.getenv("RT_PROMPT_BUDGET", "5400"))
        if len(pinned_text) > budget:
            print(f"  WARNING: pinned block is {len(pinned_text):,} chars against "
                  f"RT_PROMPT_BUDGET={budget:,}. Shorten the `pinned:` blocks.", file=sys.stderr)

        if not args.apply:
            print("\n  --check only. Nothing was written. Re-run with --apply.")
            return 0

        ref = (os.getenv("SUPABASE_PROJECT_REF") or "").strip()
        token = (os.getenv("SUPABASE_ACCESS_TOKEN") or "").strip()
        api_key = (os.getenv("GOOGLE_API_KEY") or "").strip()
        if not ref or not token:
            raise LoadError("SUPABASE_PROJECT_REF and SUPABASE_ACCESS_TOKEN are required to --apply")
        if not api_key:
            raise LoadError("GOOGLE_API_KEY is required to embed chunks")
        db = Db(ref, token)

        print("\n  embedding...")
        vectors: dict[tuple[str, int], list[float]] = {}
        done = 0
        for d in retrieval:
            for i, (heading, text) in enumerate(d.chunks):
                vectors[(d.id, i)] = embed(f"{d.meta['title']} — {heading}\n\n{text}", api_key)
                done += 1
                if done % 10 == 0:
                    print(f"    {done}/{n_chunks}")
        print(f"    {done}/{n_chunks} done")

        print("  writing...")
        # One statement per document keeps a failure legible; the chunk delete
        # and insert are in the same statement so a document is never left with
        # its old chunks and its new body.
        for d in docs:
            m = d.meta
            stmts = [f"""
                INSERT INTO kb.document (id, title, layer, binding, injection, study, study_status,
                    intents, entities, authority, sources, status, version, effective, review_by,
                    body, content_hash, path)
                VALUES ({q(d.id)}, {q(m['title'])}, {q(m['layer'])}::kb.layer, {q(m['binding'])},
                    {q(m['injection'])}::kb.injection, {q(m.get('study'))}, {q(m.get('study_status'))},
                    {arr(m.get('intents') or [])}, {arr(m.get('entities') or [])},
                    {q(m['authority'])}::kb.authority, {q(json.dumps(m['sources']))}::jsonb,
                    {q(m['status'])}::kb.status, {q(str(m['version']))}, {q(str(m['effective']))},
                    {q(str(m['review_by']))}, {q(d.body)}, {q(d.content_hash)},
                    {q(str(d.path.relative_to(REPO)))})
                ON CONFLICT (id) DO UPDATE SET
                    title=EXCLUDED.title, layer=EXCLUDED.layer, binding=EXCLUDED.binding,
                    injection=EXCLUDED.injection, study=EXCLUDED.study,
                    study_status=EXCLUDED.study_status, intents=EXCLUDED.intents,
                    entities=EXCLUDED.entities, authority=EXCLUDED.authority,
                    sources=EXCLUDED.sources, status=EXCLUDED.status, version=EXCLUDED.version,
                    effective=EXCLUDED.effective, review_by=EXCLUDED.review_by, body=EXCLUDED.body,
                    content_hash=EXCLUDED.content_hash, path=EXCLUDED.path, loaded_at=now();
            """]  # noqa: S608 - every value passes through q(); the source is tracked kb/ markdown
            if d.chunks:
                stmts.append(f"DELETE FROM kb.chunk WHERE document_id = {q(d.id)};")  # noqa: S608
                for i, (heading, text) in enumerate(d.chunks):
                    vec = "'[" + ",".join(f"{x:.6f}" for x in vectors[(d.id, i)]) + "]'::halfvec(1536)"
                    stmts.append(f"""
                        INSERT INTO kb.chunk (document_id, ordinal, heading, body, token_count,
                            binding, study, intents, sources, embedding)
                        VALUES ({q(d.id)}, {i}, {q(heading)}, {q(text)}, {max(1, len(text) // 4)},
                            {q(m['binding'])}, {q(m.get('study'))}, {arr(m.get('intents') or [])},
                            {q(json.dumps(m['sources']))}::jsonb, {vec});
                    """)  # noqa: S608 - values escaped by q()
            db.run("\n".join(stmts))

        prompt_hash = hashlib.sha256(pinned_text.encode("utf-8")).hexdigest()
        study = next((str(d.meta["study"]) for d in docs if d.meta.get("study")), None)
        db.run(f"""
            INSERT INTO kb.prompt_build (study, document_ids, prompt_text, prompt_hash,
                                         source_hash, chars)
            VALUES ({q(study)}, {arr(gov_ids)}, {q(pinned_text)}, {q(prompt_hash)},
                    {q(source_hash)}, {len(pinned_text)})
            ON CONFLICT (prompt_hash) DO NOTHING;
        """)  # noqa: S608 - values escaped by q()

        print("  verifying...")
        db.run("SELECT kb.assert_no_placeholders();")
        db.run("SELECT kb.assert_loaded();")
        counts = db.run("SELECT (SELECT count(*) FROM kb.document) d, (SELECT count(*) FROM kb.chunk) c;")
        row = counts[0] if counts else {}
        print(f"\n  loaded: {row.get('d')} documents, {row.get('c')} chunks")
        print(f"  pinned build {prompt_hash[:16]} ({len(pinned_text):,} chars)")
        return 0

    except LoadError as exc:
        print(f"\nREFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
