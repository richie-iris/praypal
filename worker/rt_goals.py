"""rt_goals.py — In-memory and persistent goal tracking for the assistant friend persona.

Allows the companion to act as an organized, caring assistant who tracks life goals,
milestones, habits, and commitments, following up across phone calls.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any
import rt_obs


@dataclass
class Goal:
    id: str
    title: str
    category: str = "personal"
    status: str = "active"  # "active", "completed", "paused"
    target_date: str = ""
    milestones: list[str] = field(default_factory=list)
    notes: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Goal:
        return cls(
            id=str(data.get("id") or f"g_{int(time.time()*1000)}"),
            title=str(data.get("title") or "").strip(),
            category=str(data.get("category") or "personal").strip(),
            status=str(data.get("status") or "active").strip().lower(),
            target_date=str(data.get("target_date") or "").strip(),
            milestones=[str(m) for m in data.get("milestones") or [] if m],
            notes=str(data.get("notes") or "").strip(),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )


def extract_goals_from_bundle(bundle: dict[str, Any]) -> list[Goal]:
    """Extract and parse all Goal objects from a caller's full bundle."""
    return extract_goals_from_schemas((bundle or {}).get("schemas") or [])


def extract_goals_from_schemas(schemas: list[dict[str, Any]]) -> list[Goal]:
    """Parse active goals from the caller's schemas bundle."""
    goals: list[Goal] = []
    for s in schemas or []:
        cat = (s.get("category") or "").lower()
        if cat in ("goals", "goal", "active_goals"):
            try:
                raw_summary = s.get("data_summary") or "{}"
                obj = json.loads(raw_summary) if isinstance(raw_summary, str) else raw_summary
                if isinstance(obj, dict):
                    for k, v in obj.items():
                        if isinstance(v, dict):
                            if "title" not in v:
                                v["title"] = k
                            goals.append(Goal.from_dict(v))
                        elif isinstance(v, str):
                            goals.append(Goal(id=k, title=v))
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict):
                            goals.append(Goal.from_dict(item))
            except Exception as _exc:
                rt_obs.obs.caught("rt_goals.extract_goals", _exc)
    return sorted(goals, key=lambda g: g.updated_at, reverse=True)


def format_goals_canvas(goals: list[Goal], max_items: int = 5) -> str:
    """Render goals into a clean, concise prompt block for the LLM."""
    active = [g for g in goals if g.status == "active"][:max_items]
    if not active:
        return ""

    lines = ["<ACTIVE_GOALS>"]
    for g in active:
        target_str = f" (target: {g.target_date})" if g.target_date else ""
        milestone_str = f" | Recent step: {g.milestones[-1]}" if g.milestones else ""
        notes_str = f" | Note: {g.notes}" if g.notes else ""
        lines.append(f"- [ID: {g.id}] {g.title}{target_str}{milestone_str}{notes_str}")
    lines.append(
        "Use manage_goals to celebrate progress, add milestone steps, or create new goals when they mention aspirations or commitments."
    )
    lines.append("</ACTIVE_GOALS>")
    return "\n".join(lines)
