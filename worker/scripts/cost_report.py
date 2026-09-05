#!/usr/bin/env python3
"""cost_report.py — what an hour of Phone-Pal actually costs.

rt_costs.py has always been able to price ONE call. Nothing ever asked the
question the business needs answered: what does an hour cost, what does a
concurrent caller cost, and what happens to the bill at a thousand of them.

This reads real traced calls out of rt.calls, prices each one with the same
rt_costs.estimate() the worker uses (so the report and the ledger can never
disagree), and buckets them by hour.

    python scripts/cost_report.py                 # last 24h, hourly
    python scripts/cost_report.py --days 7        # a week
    python scripts/cost_report.py --project 500   # forecast at 500 calls/day
    python scripts/cost_report.py --json

HONESTY ABOUT THE NUMBERS. Every rate in rt_costs.RATES was read off a public
pricing page in August 2026 and is overridable by environment variable. This is
an estimate with its workings shown, never a bill. Where the sample is too small
to mean anything, the report says so rather than extrapolating from three calls
and presenting it as a run rate.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env.local"))
load_dotenv(os.path.join(ROOT, ".env"))

import rt_costs  # noqa: E402
import rt_prefs  # noqa: E402

# Below this many calls an "average" is noise, not a metric.
_MEANINGFUL = 10


def _arg(flag: str, default=None, cast=str):
    if flag in sys.argv:
        try:
            return cast(sys.argv[sys.argv.index(flag) + 1])
        except Exception:
            return default
    return default


def fetch(days: int) -> tuple[list[dict], dict[str, list[dict]]]:
    """Traced calls in the window, plus their events keyed by call_id."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    # rt.* lives outside the PostgREST-exposed schema, so go through the same
    # console RPC the operator UI uses. Reading calls two different ways is how
    # a report and a dashboard end up disagreeing about the same day.
    calls = rt_prefs._req("POST", "rpc/rt_console_calls", {"p_limit": 5000}) or []
    calls = [c for c in calls if (c.get("started_at") or "") >= since]
    # One fetch, grouped locally — the same shape call_console.py uses. Asking
    # per call would be one round trip per call for a report that already knows
    # it wants all of them.
    events: dict[str, list[dict]] = defaultdict(list)
    for r in (rt_prefs._req("POST", "rpc/rt_console_events", {"p_limit": 20000}) or []):
        events[r.get("call_id")].append(r)
    return calls, events


def main() -> None:
    days = _arg("--days", 1, int)
    project = _arg("--project", None, int)
    as_json = "--json" in sys.argv

    try:
        calls, events = fetch(days)
    except Exception as exc:
        sys.exit(f"could not read rt.calls: {exc}")

    if not calls:
        print(f"No traced calls in the last {days}d. "
              f"Nothing to price — this is a real answer, not an error.")
        return

    priced = []
    for c in calls:
        try:
            est = rt_costs.estimate(c, events.get(c["call_id"], []))
        except Exception as e:
            print(f"[cost-report] skipped {c.get('call_id')}: {e}", file=sys.stderr)
            continue
        priced.append((c, est))

    buckets: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "usd": 0.0, "minutes": 0.0, "in_tok": 0, "out_tok": 0,
                 "callers": set()})
    for c, est in priced:
        hour = (c.get("started_at") or "")[:13]
        b = buckets[hour]
        b["calls"] += 1
        b["usd"] += float(est.get("total_usd") or est.get("total") or 0)
        b["minutes"] += float(c.get("duration_sec") or 0) / 60.0
        b["in_tok"] += int(c.get("in_tokens") or 0)
        b["out_tok"] += int(c.get("out_tokens") or 0)
        b["callers"].add(c.get("phone_hash"))

    total_usd = sum(b["usd"] for b in buckets.values())
    total_calls = sum(b["calls"] for b in buckets.values())
    total_min = sum(b["minutes"] for b in buckets.values())
    per_call = total_usd / total_calls if total_calls else 0.0
    per_min = total_usd / total_min if total_min else 0.0
    busiest = max(buckets.items(), key=lambda kv: kv[1]["calls"], default=(None, None))

    if as_json:
        print(json.dumps({
            "window_days": days, "calls": total_calls,
            "total_usd": round(total_usd, 4),
            "usd_per_call": round(per_call, 4),
            "usd_per_minute": round(per_min, 4),
            "minutes": round(total_min, 1),
            "meaningful": total_calls >= _MEANINGFUL,
            "hours": {h: {"calls": b["calls"], "usd": round(b["usd"], 4),
                          "minutes": round(b["minutes"], 1),
                          "distinct_callers": len(b["callers"])}
                      for h, b in sorted(buckets.items())},
        }, indent=2))
        return

    print(f"\n=== COST — last {days}d, {total_calls} calls ===\n")
    print(f"{'HOUR (UTC)':<16}{'CALLS':>6}{'CALLERS':>9}{'MINUTES':>9}"
          f"{'USD':>10}{'USD/CALL':>10}")
    for h, b in sorted(buckets.items()):
        pc = b["usd"] / b["calls"] if b["calls"] else 0
        print(f"{h:<16}{b['calls']:>6}{len(b['callers']):>9}{b['minutes']:>9.1f}"
              f"{b['usd']:>10.4f}{pc:>10.4f}")

    print(f"\n{'TOTAL':<16}{total_calls:>6}{'':>9}{total_min:>9.1f}{total_usd:>10.4f}"
          f"{per_call:>10.4f}")
    print(f"\n  per call    ${per_call:.4f}")
    print(f"  per minute  ${per_min:.4f}")
    if busiest[0]:
        print(f"  busiest hour {busiest[0]}  ({busiest[1]['calls']} calls, "
              f"${busiest[1]['usd']:.4f})")

    if total_calls < _MEANINGFUL:
        print(f"\n  NOT A RUN RATE. {total_calls} calls is too small a sample to "
              f"average. Treat these as individual observations, not a forecast.")
    elif project:
        print(f"\n  At {project} calls/day, holding this per-call average:")
        print(f"    daily   ${per_call * project:,.2f}")
        print(f"    monthly ${per_call * project * 30:,.2f}")
        print(f"    yearly  ${per_call * project * 365:,.2f}")
        print("    (assumes call length and tool use hold at today's mix)")

    print("\n  Rates are estimates read from public pricing, overridable by env.")
    print("  This is a model with its workings shown, not a bill.\n")


if __name__ == "__main__":
    main()
