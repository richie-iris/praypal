"""rt_tools.py — In-Call Agent Tools & Execution Handlers for Phone-Pal.

Provides a clean interface for in-call function tools, database interactions,
and safety guards used by the Phone-Pal real-time voice agent.
"""
from __future__ import annotations


import agent

# Re-export agent class and tool execution functions
RtAgent = agent.RtAgent
_perform_web_search = agent._perform_web_search
_number_heard_from_caller = agent._number_heard_from_caller
_contains_phone_number = agent._contains_phone_number
_number_in_recent_search = agent._number_in_recent_search
_lookup_tier = agent._lookup_tier
_COMMON_WORDS = agent._COMMON_WORDS
_heard_in_caller_lines = agent._heard_in_caller_lines
_FORGET_STOP = agent._FORGET_STOP
_INTERNAL_CATS = agent._INTERNAL_CATS
_internal_cats = agent._internal_cats
_email_spoken_by_caller = agent._email_spoken_by_caller
_WIPE_CONSENT_PHRASES = agent._WIPE_CONSENT_PHRASES
_wipe_consent_line = agent._wipe_consent_line
_wipe_consented_since = agent._wipe_consented_since
_wipe_confirmed_since = agent._wipe_confirmed_since
_FORGET_MAX_ITEMS = agent._FORGET_MAX_ITEMS
_TooManyToForget = agent._TooManyToForget
_forget_topic = agent._forget_topic
_add_clarification = agent._add_clarification
_clear_clarification = agent._clear_clarification

__all__ = [
    "RtAgent",
    "_COMMON_WORDS",
    "_FORGET_MAX_ITEMS",
    "_FORGET_STOP",
    "_INTERNAL_CATS",
    "_TooManyToForget",
    "_WIPE_CONSENT_PHRASES",
    "_add_clarification",
    "_clear_clarification",
    "_contains_phone_number",
    "_email_spoken_by_caller",
    "_forget_topic",
    "_heard_in_caller_lines",
    "_internal_cats",
    "_lookup_tier",
    "_number_heard_from_caller",
    "_number_in_recent_search",
    "_perform_web_search",
    "_wipe_consent_line",
    "_wipe_consented_since",
    "_wipe_confirmed_since",
]
