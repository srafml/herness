"""Tests for tools.synth.text (U11-05): UT11-06 and UT11-07."""

import dataclasses
from collections import Counter

import numpy as np
import pytest

from tools.synth.param_groups import TextParams
from tools.synth.params import SynthUsageError
from tools.synth.text import (
    CHANGE_MARKERS,
    REPEAT_MARKERS,
    ROOT_CAUSE_OPTIONS,
    RenderedText,
    TemplateBank,
    render_change_text,
    render_incident_text,
    render_jira_text,
)

pytestmark = pytest.mark.unit

_SHORT_CAP = 160
_DESCRIPTION_CAP = 3000


@pytest.fixture(scope="module")
def bank() -> TemplateBank:
    return TemplateBank()


def _incident(bank: TemplateBank, rng: np.random.Generator, **kwargs: object) -> RenderedText:
    args: dict[str, object] = {
        "family": None,
        "slots": None,
        "change_flavored": False,
        "repeat_flavored": False,
        "impact_level": 1,
        "component": "Payments API",
    }
    args.update(kwargs)
    return render_incident_text(bank, rng, **args)  # type: ignore[arg-type]


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in markers)


def test_ut11_06_root_causes_change_patterns_and_caps(bank: TemplateBank) -> None:
    """UT11-06 1,000 renders: root causes in the options, change patterns, length caps."""
    assert ROOT_CAUSE_OPTIONS == (
        "software_defect",
        "config_change",
        "capacity",
        "infrastructure",
        "dependency",
        "data_issue",
        "access_identity",
        "user_error",
        "unknown",
    )
    rng = np.random.default_rng(1106)
    for i in range(1000):
        flavored = i % 2 == 0
        text = _incident(bank, rng, change_flavored=flavored, impact_level=i % 4)
        assert text.root_cause in ROOT_CAUSE_OPTIONS
        assert _has_marker(text.description, CHANGE_MARKERS) is flavored
        assert len(text.short_description) <= _SHORT_CAP
        assert len(text.description) <= _DESCRIPTION_CAP
        assert text.language in ("en", "es")
        assert text.close_notes


def test_ut11_06_repeat_phrase_follows_flag(bank: TemplateBank) -> None:
    """UT11-06 repeat-flavored texts say "again" or "recurring"; others never do."""
    rng = np.random.default_rng(7)
    for i in range(400):
        repeat = i % 3 == 0
        text = _incident(bank, rng, repeat_flavored=repeat)
        assert _has_marker(text.description, REPEAT_MARKERS) is repeat


def test_ut11_06_family_fixes_root_cause_and_long_component_is_capped(bank: TemplateBank) -> None:
    """UT11-06 named families keep their root cause; caps hold for a long component."""
    rng = np.random.default_rng(3)
    long_component = "Very Long Component Name " * 20
    for _ in range(50):
        pool = _incident(bank, rng, family="tpl_conn_pool", component=long_component)
        cert = _incident(bank, rng, family="tpl_cert_expiry", impact_level=3)
        assert (pool.family, pool.root_cause) == ("tpl_conn_pool", "capacity")
        assert (cert.family, cert.root_cause) == ("tpl_cert_expiry", "access_identity")
        assert len(pool.short_description) <= _SHORT_CAP
        assert len(pool.description) <= _DESCRIPTION_CAP


def test_ut11_06_bank_vocabulary_sizes(bank: TemplateBank) -> None:
    """UT11-06 bank sizes: 60 symptoms, 25 causes, 40 actions, 30 families x 3-6 patterns."""
    assert len(bank.symptoms) == len(set(bank.symptoms)) == 60
    assert len(bank.actions) == len(set(bank.actions)) == 40
    assert len(bank.root_causes) == 25
    assert {option for _, option in bank.root_causes} == set(ROOT_CAUSE_OPTIONS)
    assert len(bank.families) == 30
    for family in bank.families.values():
        assert 3 <= len(family.patterns) <= 6
        assert family.change_patterns
        assert family.root_cause in ROOT_CAUSE_OPTIONS
    with pytest.raises(dataclasses.FrozenInstanceError):
        bank.symptoms = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        bank.families["x"] = bank.families["tpl_conn_pool"]  # type: ignore[index]


def test_ut11_06_cluster_slots_are_mostly_reused(bank: TemplateBank) -> None:
    """UT11-06 given slots are reused with each slot re-drawn at about 20 %."""
    rng = np.random.default_rng(11)
    base = _incident(bank, rng, family="tpl_conn_pool")
    assert set(base.slots) == {"symptom", "action", "host", "count"}
    kept = Counter[str]()
    renders = 2000
    for _ in range(renders):
        text = _incident(bank, rng, family="tpl_conn_pool", slots=base.slots)
        assert set(text.slots) == set(base.slots)
        kept.update(key for key, value in text.slots.items() if value == base.slots[key])
    for key in base.slots:
        assert 0.75 <= kept[key] / renders <= 0.88


def test_ut11_06_partial_slots_are_filled_and_unknown_family_fails(bank: TemplateBank) -> None:
    """UT11-06 missing slots are drawn; unknown family or impact level is a usage error."""
    rng = np.random.default_rng(5)
    text = _incident(bank, rng, slots={"host": "db01"}, family="tpl_dns")
    assert set(text.slots) == {"symptom", "action", "host", "count"}
    with pytest.raises(SynthUsageError):
        _incident(bank, rng, family="tpl_nope")
    with pytest.raises(SynthUsageError):
        _incident(bank, rng, impact_level=4)


def test_ut11_06_same_seed_same_text(bank: TemplateBank) -> None:
    """UT11-06 rendering is deterministic for a seeded generator."""
    first = [_incident(bank, np.random.default_rng(9)) for _ in range(3)]
    second = [_incident(bank, np.random.default_rng(9)) for _ in range(3)]
    assert first == second


def test_ut11_06_noise_rates_apply(bank: TemplateBank) -> None:
    """UT11-06 casing noise hits short descriptions; rates come from TextParams."""
    rng = np.random.default_rng(21)
    noisy = TextParams(typo_rate=1.0, casing_noise_rate=1.0, spanish_share=1.0)
    for _ in range(50):
        text = _incident(bank, rng, text=noisy, change_flavored=True, repeat_flavored=True)
        assert text.language == "es"
        short = text.short_description
        assert short in (short.upper(), short.lower())
        assert _has_marker(text.description, CHANGE_MARKERS)
        assert _has_marker(text.description, REPEAT_MARKERS)
    quiet = TextParams(typo_rate=0.0, casing_noise_rate=0.0, spanish_share=0.0)
    text = _incident(bank, rng, text=quiet, family="tpl_conn_pool")
    assert "connection pool" in text.description


def test_ut11_07_families_and_spanish_share(bank: TemplateBank) -> None:
    """UT11-07 10,000 renders: families include the plant families; Spanish share 0.05."""
    assert {"tpl_conn_pool", "tpl_cert_expiry"} <= set(bank.families)
    assert bank.families["tpl_conn_pool"].root_cause == "capacity"
    assert bank.families["tpl_cert_expiry"].root_cause == "access_identity"
    rng = np.random.default_rng(1107)
    renders = [_incident(bank, rng, impact_level=int(rng.integers(0, 4))) for _ in range(10_000)]
    families = {text.family for text in renders}
    assert {"tpl_conn_pool", "tpl_cert_expiry"} <= families
    assert families == set(bank.families)
    spanish = sum(text.language == "es" for text in renders) / len(renders)
    assert abs(spanish - 0.05) <= 0.01


def test_ut11_07_change_and_jira_texts(bank: TemplateBank) -> None:
    """UT11-07 change and Jira renders stay within caps and the root cause options."""
    rng = np.random.default_rng(17)
    for emergency in (True, False):
        text = render_change_text(bank, rng, component="Billing", emergency=emergency)
        assert "Billing" in text.short_description
        assert text.root_cause in ROOT_CAUSE_OPTIONS
        assert len(text.short_description) <= _SHORT_CAP
        assert ("Emergency" in text.description) is emergency
    for issue_type in ("initiative", "epic", "feature", "story", "bug", "task"):
        text = render_jira_text(bank, rng, issue_type=issue_type, component="Search", theme=None)
        assert text.family in bank.families
        assert len(text.short_description) <= _SHORT_CAP
    epic = render_jira_text(bank, rng, issue_type="epic", component="Pay", theme="tpl_conn_pool")
    assert "connection pool" in epic.short_description.lower()
    assert epic.root_cause == "capacity"
    with pytest.raises(SynthUsageError):
        render_jira_text(bank, rng, issue_type="saga", component="Pay", theme=None)
    with pytest.raises(SynthUsageError):
        render_jira_text(bank, rng, issue_type="epic", component="Pay", theme="tpl_nope")
