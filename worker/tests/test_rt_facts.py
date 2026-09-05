"""test_rt_facts.py — Unit tests for fact extraction & normalization."""
from __future__ import annotations

from rt_facts import _slug, _norm_key, _render, derive_facts


def test_slug_ascii_and_spaces():
    assert _slug("John Doe") == "john-doe"
    assert _slug("  Mary   Jane  ") == "mary-jane"
    assert _slug("MARY") == "mary"


def test_slug_unicode_preservation():
    # Non-Latin characters should normalize predictably without collapsing to 'unknown'
    assert _slug("José") == "jose"
    assert _slug("Мария") == "мария"
    assert _slug("北京") == "北京"


def test_slug_empty_and_symbols():
    # Blank inputs
    assert _slug("") == "unknown"
    assert _slug(None) == "unknown"
    # Emoji/symbol only inputs fall back to hash
    symbol_slug = _slug("🎉🎉")
    assert symbol_slug.startswith("x")
    assert len(symbol_slug) > 2


def test_norm_key():
    assert _norm_key("family", "Uncle Bob") == "family:uncle-bob"
    assert _norm_key("pets", "Fluffy") == "pets:fluffy"


def test_render_values():
    assert _render("Simple String") == "Simple String"
    assert _render(123) == "123"
    assert _render({"relation": "sister", "notes": "lives in NY"}) == 'notes: lives in NY; relation: sister'


def test_derive_facts():
    extraction = {
        "family": {"Mary": {"relationship": "daughter"}},
        "pets": {"Rex": {"species": "dog"}},
        "hobbies": ["gardening"],
        "wellbeing": "feeling energized",
    }
    facts = derive_facts(extraction)
    assert len(facts) >= 3
    keys = [f["p_norm_key"] for f in facts]
    assert "family:mary" in keys
    assert "pet:rex" in keys
