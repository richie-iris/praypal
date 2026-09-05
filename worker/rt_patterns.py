"""rt_patterns.py — the one regex that is genuinely shared.

This file used to hold seven "centralized" patterns and export exactly one of
them to one caller. The other six were never imported anywhere, and each had a
live twin elsewhere with DIFFERENT behaviour:

  FAREWELL_PHRASE  a loose substring match — precisely the bug agent.py's
                   _is_farewell was rewritten to kill after it hung up on
                   someone mid-sentence for saying "have a good day" about
                   their daughter. Anyone who "centralized" onto this would
                   have reintroduced a known production defect.
  SSN_PATTERN      broader than rt_prefs._SSN; would have scrubbed ordinary
                   long digit runs out of stored facts.
  PHONE_IN_TEXT    no capture groups, so agent._register_dialable could not
                   have used it at all.
  BAIT_QUERY       refuses "amazon", "apple", "888" — rt_bridge._BAIT_QUERY
                   deliberately does not, because those are real lookups.
  TRANSIENT_PAUSE  duplicated inline in rt_postcall_worker.
  SPELLED_RUN      duplicated in rt_prefs, which also needs despell/join_spelled
                   alongside it.

A registry that nothing reads is not a registry; it is a loaded gun pointed at
the next person who tidies up. Deleted. What remains is the one pattern with a
real second caller.
"""
from __future__ import annotations

import re

CLINICAL = re.compile(
    r"\b(dr|doctor|md|do|np|pa|dds|dmd|physician|clinic|hospital|medical|medicine|"
    r"health|care|pharmacy|walgreens|cvs|rite aid|primary care|pediatric|"
    r"cardiolog(?:y|ist)?|orthoped(?:ic|ics|ist)?|dermatolog(?:y|ist)?|dentist|dental|"
    r"optometri(?:st|c|sts)?|ophthalmolog(?:y|ist)?|psychiatri(?:c|st)?|therapy|"
    r"therapist|counselor|urgent care|er|emergency room|nursing home|assisted living)\b",
    re.IGNORECASE,
)

if __name__ == "__main__":
    for w in ("Dr. Patel", "my cardiologist", "the dermatologist", "an optometrist",
              "her psychiatrist", "orthopedic surgeon", "the dentist"):
        if not CLINICAL.search(w):
            raise SystemExit(f"CLINICAL missed {w!r}")
    for w in ("Frank's Pizza", "the senior center", "the hardware store"):
        if CLINICAL.search(w):
            raise SystemExit(f"CLINICAL false positive {w!r}")
    print("✅ CLINICAL validated")
