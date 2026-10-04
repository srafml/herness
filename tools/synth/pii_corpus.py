"""Labelled redaction corpus for the impl 10 redaction tests (U11-25, design §3.1, §5.6).

`write_pii_corpus` writes JSONL, one object per line: `{"id": "pii-<nnnnnn>", "text",
"spans": [{"start", "end", "type"}], "keep": [{"start", "end", "kind": "ticket_number"}]}`.
Each text is the first 1-3 sentences of a templated incident description
(`render_incident_text`) with 0-3 PII spans (p = .10/.40/.30/.20) from `inject_pii`.

TH11-01: synthetic by construction. Every value comes from `tools.synth.pii` (reserved
ranges, `+1-202-555-01NN` phones, `synthetic`-prefixed credentials and tokens) and names
only from the invented name list of `build_name_list`; no real directory is read.

Spec note (T11-15): `inject_pii` draws span types uniformly, which puts a PERSON in about
19 % of records, below the >= 30 % rule. So a record with spans leads, with probability
`PERSON_LEAD`, with one PERSON span inserted here the way `inject_pii` inserts one (a
phrase at a sentence boundary, a name in one of the three formats) and takes its other
`k - 1` spans from `inject_pii`; the 0..3 span-count distribution is unchanged.
"""

import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import numpy as np

from tools.synth.catalog_pools import ADJECTIVES, NOUNS
from tools.synth.params import SynthUsageError
from tools.synth.pii import build_name_list, inject_pii
from tools.synth.rng import STREAM_PII_CORPUS, stream_rng
from tools.synth.text import TemplateBank, render_incident_text

N_MIN: Final = 2_000  # the impl 10 corpus size (R-56); impl 10 owns the threshold
N_MAX: Final = 1_000_000
SPAN_PROBS: Final = (0.10, 0.40, 0.30, 0.20)  # 0, 1, 2 or 3 spans
PERSON_LEAD: Final = 0.35

type Mark = dict[str, int | str]

_FIELD: Final = "text"
_IMPACT_LEVELS: Final = 4
_MAX_SENTENCES: Final = 3
_PERSON_PHRASE: Final = "Reported by "
_SENTENCE_SPLIT: Final = re.compile(r"(?<=[.!?])\s+")
_SENTENCE_END: Final = re.compile(r"[.!?](?=\s|$)")
_TICKET: Final = re.compile(r"(?:INC|CHG|PRB)\d{7}")


def _sentences(bank: TemplateBank, rng: np.random.Generator) -> str:
    """The first 1-3 sentences of a templated incident description."""
    component = f"{ADJECTIVES[int(rng.integers(len(ADJECTIVES)))]} "
    component += NOUNS[int(rng.integers(len(NOUNS)))]
    rendered = render_incident_text(
        bank,
        rng,
        family=None,
        slots=None,
        change_flavored=False,
        repeat_flavored=False,
        impact_level=int(rng.integers(_IMPACT_LEVELS)),
        component=component,
    )
    parts = _SENTENCE_SPLIT.split(rendered.description)
    return " ".join(parts[: int(rng.integers(1, _MAX_SENTENCES + 1))])


def _boundaries(text: str, spans: Sequence[Mark]) -> list[int]:
    """0, the end and every sentence end of `text` that lies outside every span."""
    points = {0, len(text)} | {m.end() for m in _SENTENCE_END.finditer(text)}
    inside = [(int(s["start"]), int(s["end"])) for s in spans]
    return sorted(p for p in points if not any(lo < p < hi for lo, hi in inside))


def _add_person(
    text: str, spans: list[Mark], rng: np.random.Generator, names: Sequence[tuple[str, str]]
) -> tuple[str, list[Mark]]:
    """Insert one PERSON value (First Last, Last, First or F. Last) at a sentence boundary."""
    first, last = names[int(rng.integers(len(names)))]
    value = (f"{first} {last}", f"{last}, {first}", f"{first[0]}. {last}")[int(rng.integers(3))]
    points = _boundaries(text, spans)
    at = points[int(rng.integers(len(points)))]
    lead = "" if at == 0 else " "
    piece = f"{lead}{_PERSON_PHRASE}{value}." + (" " if at == 0 else "")
    shift = len(piece)
    moved: list[Mark] = [
        {**s, "start": int(s["start"]) + shift, "end": int(s["end"]) + shift}
        if int(s["start"]) >= at
        else s
        for s in spans
    ]
    start = at + len(lead) + len(_PERSON_PHRASE)
    moved.append({"start": start, "end": start + len(value), "type": "PERSON"})
    return text[:at] + piece + text[at:], moved


def _record(
    index: int, bank: TemplateBank, rng: np.random.Generator, names: Sequence[tuple[str, str]]
) -> dict[str, object]:
    text = _sentences(bank, rng)
    n_spans = int(rng.choice(len(SPAN_PROBS), p=SPAN_PROBS))
    person = n_spans > 0 and rng.random() < PERSON_LEAD
    rest = n_spans - 1 if person else n_spans
    spans: list[Mark] = []
    if rest > 0:
        text, injected = inject_pii(text, _FIELD, rng, names, n_spans=rest)
        spans = [{"start": s.start, "end": s.end, "type": s.type} for s in injected]
    if person:
        text, spans = _add_person(text, spans, rng, names)
    keep = [
        {"start": m.start(), "end": m.end(), "kind": "ticket_number"}
        for m in _TICKET.finditer(text)
    ]
    ordered = sorted(spans, key=lambda s: int(s["start"]))
    return {"id": f"pii-{index:06d}", "text": text, "spans": ordered, "keep": keep}


def write_pii_corpus(seed: int, n: int, out: Path) -> int:
    """Write `n` corpus records to `out` (atomically: a temp file, then a replace) and
    return the number written. `n` outside 2,000..1,000,000 raises `SynthUsageError`."""
    if type(n) is not int or not N_MIN <= n <= N_MAX:
        msg = f"n must be an integer in {N_MIN}..{N_MAX}"
        raise SynthUsageError(msg, key="n")
    rng = stream_rng(seed, STREAM_PII_CORPUS)  # SynthUsageError for a negative seed
    bank, names = TemplateBank(), build_name_list(seed)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            for index in range(1, n + 1):
                record = _record(index, bank, rng, names)
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return n


__all__ = ["N_MAX", "N_MIN", "PERSON_LEAD", "SPAN_PROBS", "write_pii_corpus"]
