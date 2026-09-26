"""Golden suite format, loader and resolution (design 11 §4.5, §5.3.1; U11-53 … U11-55).

Hostile YAML (TH11-08): 1 MB cap, 500 questions, safe-load with an alias budget. Reference
SQL (TH11-09): spec 05 `SqlGuard`, per-thread cursor, 60 s interrupt, 1,000-row fetch cap.
`SuiteError` lives in `herness.eval.truth` (golden imports truth, so not the reverse).
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final, Literal, NoReturn, Self

import duckdb
import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from herness.core.errors import ConfigError, QueryError
from herness.core.ids import query_id
from herness.eval.scripted import _parse as _parse_yaml
from herness.eval.truth import SuiteError, TruthManifest, plant_value
from herness.harness.llm.settings import SqlSettings
from herness.harness.sql_guard import ALLOWED_SCHEMAS, SqlGuard
from herness.metrics.settings import Unit

__all__ = [
    "ClaimRule", "EntitiesExpected", "EvalQuestion", "Expected", "MentionRule", "NumericExpected",
    "ReferenceResult", "ResolvedQuestion", "RubricExpected", "RulesExpected", "SignRule", "Suite",
    "SuiteDefaults", "SuiteError", "Tolerance", "load_suite", "resolve",
]  # fmt: skip

MAX_SUITE_BYTES: Final = 1_000_000
MAX_QUESTIONS: Final = 500
QUERY_TIMEOUT_S: Final = 60.0  # read at call time; tests monkeypatch it
MAX_REFERENCE_ROWS: Final = 1_000

type Dataset = Literal["synthetic", "real"]
type _Row = tuple[object, ...]
type _Block = tuple[str, str, str | None]  # (name, reference_sql, truth_ref)
type _Refs = dict[str, ReferenceResult]
type Mention = str | MentionRule
_Sql = Annotated[str, Field(min_length=1, max_length=8000)]
_CHECK_RE: Final = r"^(rank1|set_equals|topk_contains:[0-9]+:[0-9]+|kendall_tau>=0?\.[0-9]+)$"
_PLACEHOLDER_RE: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\}")
_TRUTH_NAME_RE: Final = re.compile(r"^(T[0-9]c?|plants)\.")
_ALIASES: Final = {"service": "service_id", "team": "team_id", "ci": "ci_id",
                   "decoy": "decoy_epic_key", "owning_team": "owning_team_id"}  # fmt: skip
_DISPLAY_ATTRS: Final = frozenset({"team", "service", "ci", "org", "owning_team"})
_DISPLAY_SQL: Final = (  # design §4.5 lookup order; fixed, parameterised, trusted SQL
    "SELECT name FROM core.team WHERE team_id = ?",
    "SELECT name FROM core.service WHERE service_id = ?",
    "SELECT name FROM core.org WHERE org_id = ?",
    "SELECT key FROM core.work_item WHERE ? IN (record_id, key)",
)
_SCHEMA_SQL: Final = (
    "SELECT lower(table_schema), lower(table_name), lower(column_name), lower(data_type)"
    " FROM information_schema.columns WHERE list_contains(?, lower(table_schema))"
)
_BLOCKS: Final = ("placeholders_sql", "entities", "numeric")  # `{name}` lookup order


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Tolerance(_Model):  # exactly one of abs or rel; `exact` is abs: 0
    abs: float | None = Field(default=None, ge=0)
    rel: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.abs is None) == (self.rel is None):
            msg = "tolerance needs exactly one of abs or rel"
            raise ValueError(msg)
        return self


class MentionRule(_Model):
    entity: str = Field(min_length=1)
    with_any: list[str] = Field(min_length=1)


def _compiles(pattern: str) -> str:
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        msg = f"pattern is not a valid regex: {exc.msg}"
        raise ValueError(msg) from exc
    return pattern


class ClaimRule(_Model):  # pattern compiled with re.IGNORECASE at load
    entity: str = Field(min_length=1)
    pattern: Annotated[str, Field(min_length=1, max_length=1000), AfterValidator(_compiles)]

    @property
    def regex(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE)  # served from the re module cache


class SignRule(_Model):
    column: str = Field(min_length=1)
    sign: Literal["positive", "negative"]


class NumericExpected(_Model):
    reference_sql: _Sql
    unit: Unit
    tolerance: Tolerance | None = None
    truth_ref: str | None = None


class EntitiesExpected(_Model):
    reference_sql: _Sql
    check: str = Field(pattern=_CHECK_RE)
    truth_ref: str | None = None


class RulesExpected(_Model):
    must_mention: list[Mention] = []
    must_not_claim: list[ClaimRule] = []
    max_number_refs: int | None = Field(default=None, ge=0)  # DD11-08
    number_signs: list[SignRule] = []  # DD11-08


class RubricExpected(_Model):
    criteria: list[str] = Field(min_length=1, max_length=10)
    min_score: float = Field(ge=1, le=5)


class Expected(_Model):  # top-level must_mention/must_not_claim merge into rules
    numeric: NumericExpected | None = None
    entities: EntitiesExpected | None = None
    rules: RulesExpected | None = None
    rubric: RubricExpected | None = None
    must_mention: list[Mention] = []
    must_not_claim: list[ClaimRule] = []
    placeholders_sql: _Sql | None = None  # DD11-08

    @model_validator(mode="after")
    def _merge_rules(self) -> Self:
        loose = bool(self.must_mention or self.must_not_claim)
        if not loose and not any((self.numeric, self.entities, self.rules, self.rubric)):
            msg = "expected needs at least one grading block"
            raise ValueError(msg)
        if loose:
            rules = self.rules or RulesExpected()
            rules.must_mention = [*rules.must_mention, *self.must_mention]
            rules.must_not_claim = [*rules.must_not_claim, *self.must_not_claim]
            self.rules, self.must_mention, self.must_not_claim = rules, [], []
        return self


class EvalQuestion(_Model):
    id: str = Field(pattern=r"^[GFO][0-9]{2}$")
    pipeline: Literal["chat", "funding", "org"]
    question: str = Field(min_length=1, max_length=1000)
    tags: list[str] = []
    datasets: list[Dataset] | None = None
    setup: Literal["seed_prior_run"] | None = None
    framing: bool = False
    expected: Expected


class SuiteDefaults(_Model):
    tolerance: Tolerance
    datasets: list[Dataset] = Field(min_length=1)


class Suite(_Model):
    version: Literal[3]
    defaults: SuiteDefaults
    questions: list[EvalQuestion] = Field(max_length=MAX_QUESTIONS)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReferenceResult(BaseModel):  # one executed reference query, ≤ MAX_REFERENCE_ROWS rows
    model_config = ConfigDict(extra="forbid", frozen=True)
    query_id: str
    columns: list[str]
    rows: list[_Row]


class ResolvedQuestion(BaseModel):  # references run and placeholders resolved, or skipped
    model_config = ConfigDict(extra="forbid", frozen=True)
    question: EvalQuestion
    text: str
    reference: dict[str, ReferenceResult] = {}
    placeholders: dict[str, str] = {}
    truth_values: dict[str, str] = {}
    skip_reason: str | None = None


def _read_suite(path: Path) -> tuple[object, str]:
    try:
        with path.open("rb") as fh:  # never read more than the cap + 1 byte
            data = fh.read(MAX_SUITE_BYTES + 1)
    except OSError as exc:
        msg = "cannot read golden suite"
        raise ConfigError(msg, hint=type(exc).__name__, path=path.name) from exc
    if len(data) > MAX_SUITE_BYTES:
        msg = "golden suite exceeds 1 MB"
        raise ConfigError(msg, path=path.name)
    try:
        return _parse_yaml(data.decode("utf-8")), hashlib.sha256(data).hexdigest()
    except (UnicodeDecodeError, yaml.YAMLError, ValueError, RecursionError) as exc:
        msg = "golden suite is not valid YAML"
        raise ConfigError(msg, hint=type(exc).__name__, path=path.name) from exc


def _question_defaults(raw: object, defaults: dict[object, object]) -> object:
    """Fill `datasets` and `numeric.tolerance` from `defaults` where the question omits them."""
    if not isinstance(raw, dict):
        return raw
    out = dict(raw)
    if out.get("datasets") is None and "datasets" in defaults:
        out["datasets"] = defaults["datasets"]
    exp = out.get("expected")
    num = exp.get("numeric") if isinstance(exp, dict) else None
    if isinstance(num, dict) and num.get("tolerance") is None and "tolerance" in defaults:
        out["expected"] = {**(exp or {}), "numeric": {**num, "tolerance": defaults["tolerance"]}}
    return out


def _invalid(exc: ValidationError, questions: object, name: str) -> ConfigError:
    """`ConfigError` naming the first question an error points at; no input values."""
    errors = exc.errors(include_input=False)
    where = None
    for loc in (e["loc"] for e in errors):
        if len(loc) > 1 and loc[0] == "questions" and isinstance(loc[1], int):
            item = questions[loc[1]] if isinstance(questions, list) else None
            qid = item.get("id") if isinstance(item, dict) else None
            where = qid[:16] if isinstance(qid, str) else f"#{loc[1]}"
            break
    hint = "; ".join(f"{'.'.join(map(str, e['loc'])) or 'suite'}: {e['msg']}" for e in errors[:5])
    msg = f"golden suite {f'question {where} ' if where else ''}failed validation"
    return ConfigError(msg, hint=hint, path=name, question_id=where)


def load_suite(path: Path) -> Suite:
    """Load, size-check, default and validate a golden suite file (version 3)."""
    raw, digest = _read_suite(path)
    if not isinstance(raw, dict):
        msg = "golden suite must be a mapping"
        raise ConfigError(msg, path=path.name)
    defaults, questions = raw.get("defaults"), raw.get("questions")
    if isinstance(defaults, dict) and isinstance(questions, list):
        raw = {**raw, "questions": [_question_defaults(q, defaults) for q in questions]}
    try:
        suite = Suite.model_validate({**raw, "sha256": digest})
    except ValidationError as exc:
        raise _invalid(exc, questions, path.name) from exc
    ids = [question.id for question in suite.questions]
    duplicate = next((qid for i, qid in enumerate(ids) if qid in ids[:i]), None)
    if duplicate is not None:
        msg = f"golden suite question {duplicate} is a duplicate id"
        raise ConfigError(msg, path=path.name, question_id=duplicate)
    return suite


def _text(value: object) -> str:
    return ", ".join(map(str, value)) if isinstance(value, list) else str(value)


def _run_sql(cur: duckdb.DuckDBPyConnection, sql: str) -> tuple[list[str], list[_Row]]:
    """Execute with a `QUERY_TIMEOUT_S` interrupt timer; fetch ≤ `MAX_REFERENCE_ROWS` rows."""
    timer = threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)  # always cancelled below
    timer.start()
    try:
        cur.execute(sql)
        columns = [str(d[0]) for d in cur.description or ()]
        rows = [tuple(row) for row in cur.fetchmany(MAX_REFERENCE_ROWS)]
    finally:
        timer.cancel()
    return columns, rows


def _guard(cur: duckdb.DuckDBPyConnection) -> SqlGuard:
    """A `SqlGuard` over the build's allowed schemas with the default blocked columns."""
    schema: dict[str, dict[str, dict[str, str]]] = {}  # no empty schema: sqlglot needs depth
    for db, table, column, kind in cur.execute(_SCHEMA_SQL, [sorted(ALLOWED_SCHEMAS)]).fetchall():
        schema.setdefault(db, {}).setdefault(table, {})[column] = kind
    return SqlGuard(schema, SqlSettings().blocked_columns)


def _blocks(exp: Expected) -> list[_Block]:  # in `{name}` lookup order (see `_BLOCKS`)
    sql, pairs = exp.placeholders_sql, (("entities", exp.entities), ("numeric", exp.numeric))
    out: list[_Block] = [("placeholders_sql", sql, None)] if sql else []
    return out + [(n, b.reference_sql, b.truth_ref) for n, b in pairs if b is not None]


def _placeholder_names(question: EvalQuestion) -> list[str]:  # question and rule strings
    rules = question.expected.rules or RulesExpected()
    texts = [question.question, *(p for c in rules.must_not_claim for p in (c.entity, c.pattern))]
    for item in rules.must_mention:
        texts += [item] if isinstance(item, str) else [item.entity, *item.with_any]
    return list(dict.fromkeys(m.group(1) for t in texts for m in _PLACEHOLDER_RE.finditer(t)))


@dataclass(frozen=True, slots=True)
class _Resolver:
    qid: str
    cur: duckdb.DuckDBPyConnection
    truth: TruthManifest | None
    build_id: str
    cache: dict[str, ReferenceResult]
    guard: SqlGuard

    def fail(self, reason: str, hint: str | None = None) -> NoReturn:
        raise SuiteError(reason, hint=hint, question_id=self.qid)

    def reference(self, sql: str) -> ReferenceResult:
        try:
            self.guard.check(sql)
        except QueryError as exc:
            self.fail("reference SQL rejected", exc.hint)
        qid = query_id(sql, {}, self.build_id)
        if (cached := self.cache.get(qid)) is not None:
            return cached
        try:
            columns, rows = _run_sql(self.cur, sql)
        except duckdb.InterruptException:
            self.fail("reference SQL timed out")
        except duckdb.Error as exc:
            self.fail("reference SQL failed", type(exc).__name__)
        if not rows:
            self.fail("empty reference")
        self.cache[qid] = ReferenceResult(query_id=qid, columns=columns, rows=rows)
        return self.cache[qid]

    def plant(self, path: str, values: dict[str, str]) -> str:
        if self.truth is None:
            self.fail("truth manifest not available", path)
        try:
            value = _text(plant_value(self.truth, path))
        except SuiteError as exc:
            raise SuiteError(exc.message, hint=path, question_id=self.qid) from exc
        values[path] = value
        return value

    def display(self, value: str) -> str:
        for sql in _DISPLAY_SQL:
            try:
                row = self.cur.execute(sql, [value]).fetchone()
            except duckdb.Error:  # the build has no such display table
                continue
            if row is not None and row[0] is not None:
                return str(row[0])
        self.fail("no display name for entity")

    def placeholder(self, name: str, ref: _Refs, values: dict[str, str]) -> str:
        if _TRUTH_NAME_RE.match(name):
            head, _, attr = name.rpartition(".")
            value = self.plant(f"{head}.{_ALIASES.get(attr, attr)}", values)
            return self.display(value) if attr in _DISPLAY_ATTRS else value
        if name == "entity_name" and "entities" in ref:
            return self.display(_text(ref["entities"].rows[0][0]))
        for block in () if name == "entity_name" else _BLOCKS:
            result = ref.get(block)
            if result is not None and name in result.columns:
                return _text(result.rows[0][result.columns.index(name)])
        self.fail("unresolved placeholder", name)


def resolve(
    question: EvalQuestion,
    truth: TruthManifest | None,
    con: duckdb.DuckDBPyConnection,
    *,
    build_id: str,
    dataset_kind: Dataset,
    cache: dict[str, ReferenceResult],
) -> ResolvedQuestion:
    """Run the question's reference SQL read-only and resolve its placeholders (§4.5)."""
    blocks = _blocks(question.expected)
    wrong_kind = question.datasets is not None and dataset_kind not in question.datasets
    if wrong_kind or (dataset_kind == "real" and any(ref for _, _, ref in blocks)):
        skip = "dataset" if wrong_kind else "truth_ref_on_real"
        return ResolvedQuestion(question=question, text=question.question, skip_reason=skip)
    cur = con.cursor()  # per-thread cursor
    try:
        manifest = truth if dataset_kind == "synthetic" else None
        ctx = _Resolver(question.id, cur, manifest, build_id, cache, _guard(cur))
        reference = {name: ctx.reference(sql) for name, sql, _ in blocks}
        truth_values: dict[str, str] = {}
        for name, _sql, ref in blocks:  # truth_ref must equal reference row 1, column 1
            if ref and ctx.plant(ref, truth_values) != _text(reference[name].rows[0][0]):
                ctx.fail("truth_ref mismatch", ref)
        names = _placeholder_names(question)
        placeholders = {name: ctx.placeholder(name, reference, truth_values) for name in names}
    finally:
        cur.close()
    text = _PLACEHOLDER_RE.sub(lambda m: placeholders[m.group(1)], question.question)
    return ResolvedQuestion(
        question=question, text=text, reference=reference, placeholders=placeholders,
        truth_values=truth_values,
    )  # fmt: skip
