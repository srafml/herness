"""Label store, `label_check` sync and gold digest (U03-75 ... U03-77; design 03 §4.5, §4.6).

Layout (impl 03 §4.4): ``data/labels/<qsv>/{teacher,human,gold}/part-<ulid>.parquet``, raw gold
reviews in ``gold/_reviews/`` (delta DD-07), freeze markers in ``gold/_frozen/``, the sync
watermark in ``_sync.json``. Parts are written atomically through ``cache.replace_atomic``;
readers skip ``.``/``_``-prefixed entries. Rows carry ``record_id`` and hashes, never text;
gold is frozen per (qid, fingerprint) and never mixed into training (TH03-04).
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from herness.core import time as clock
from herness.core.audit import log_lock
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.ids import canonical_json, new_ulid, sha256_hex
from herness.core.logging import get_logger
from herness.core.types import Question, QuestionSet
from herness.enrich.cache import io_error, replace_atomic
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import question_fingerprint
from herness.store.ops import ReviewItem, list_review_items

type AppendKind = Literal["teacher", "human", "gold", "gold_reviews"]
type ItemKind = Literal["human", "gold_reviews"]

_KEY: Final = ("content_hash", "question", "question_fingerprint")
_S, _TS = pa.string(), pa.timestamp("us", tz="UTC")
_LABEL_FIELDS: Final = [(name, _S) for name in (*_KEY[:1], "record_id", *_KEY[1:], "answer")]
TEACHER_SCHEMA: Final = pa.schema([
    *_LABEL_FIELDS, ("distribution", pa.map_(_S, pa.float64())), ("decider", _S),
    ("decider_version", _S), ("round", pa.int16()), ("stratum", _S), ("purpose", _S),
])  # fmt: skip
HUMAN_SCHEMA: Final = pa.schema(
    [*_LABEL_FIELDS, ("labeled_by", _S), ("labeled_at", _TS), ("item_id", _S)]
)
GOLD_SCHEMA: Final = pa.schema([*HUMAN_SCHEMA, ("fold", pa.int16()), ("adjudicated", pa.bool_())])
LABEL_SCHEMAS: Final[dict[str, pa.Schema]] = {
    "teacher": TEACHER_SCHEMA, "human": HUMAN_SCHEMA, "gold": GOLD_SCHEMA,
    "gold_reviews": HUMAN_SCHEMA,
}  # fmt: skip
_QID_RE: Final = re.compile(r"^[a-z][a-z0-9_]{1,40}$")  # herness.core.types Question.id
_FINGERPRINT_RE: Final = re.compile(r"^[0-9a-f]{16}$")
_DIGEST_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_OPTION_KEY_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")  # Question option keys
_BOOL_LABELS: Final = frozenset({"true", "false"})
_SCORE_LABELS: Final = frozenset({"0", "1", "2", "3"})
_BOOL_WORDS: Final = frozenset({"true", "false", "yes", "no"})
_PURPOSE_KIND: Final[dict[str, ItemKind]] = {
    "spot_check": "human", "ensemble_disagreement": "human", "gold": "gold_reviews",
}  # fmt: skip
_PAYLOAD_TEXT: Final = ("content_hash", "record_id", "question", "question_fingerprint")
_PAGE: Final = 500
_LOCK_TIMEOUT_S: Final = 10.0
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)

_log = get_logger("enrich.labels")


@contextlib.contextmanager
def _os_errors(what: str) -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        raise io_error(exc, f"cannot access labels {what}") from exc


def _schema(kind: str) -> pa.Schema:
    if kind not in LABEL_SCHEMAS:
        msg = f"unknown label kind: {kind}"
        raise ConfigError(msg)
    return LABEL_SCHEMAS[kind]


def _write_json(target: Path, body: dict[str, object]) -> None:
    text = json.dumps(body, sort_keys=True)
    replace_atomic(target, lambda tmp: tmp.write_text(text, encoding="utf-8"), kind="label file")


def _read_json(path: Path) -> dict[str, object] | None:
    """The JSON object in ``path``; None when absent. ConfigError when unreadable."""
    with _os_errors(path.name):
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        msg = f"{path.name} is unreadable: not a JSON object"
        raise ConfigError(msg)
    return body


def _marker_name(qid: str, fingerprint: str) -> str:
    if _QID_RE.fullmatch(qid) is None or _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        msg = "invalid question id or fingerprint for a gold marker"
        raise ConfigError(msg)
    return f"{qid}-{fingerprint}.json"


def _pair_list(table: pa.Table) -> list[tuple[str, str]]:
    """(question, question_fingerprint) of every row, in row order."""
    questions = table.column("question").to_pylist()
    fingerprints = table.column("question_fingerprint").to_pylist()
    return list(zip(questions, fingerprints, strict=True))


class LabelStore:
    """Append-only label parts of one question set version (U03-75); single writer per process."""

    def __init__(self, paths: EnrichPaths, qsv: str) -> None:
        self.paths = paths
        self.qsv = qsv
        self.root = paths.labels_dir(qsv, "teacher").parent  # validates qsv (TH03-08)

    def _dir(self, kind: str) -> Path:
        _schema(kind)
        if kind == "gold_reviews":
            return self.paths.labels_dir(self.qsv, "gold") / "_reviews"
        return self.root / kind

    def _frozen_dir(self) -> Path:
        return self.paths.labels_dir(self.qsv, "gold") / "_frozen"

    def append(self, kind: AppendKind, rows: pa.Table) -> Path | None:
        """Write ``rows`` as one new part; None for an empty table.

        Raises ConfigError for an unknown kind or gold rows of a frozen (qid, fingerprint),
        SchemaViolation when the schema differs, StoreBusy or FatalError on OS errors.
        """
        schema = _schema(kind)
        bad = not rows.schema.equals(schema, check_metadata=False)
        if bad or any(rows.column(name).null_count for name in _KEY):  # keys sort and hash
            msg = f"label rows do not match the {kind} schema or have null keys"
            raise SchemaViolation(msg, question_set_version=self.qsv)
        if kind == "gold":
            for qid, fingerprint in sorted(set(_pair_list(rows))):
                if self.is_gold_frozen(qid, fingerprint):
                    msg = f"gold frozen for {qid}"
                    raise ConfigError(msg, question_set_version=self.qsv)
        return self._write(kind, rows)

    def _write(self, kind: str, rows: pa.Table) -> Path | None:
        if rows.num_rows == 0:
            return None
        target = self._dir(kind) / f"part-{new_ulid()}.parquet"
        data = rows.replace_schema_metadata(None)
        write = lambda tmp: pq.write_table(data, tmp, compression="zstd")  # noqa: E731
        replace_atomic(target, write, kind="label part")
        return target

    def read(self, kind: AppendKind, *, columns: list[str] | None = None) -> pa.Table:
        """All rows of ``kind`` (an empty table with its schema when none). Raises
        SchemaViolation for a part with a foreign schema, StoreBusy or FatalError."""
        schema = _schema(kind)
        if columns is not None:
            schema = pa.schema([schema.field(name) for name in columns])
        folder = self._dir(kind)
        with _os_errors(kind):
            if not folder.is_dir():
                return schema.empty_table()
            dataset = ds.dataset(folder, format="parquet", ignore_prefixes=[".", "_"])
            fragments = list(dataset.get_fragments())
            if not fragments:  # e.g. gold/ with only _reviews/ and _frozen/ (T03-32 fix)
                return schema.empty_table()
            for fragment in fragments:
                if not fragment.physical_schema.equals(_schema(kind), check_metadata=False):
                    msg = f"label part schema mismatch: {Path(fragment.path).name}"
                    raise SchemaViolation(msg, question_set_version=self.qsv)
            table = dataset.to_table(columns=schema.names)
        return table.cast(schema).replace_schema_metadata(None)

    def item_ids(self, kind: ItemKind) -> set[str]:
        """The ``item_id`` of every ``human`` or ``gold_reviews`` row."""
        return set(self.read(kind, columns=["item_id"]).column("item_id").to_pylist())

    def latest_human(self) -> pa.Table:
        """One human row per (content_hash, question, question_fingerprint): latest labeled_at."""
        ordered = self.read("human").sort_by(
            [("labeled_at", "descending"), ("item_id", "descending")]
        )
        first: dict[tuple[object, ...], int] = {}
        for index, key in enumerate(
            zip(*(ordered.column(n).to_pylist() for n in _KEY), strict=True)
        ):
            first.setdefault(key, index)
        return ordered.take(pa.array(sorted(first.values()), type=pa.int64()))

    def gold_hashes(self) -> set[str]:
        """Every gold ``content_hash`` (excluded from samples and training, TH03-04)."""
        table = self.read("gold", columns=["content_hash"])
        return set(table.column("content_hash").to_pylist())

    def is_gold_frozen(self, qid: str, fingerprint: str) -> bool:
        marker = self._frozen_dir() / _marker_name(qid, fingerprint)
        with _os_errors(marker.name):
            return marker.is_file()

    def freeze_gold(self, qid: str, fingerprint: str, digest: str, n: int) -> None:
        """Write ``gold/_frozen/<qid>-<fingerprint>.json`` ``{digest, n, frozen_at}``; the same
        freeze again is a no-op, another one, a bad digest or n < 0 raise ConfigError."""
        if _DIGEST_RE.fullmatch(digest) is None or type(n) is not int or n < 0:
            msg = "freeze_gold needs a 64-hex digest and n >= 0"
            raise ConfigError(msg)
        marker = self._frozen_dir() / _marker_name(qid, fingerprint)
        existing = _read_json(marker)
        if existing is not None:
            if existing.get("digest") == digest and existing.get("n") == n:
                return
            msg = f"gold frozen for {qid}"
            raise ConfigError(msg, question_set_version=self.qsv)
        _write_json(marker, {"digest": digest, "n": n, "frozen_at": clock.now().isoformat()})

    def migrate_from(self, old_qsv: str, new_qs: QuestionSet) -> int:
        """Copy rows and freeze markers of unchanged questions from ``old_qsv`` (returns rows).

        Idempotent through ``_migrated_from_<old_qsv>.json`` (written last); gold markers are
        copied only with gold rows. Raises ConfigError for equal or mismatched versions or
        an unreadable marker; read and write errors as ``read`` and ``append``.
        """
        if old_qsv == self.qsv or new_qs.version != self.qsv:
            msg = "migrate_from needs another old version and this version's question set"
            raise ConfigError(msg, question_set_version=self.qsv)
        old = LabelStore(self.paths, old_qsv)
        marker = self.root / f"_migrated_from_{old_qsv}.json"
        done = _read_json(marker)
        if done is not None:
            rows = done.get("rows")
            if type(rows) is not int or rows < 0:
                msg = f"{marker.name} is unreadable: no row count"
                raise ConfigError(msg)
            return rows
        wanted = {(q.id, q.fingerprint or question_fingerprint(q)) for q in new_qs.questions}
        copied = 0
        gold_pairs: set[tuple[str, str]] = set()
        for kind in LABEL_SCHEMAS:
            table = old.read(kind)  # type: ignore[arg-type]
            keep = [pair in wanted for pair in _pair_list(table)]
            kept = table.filter(pa.array(keep, type=pa.bool_()))
            self._write(kind, kept)
            copied += kept.num_rows
            if kind == "gold":
                gold_pairs = set(_pair_list(kept))
        for qid, fingerprint in sorted(gold_pairs):
            body = _read_json(old._frozen_dir() / _marker_name(qid, fingerprint))
            if body is not None:
                _write_json(self._frozen_dir() / _marker_name(qid, fingerprint), body)
        _write_json(marker, {"rows": copied, "finished_at": clock.now().isoformat()})
        _log.info("enrich.labels.migrated", old=old_qsv, new=self.qsv, rows=copied)
        return copied


def gold_digest(gold: pa.Table) -> str:
    """``gold_sha256`` of ``eval.json`` (U03-77): SHA-256 hex over the canonical JSON lines of
    ``[question, question_fingerprint, content_hash, answer, fold]`` sorted by (question,
    content_hash); ties sort by the whole line, so the digest ignores part layout and row order.
    """
    names = ("question", "question_fingerprint", "content_hash", "answer", "fold")
    rows = zip(*(gold.column(name).to_pylist() for name in names), strict=True)
    lines = sorted((row[0], row[2], canonical_json(list(row))) for row in rows)
    return sha256_hex("\n".join(line for _, _, line in lines))


def _valid_label(question: Question, answer: str) -> bool:
    """Whether ``answer`` is a label of ``question`` (U03-50 label sets)."""
    if question.type == "bool":
        return answer in _BOOL_LABELS
    if question.type == "score":
        return answer in _SCORE_LABELS
    if question.options is not None:
        return answer in question.options
    # dynamic options (core.team / core.service) resolve at run time: any valid option key
    return _OPTION_KEY_RE.fullmatch(answer) is not None and answer.lower() not in _BOOL_WORDS


def _answer(item: ReviewItem) -> object:
    """``note.answer`` when the note is JSON with a string ``answer``, else ``payload.answer``."""
    try:
        note = json.loads(item.note) if item.note is not None else None
    except ValueError:
        note = None
    if isinstance(note, dict) and isinstance(note.get("answer"), str):
        return note["answer"]
    return item.payload.get("answer")


def _label_row(item: ReviewItem, qs: QuestionSet) -> tuple[ItemKind, dict[str, object]] | None:
    """The target kind and human-schema row of an approved item; None to skip it."""
    kind = _PURPOSE_KIND.get(str(item.payload.get("purpose")))  # unknown purpose: invalid
    answer = _answer(item)
    fields = {name: item.payload.get(name) for name in _PAYLOAD_TEXT}
    question = next((q for q in qs.questions if q.id == fields["question"]), None)
    valid = all(isinstance(v, str) for v in fields.values()) and isinstance(answer, str)
    if kind is None or question is None or not valid or not _valid_label(question, str(answer)):
        _log.warning("enrich.labels.invalid_answer", item_id=item.item_id)
        return None
    row = {**fields, "answer": answer, "labeled_by": item.decided_by}
    return kind, {**row, "labeled_at": item.decided_at, "item_id": item.item_id}


def _watermark(path: Path) -> tuple[datetime, str]:
    body = _read_json(path)
    if body is None:
        return _EPOCH, ""
    stamp, item_id = body.get("last_decided_at"), body.get("last_item_id")
    try:
        decided = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
    except ValueError:
        decided = None
    if decided is None or decided.tzinfo is None or not isinstance(item_id, str):
        msg = f"{path.name} is unreadable: bad watermark"
        raise ConfigError(msg)
    return decided, item_id


def _decided_items(cursor: tuple[datetime, str]) -> Iterator[ReviewItem]:
    """Decided ``label_check`` items after ``cursor`` by (decided_at, item_id), 500 per page."""
    while True:
        page = list_review_items(
            kind="label_check", statuses=("approved", "rejected"), decided_after=cursor, limit=_PAGE
        )
        yield from page
        if len(page) < _PAGE:
            return
        cursor = (page[-1].decided_at or _EPOCH, page[-1].item_id)


def sync_label_checks(store: LabelStore, *, qs: QuestionSet) -> dict[str, int]:
    """Move decided ``label_check`` items of ``qs.version`` into ``human/`` or ``gold/_reviews/``
    (U03-76); returns counts ``human``, ``gold_reviews`` (rows appended) and ``skipped``.

    Idempotent (watermark ``_sync.json`` and ``item_id`` dedupe) under ``data/locks/labels.lock``.
    Raises ConfigError for a question set of another version or a bad watermark; StoreBusy
    (lock held 10 s, ops read) and write errors propagate.
    """
    if qs.version != store.qsv:
        msg = "sync_label_checks needs the store's question set"
        raise ConfigError(msg, question_set_version=store.qsv)
    lock = store.paths.data_root / "locks" / "labels.lock"
    with _os_errors("lock"):
        lock.parent.mkdir(parents=True, exist_ok=True)
    with log_lock(lock, timeout_s=_LOCK_TIMEOUT_S):
        mark = store.root / "_sync.json"
        cursor = _watermark(mark)
        rows: dict[ItemKind, list[dict[str, object]]] = {"human": [], "gold_reviews": []}
        counts = {"human": 0, "gold_reviews": 0, "skipped": 0}
        last: ReviewItem | None = None
        for item in _decided_items(cursor):
            last = item
            if item.payload.get("question_set_version") != qs.version:
                continue
            label = _label_row(item, qs) if item.status == "approved" else None
            if label is None:
                counts["skipped"] += 1
            else:
                rows[label[0]].append(label[1])
        for kind, new_rows in rows.items():
            known = store.item_ids(kind)
            fresh = [row for row in new_rows if row["item_id"] not in known]
            store.append(kind, pa.Table.from_pylist(fresh, schema=HUMAN_SCHEMA))
            counts[kind] = len(fresh)
        if last is not None and last.decided_at is not None:
            stamp = last.decided_at.isoformat()
            _write_json(mark, {"last_decided_at": stamp, "last_item_id": last.item_id})
    _log.info("enrich.labels.synced", **counts)
    return counts
