"""The `eval` job payload, its GPU class, idempotency key and the CLI exit code (U11-69).

Private sibling of `herness.eval.runner` (§2 module map), which re-exports every public name
here. `EvalPayload` is the closed, validated form of `ctx.job.payload` (U11-68 step 1,
R-42): `repeat` is bounded to 1..10 (TH11-13), so a payload asking for more is rejected.
`build_eval_payload` is what the `herness eval` command (T09-24, R-47) enqueues.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from herness.core.config import get_config
from herness.core.errors import ConfigError
from herness.core.types import Depth
from herness.eval.settings import EvalConfig

__all__ = [
    "EXIT_CONFIG",
    "EXIT_GATE",
    "EXIT_OK",
    "EXIT_OTHER",
    "EvalPayload",
    "build_eval_payload",
    "eval_gpu_class",
    "eval_idem_key",
    "exit_code",
]

EXIT_OK: Final = 0
EXIT_OTHER: Final = 1
EXIT_CONFIG: Final = 3
EXIT_GATE: Final = 14
CLASSIFIER_SUITE: Final = "classifier"

type Profile = Literal["local", "hybrid", "premium", "synth"]
type EvalGpu = Literal["none", "decider", "reasoning"]  # U11-69: never `large`


class EvalPayload(BaseModel):
    """`ctx.job.payload` of an `eval` job (U11-68 step 1); closed and strictly typed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    suite: str = Field(default="golden", min_length=1, max_length=512)
    profile: Profile
    compare_profile: Profile | None = None
    depth: Depth = "standard"
    ids: list[str] = Field(default=[], max_length=500)
    tags: list[str] = Field(default=[], max_length=100)
    repeat: int = Field(ge=1, le=10)  # TH11-13
    mock_llm_dir: str | None = Field(default=None, min_length=1, max_length=1024)
    no_judge: bool = False
    resume_run_id: str | None = Field(default=None, pattern=r"^run_[0-9A-HJKMNP-TV-Z]{26}$")
    baseline: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    set_baseline: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    compare_runs: list[str] = Field(default=[], max_length=10)


def validate_payload(payload: Mapping[str, object]) -> EvalPayload:
    """`payload` as `EvalPayload`, else `ConfigError` (field names only, never values)."""
    try:
        return EvalPayload.model_validate(dict(payload))
    except ValidationError as exc:
        fields = sorted({".".join(map(str, err["loc"])) for err in exc.errors()})
        msg = f"eval payload invalid: {', '.join(fields)}"
        raise ConfigError(msg) from exc


def _settings(options: Mapping[str, Any]) -> EvalConfig:
    settings = options.get("settings")
    if isinstance(settings, EvalConfig):
        return settings
    loaded = get_config().eval
    if loaded is None:
        msg = "eval settings are not configured"
        raise ConfigError(msg)
    return loaded


def build_eval_payload(**options: Any) -> dict[str, Any]:  # noqa: ANN401 - CLI option values
    """The job payload for `options` (U11-69): the U11-68 keys only, validated.

    `repeat` defaults to `eval.yaml: repeat[depth]` (`settings=` may pass an `EvalConfig`,
    else the loaded config's `eval` section). `priority` is not a payload key: it stays
    None so the impl 08 per-kind default applies (R-41); the key comes from `eval_idem_key`.
    """
    keys = set(EvalPayload.model_fields)
    raw = {k: v for k, v in options.items() if k in keys and v is not None}
    unknown = sorted(set(options) - keys - {"settings"})
    if unknown:
        msg = f"unknown eval options: {', '.join(unknown)}"
        raise ConfigError(msg)
    for name in ("ids", "tags", "compare_runs"):
        if name in raw:
            raw[name] = list(raw[name])
    if "repeat" not in raw:
        raw["repeat"] = _settings(options).repeat[raw.get("depth", "standard")]
    return validate_payload(raw).model_dump(mode="json")


def eval_idem_key(payload: Mapping[str, object]) -> str | None:
    """`resume:eval:<run_id>` for a resume, else None (the spec 08 default key, U11-69)."""
    run_id = payload.get("resume_run_id")
    return f"resume:eval:{run_id}" if isinstance(run_id, str) else None


def eval_gpu_class(payload: Mapping[str, object]) -> EvalGpu:
    """`none` for a mock or compare-runs job, `decider` for the classifier suite (R-43),
    else `reasoning` (U11-69)."""
    if payload.get("mock_llm_dir") or payload.get("compare_runs"):
        return "none"
    return "decider" if payload.get("suite") == CLASSIFIER_SUITE else "reasoning"


def _failed_class(outcome: Mapping[str, object]) -> str | None:
    """The error class of a failed job (`last_error.class`, or `error_type`), else None."""
    error = outcome.get("last_error")
    if isinstance(error, Mapping) and isinstance(error.get("class"), str):
        return str(error["class"])
    kind = outcome.get("error_type")
    return kind if isinstance(kind, str) else None


def exit_code(outcome: Mapping[str, object]) -> int:
    """The CLI exit code of an eval job (U11-69, R-46): 0 passed, 14 an eval gate failed,
    3 a `ConfigError`, 1 any other job failure. Never 2 (Typer usage) and never 4.

    `outcome` is a job outcome or row (`status` with `result` or `last_error`), or the
    `{run_id, passed, exit_code}` result itself.
    """
    failed = _failed_class(outcome)
    if failed is not None or outcome.get("status") == "failed":
        return EXIT_CONFIG if failed == "ConfigError" else EXIT_OTHER
    result = outcome.get("result", outcome)
    if not isinstance(result, Mapping) or "passed" not in result:
        return EXIT_OTHER
    if result.get("passed") is True:
        return EXIT_OK
    return EXIT_GATE
