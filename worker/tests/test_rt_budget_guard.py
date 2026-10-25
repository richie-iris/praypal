"""test_rt_budget_guard.py — Tests for in-call budget evaluation and soft-wrap benedictions."""
import pytest
from rt_budget_guard import (
    evaluate_call_budget,
    estimate_live_call_cogs,
    get_soft_wrap_closing_text,
)


def test_estimate_live_call_cogs():
    # 5-minute call (300 sec) with 80k in tokens and 60k out tokens
    cost = estimate_live_call_cogs(300, in_tokens=80000, out_tokens=60000)
    assert 0.90 <= cost <= 1.05


def test_evaluate_call_budget_normal_state():
    eval_res = evaluate_call_budget(tier="grace", elapsed_seconds=120, in_tokens=30000, out_tokens=20000)
    assert eval_res.status == "normal"
    assert eval_res.should_soft_wrap is False
    assert eval_res.should_hard_stop is False
    assert eval_res.closing_directive is None
    assert eval_res.remaining_seconds == 180.0


def test_evaluate_call_budget_soft_wrap_grace_tier():
    eval_res = evaluate_call_budget(
        tier="grace",
        elapsed_seconds=275,
        in_tokens=70000,
        out_tokens=50000,
        persona="jesus",
    )
    assert eval_res.status == "soft_wrap"
    assert eval_res.should_soft_wrap is True
    assert eval_res.should_hard_stop is False
    assert eval_res.remaining_seconds == 25.0
    assert "SOFT_WRAP_DIRECTIVE" in eval_res.closing_directive
    assert "Peace I leave with you" in eval_res.closing_directive
    assert "callpraypal.com" in eval_res.closing_directive


def test_evaluate_call_budget_hard_stop():
    eval_res = evaluate_call_budget(tier="grace", elapsed_seconds=301, persona="krishna")
    assert eval_res.status == "hard_stop"
    assert eval_res.should_hard_stop is True
    assert eval_res.remaining_seconds == 0.0
    assert "Maximum call duration reached" in eval_res.closing_directive


def test_devotion_tier_limits():
    # At 1000s, devotion is still normal (limit is 1200s, soft-wrap at 1080s)
    eval_normal = evaluate_call_budget(tier="devotion", elapsed_seconds=1000)
    assert eval_normal.status == "normal"

    # At 1100s, devotion soft wraps
    eval_wrap = evaluate_call_budget(tier="devotion", elapsed_seconds=1100, persona="mother")
    assert eval_wrap.status == "soft_wrap"
    assert "maternal shelter" in eval_wrap.closing_directive
