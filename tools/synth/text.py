"""Template-only free text with slot-derived truth labels (U11-05, design §5.1.4).

No LLM and no real data (TH11-01): every word comes from the built-in vocabularies
in `tools.synth.text_vocab`.
Truth labels follow the slots: `root_cause` is the family's cause, a change-flavored text
contains one of `CHANGE_MARKERS`, a repeat-flavored text one of `REPEAT_MARKERS`.
"""

import dataclasses
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

import numpy as np

from tools.synth.param_groups import TextParams
from tools.synth.params import SynthUsageError
from tools.synth.text_vocab import (
    ACTIONS,
    CAUSES,
    CHANGE_EN,
    CHANGE_ES,
    CHANGE_MARKERS,
    CHANGE_VERBS,
    FAMILY_ROWS,
    HOSTS,
    IMPACT,
    JIRA_SUMMARY,
    OPTION_ES,
    PATTERNS_EN,
    PATTERNS_ES,
    REPEAT,
    REPEAT_MARKERS,
    ROOT_CAUSE_OPTIONS,
    SYMPTOMS,
)

SLOT_KEYS: Final = ("symptom", "action", "host", "count")

_SHORT_CAP: Final = 160
_DESCRIPTION_CAP: Final = 3000
_DEFAULT_TEXT: Final = TextParams()
_MIN_TYPO_WORD: Final = 4


@dataclasses.dataclass(frozen=True, slots=True)
class Family:
    """One template family: its cause and its English and Spanish patterns."""

    name: str
    cause: str
    root_cause: str
    topic: str
    topic_es: str
    patterns: tuple[str, ...]
    change_patterns: tuple[str, ...]
    patterns_es: tuple[str, ...]
    change_patterns_es: tuple[str, ...]


def _families() -> Mapping[str, Family]:
    out: dict[str, Family] = {}
    for index, (name, topic, topic_es, cause_index) in enumerate(FAMILY_ROWS):
        cause, option = CAUSES[cause_index]
        count = 3 + index % 4  # 3..6 patterns per family
        chosen = [PATTERNS_EN[(index + k) % len(PATTERNS_EN)] for k in range(count)]
        out[name] = Family(
            name=name,
            cause=cause,
            root_cause=option,
            topic=topic,
            topic_es=topic_es,
            patterns=tuple(p.replace("{topic}", topic) for p in chosen),
            change_patterns=tuple(p.replace("{topic}", topic) for p in CHANGE_EN),
            patterns_es=tuple(p.replace("{topic}", topic_es) for p in PATTERNS_ES),
            change_patterns_es=tuple(p.replace("{topic}", topic_es) for p in CHANGE_ES),
        )
    return MappingProxyType(out)


def _protected_words() -> frozenset[str]:
    phrases = [*CHANGE_MARKERS, *REPEAT_MARKERS, *(p for v in IMPACT.values() for p in v)]
    return frozenset(word.strip(".,:;").lower() for phrase in phrases for word in phrase.split())


@dataclasses.dataclass(frozen=True, slots=True)
class TemplateBank:
    """The built-in vocabularies and template families; immutable after construction."""

    symptoms: tuple[str, ...] = dataclasses.field(default=SYMPTOMS, init=False)
    actions: tuple[str, ...] = dataclasses.field(default=ACTIONS, init=False)
    root_causes: tuple[tuple[str, str], ...] = dataclasses.field(default=CAUSES, init=False)
    families: Mapping[str, Family] = dataclasses.field(default_factory=_families, init=False)
    protected: frozenset[str] = dataclasses.field(default_factory=_protected_words, init=False)

    def family(self, name: str) -> Family:
        """Return the family `name`, else raise `SynthUsageError`."""
        try:
            return self.families[name]
        except KeyError:
            msg = "unknown template family"
            raise SynthUsageError(msg, key="family") from None


@dataclasses.dataclass(frozen=True, slots=True)
class RenderedText:
    """Rendered free text with the slot-derived labels."""

    short_description: str
    description: str
    close_notes: str
    family: str
    root_cause: str
    slots: dict[str, str]
    language: Literal["en", "es"]


def _pick[T](rng: np.random.Generator, items: tuple[T, ...]) -> T:
    return items[int(rng.integers(len(items)))]


def _draw_slot(bank: TemplateBank, rng: np.random.Generator, key: str) -> str:
    if key == "symptom":
        return _pick(rng, bank.symptoms)
    if key == "action":
        return _pick(rng, bank.actions)
    if key == "host":
        return f"{_pick(rng, HOSTS)}{int(rng.integers(1, 100)):02d}"
    return str(int(rng.integers(2, 500)))


def _slots(
    bank: TemplateBank, rng: np.random.Generator, given: Mapping[str, str] | None, variation: float
) -> dict[str, str]:
    out: dict[str, str] = {}
    for key in SLOT_KEYS:
        if given is not None and key in given and rng.random() >= variation:
            out[key] = given[key]
        else:
            out[key] = _draw_slot(bank, rng, key)
    return out


def _fill(pattern: str, slots: Mapping[str, str], component: str, topic: str = "") -> str:
    return pattern.format(
        component=component,
        topic=topic,
        Symptom=slots["symptom"][:1].upper() + slots["symptom"][1:],
        Action=slots["action"][:1].upper() + slots["action"][1:],
        **slots,
    )


def _typos(text: str, rng: np.random.Generator, rate: float, protected: frozenset[str]) -> str:
    """Swap two adjacent letters in a word with probability `rate`, sparing marker words."""
    words = text.split(" ")
    draws = rng.random(len(words))
    for i, word in enumerate(words):
        if draws[i] < rate and len(word) >= _MIN_TYPO_WORD:
            if word.strip(".,:;()").lower() in protected:
                continue
            at = int(rng.integers(len(word) - 1))
            words[i] = word[:at] + word[at + 1] + word[at] + word[at + 2 :]
    return " ".join(words)


def _check_impact(impact_level: int) -> None:
    if not 0 <= impact_level <= len(IMPACT["en"]) - 1:
        msg = "impact_level must be in 0..3"
        raise SynthUsageError(msg, key="impact_level")


def render_incident_text(  # noqa: PLR0913 - signature fixed by U11-05
    bank: TemplateBank,
    rng: np.random.Generator,
    *,
    family: str | None,
    slots: Mapping[str, str] | None,
    change_flavored: bool,
    repeat_flavored: bool,
    impact_level: int,
    component: str,
    text: TextParams = _DEFAULT_TEXT,
) -> RenderedText:
    """Render incident text; see U11-05 for the algorithm and `text` for the noise rates."""
    _check_impact(impact_level)
    fam = (
        bank.family(family)
        if family is not None
        else bank.families[_pick(rng, tuple(bank.families))]
    )
    filled = _slots(bank, rng, slots, text.slot_variation)
    language: Literal["en", "es"] = "es" if rng.random() < text.spanish_share else "en"
    if language == "es":
        pool = fam.change_patterns_es if change_flavored else fam.patterns_es
    else:
        pool = fam.change_patterns if change_flavored else fam.patterns
    parts = [_fill(_pick(rng, pool), filled, component)]
    if repeat_flavored:
        parts.append(_pick(rng, REPEAT[language]))
    parts.append(IMPACT[language][impact_level])
    if language == "es":
        short = f"Incidencia en {component}: {fam.topic_es}"
        notes = f"Causa raíz: {OPTION_ES[fam.root_cause]}. Resolución: se aplicó la corrección."
    else:
        short = f"{_fill('{Symptom}', filled, component)} in {component}"
        notes = f"Root cause: {fam.cause}. Resolution: {filled['action']}."
    description = _typos(" ".join(parts), rng, text.typo_rate, bank.protected)
    notes = _typos(notes, rng, text.typo_rate, bank.protected)
    if rng.random() < text.casing_noise_rate:
        short = short.upper() if rng.random() < 0.5 else short.lower()  # noqa: PLR2004 - even odds
    return RenderedText(
        short_description=short[:_SHORT_CAP],
        description=description[:_DESCRIPTION_CAP],
        close_notes=notes[:_DESCRIPTION_CAP],
        family=fam.name,
        root_cause=fam.root_cause,
        slots=filled,
        language=language,
    )


def render_change_text(
    bank: TemplateBank, rng: np.random.Generator, *, component: str, emergency: bool
) -> RenderedText:
    """Render change text; an emergency change fixes a drawn family's cause."""
    filled = _slots(bank, rng, None, 1.0)
    verb = _pick(rng, CHANGE_VERBS)
    if emergency:
        fam = bank.families[_pick(rng, tuple(bank.families))]
        name, cause = fam.name, fam.root_cause
        purpose = f"Emergency change to stop {fam.topic}: {filled['action']}."
    else:
        name, cause = "chg_planned", "unknown"
        purpose = f"Planned change: {filled['action']} on {filled['host']}."
    description = f"{purpose} Validation: smoke tests and monitoring. Rollback: previous version."
    return RenderedText(
        short_description=f"{verb} {component}"[:_SHORT_CAP],
        description=description[:_DESCRIPTION_CAP],
        close_notes="Change implemented as planned.",
        family=name,
        root_cause=cause,
        slots=filled,
        language="en",
    )


def render_jira_text(
    bank: TemplateBank,
    rng: np.random.Generator,
    *,
    issue_type: str,
    component: str,
    theme: str | None,
) -> RenderedText:
    """Render a Jira summary and description; `theme` names a family (for example a plant's)."""
    if issue_type not in JIRA_SUMMARY:
        msg = "unknown Jira issue type"
        raise SynthUsageError(msg, key="issue_type")
    fam = (
        bank.family(theme) if theme is not None else bank.families[_pick(rng, tuple(bank.families))]
    )
    filled = _slots(bank, rng, None, 1.0)
    summary = _fill(JIRA_SUMMARY[issue_type], filled, component, fam.topic)
    description = (
        f"Context: {component} has seen {fam.topic}; users report {filled['symptom']}. "
        f"Goal: {filled['action']}. Cause area: {fam.cause}."
    )
    return RenderedText(
        short_description=summary[:_SHORT_CAP],
        description=description[:_DESCRIPTION_CAP],
        close_notes="",
        family=fam.name,
        root_cause=fam.root_cause,
        slots=filled,
        language="en",
    )


__all__ = [
    "CHANGE_MARKERS",
    "REPEAT_MARKERS",
    "ROOT_CAUSE_OPTIONS",
    "SLOT_KEYS",
    "Family",
    "RenderedText",
    "TemplateBank",
    "render_change_text",
    "render_incident_text",
    "render_jira_text",
]
