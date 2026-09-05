#!/usr/bin/env python3
"""backfill_facts.py — Phase 2 of the memory plan: prose & registry -> rt.facts.

One-time (but idempotent) conversion of the legacy stores into fact rows, so
the streamlined hydrator's WHAT YOU KNOW block has content for callers who
predate the fact store. Re-running is safe by construction: rt_upsert_fact
CONFIRMS identical values instead of duplicating them, so a second run just
bumps confirmation counts by one.

Reads each caller's registry domain blobs (family, pets, health, hobbies,
work, places, preferences) and prose scalars (caller_rules, persona_directives)
and pushes them through the exact same derive/dual_write path live calls use —
one write path, no special backfill logic to drift.

Usage:
    .venv/bin/python scripts/backfill_facts.py            # all callers
    .venv/bin/python scripts/backfill_facts.py --dry-run  # show, write nothing
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env.dev")
load_dotenv(ROOT / ".env.local")

import rt_facts
import rt_prefs

_DOMAINS = ("family", "pets", "health", "hobbies", "work", "places", "preferences", "vehicles", "finances")


def main() -> None:
    dry = "--dry-run" in sys.argv
    callers = rt_prefs._req("POST", "rpc/rt_get_all_callers", {}) or []
    if not isinstance(callers, list):
        print("could not list callers"); sys.exit(1)
    total = {"insert": 0, "confirm": 0, "supersede": 0, "failed": 0, "skipped": 0, "refused": 0}
    for c in callers:
        h = c.get("phone_hash")
        if not h:
            continue
        bundle = rt_prefs._req("POST", "rpc/rt_get_caller_full_bundle", {"p_hash": h}) or {}
        registry = bundle.get("schemas") or []
        caller = bundle.get("caller") or {}

        pseudo: dict = {}
        for e in registry:
            cat = (e.get("category") or "").lower()
            if cat not in _DOMAINS:
                continue
            try:
                data = json.loads(e.get("data_summary") or "{}")
            except Exception:
                data = {"detail": e.get("data_summary")}
            if data:
                pseudo[cat] = data
        for scalar in ("caller_rules", "persona_directives"):
            if (caller.get(scalar) or "").strip():
                pseudo[scalar] = caller[scalar]

        if not pseudo:
            continue
        name = caller.get("display_name") or "?"
        if dry:
            facts = rt_facts.derive_facts(pseudo)
            print(f"  {name:<10} {h[:12]}… would write {len(facts)}: "
                  f"{[f['p_norm_key'] for f in facts]}")
            continue
        tally = rt_facts.dual_write(h, pseudo, call_id="BACKFILL_2026_08_15")
        print(f"  {name:<10} {h[:12]}… {tally}")
        for k in total:
            total[k] += tally.get(k, 0)
    print(f"\nTOTAL: {total}")


if __name__ == "__main__":
    main()
