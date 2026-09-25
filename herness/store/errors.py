"""Store error subclasses (impl 02 U02-01 … U02-05); messages name identifiers only (TH02-14)."""

from typing import ClassVar, Literal

from herness.core.errors import FatalError, NotFound, RecoverableError, SchemaViolation

type LakeWriterState = Literal["committed", "aborted"]
type MigrationReason = Literal[
    "checksum_mismatch",
    "apply_failed",
    "out_of_range",
    "duplicate_version",
    "unknown_applied",
    "sqlite_too_old",
]


class NotFoundError(NotFound):
    """A requested ops row, build or pointer does not exist (CLI exit 7, R-46)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("kind", "key")

    def __init__(self, message: str, *, kind: str, key: str) -> None:
        super().__init__(message, details={"kind": kind, "key": key})
        self.kind, self.key = kind, key


class ReviewItemConflict(RecoverableError):  # noqa: N818 - name fixed by impl 02 U02-02
    """A review decision was attempted on an item that is not ``pending``."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("item_id", "current_status")

    def __init__(self, item_id: str, current_status: str) -> None:
        super().__init__(f"review item {item_id} is already {current_status}")
        self.item_id, self.current_status = item_id, current_status


class LakeContractError(SchemaViolation):
    """A batch handed to ``LakeWriter.write`` violates the lake contract (design 02 §3.1)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("source", "entity", "rule", "column", "bad_rows")

    def __init__(self, source: str, entity: str, rule: str, column: str, bad_rows: int) -> None:
        where = f"{source}/{entity}: {rule} on {column}"
        super().__init__(f"lake contract violated for {where} ({bad_rows} rows)")
        self.source, self.entity, self.rule, self.column = source, entity, rule, column
        self.bad_rows = bad_rows


class LakeStateError(FatalError):
    """A ``LakeWriter`` method was called after ``commit()`` or ``abort()``."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("source", "entity", "state", "method")

    def __init__(self, source: str, entity: str, state: LakeWriterState, method: str) -> None:
        super().__init__(f"LakeWriter for {source}/{entity} is {state}; {method}() not allowed")
        self.source, self.entity, self.state, self.method = source, entity, state, method


class MigrationError(SchemaViolation):
    """Migration discovery, checksum verification or application failed (R-11)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("version", "name", "reason", "detail")

    def __init__(self, version: int, name: str, reason: MigrationReason, detail: str) -> None:
        super().__init__(f"ops migration {version:03d}_{name}: {reason} ({detail})")
        self.version, self.name, self.reason, self.detail = version, name, reason, detail
