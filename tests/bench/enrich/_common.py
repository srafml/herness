"""Shared constants and helpers of the enrichment benchmarks BT03-01..BT03-11 (T03-36).

Thresholds are the verbatim impl 03 §10 values (`docs/impl/03-enrichment.impl.md`); a miss is
never fixed by editing them. Every row records through the T11-18 recorder (`data/bench/`,
gitignored, owner spec "03") and writes one stderr line
`BT03-NN <metric> <value> <unit> (target <op> <limit> <unit>)`.

Run classes (see the module docstrings and the T03-36 report):

* lake rows (pure pipeline or IO): `HERNESS_BENCH_FULL=1` plus `HERNESS_SYNTH_FULL_ROOT`
  (the seed 42 `full` root, reused and left as generated; generated into a temp folder only
  when the variable is unset), as T02-23 does;
* service rows (a real model or a GPU service): `HERNESS_BENCH_FULL=1` plus the service
  variables below; skipped, with a reason naming the blocker and the variables, otherwise.

All variables are read through `tests.support.bench.bench_flag` (the `HERNESS_BENCH_*`
containment of T11-18); the two `HERNESS_SYNTH_FULL_ROOT` / `HERNESS_EVAL_*` names are plain
environment variables, as in T02-23 and ET11-03.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Final, Literal

import pytest
from tests.support.bench import BenchRecord, BenchRecorder, bench_flag

OWNER: Final = "03"
SEED: Final = 42
TIMEOUT_S: Final = 24 * 3600  # per row; the full-scale builds and the BT03-10 priming are long

FULL_ENV: Final = "HERNESS_BENCH_FULL"
FULL_ROOT_ENV: Final = "HERNESS_SYNTH_FULL_ROOT"
EMBED_MODEL_ENV: Final = "HERNESS_BENCH_EMBED_MODEL_DIR"  # bge-m3 directory (safetensors)
LAYA_DATA_ENV: Final = "HERNESS_BENCH_LAYA_DATA"  # data root whose models/laya/CURRENT is set
OPENJEV_URL_ENV: Final = "HERNESS_BENCH_OPENJEV_URL"  # loopback OpenJev base URL
LLM_PROFILE_ENV: Final = "HERNESS_BENCH_LLM_PROFILE"  # config profile of the local LLM
BENCH_CONFIG_ENV: Final = "HERNESS_BENCH_CONFIG_DIR"  # optional: config dir for LLM_PROFILE

# --- verbatim impl 03 section 10 thresholds -------------------------------------------------
BT01_LIMIT_S: Final = 60.0  # BT03-01 incremental text stage, 10k changed: < 1 min
BT02_RATE_MIN: Final = 700.0  # BT03-02 bge-m3: >= 700 texts/s
BT02_10K_LIMIT_S: Final = 30.0  # BT03-02 nightly 10k: < 30 s
BT03_RATE_MIN: Final = 400.0  # BT03-03 Laya: >= 400 records/s
BT03_10K_LIMIT_S: Final = 60.0  # BT03-03 10k increment: < 1 min
BT04_RATE_MIN: Final = 25.0  # BT03-04 OpenJev escalation: >= 25 records/s
BT05_RATE_TARGET: Final = 5.0  # BT03-05 LLM decider 3 votes: ~5 records/s, recorded (V-12)
BT06_SHARE_MAX: Final = 0.10  # BT03-06 escalation share after acceptance: <= 10 % of pairs
BT07_RECLUSTER_LIMIT_S: Final = 30 * 60.0  # BT03-07 full recluster excl. naming: < 30 min
BT07_NAMING_LIMIT_S: Final = 45 * 60.0  # BT03-07 naming <= 500 calls: < 45 min
BT07_NAMING_CALLS: Final = 500
BT08_LIMIT_S: Final = 2 * 60.0  # BT03-08 nightly incremental clustering 10k: < 2 min
BT09_LIMIT_S: Final = 3 * 60.0  # BT03-09 heuristic change linking: < 3 min
BT10_LIMIT_S: Final = 20 * 60.0  # BT03-10 nightly enrichment total: < 20 min
BT11_LIMIT_S: Final = 5 * 60.0  # BT03-11 mapping suggestions 2k x 5k: < 5 min

NIGHTLY_RECORDS: Final = 10_000  # the nightly increment of BT03-01/02/03/08/10
FULL_MIN_INCIDENTS: Final = 4_000_000  # the data decides the scale label, not the environment

type Op = Literal["lower", "higher"]


def line(  # noqa: PLR0913 - one printed measurement
    bench_id: str, metric: str, value: float, unit: str, limit: float, better: Op,
    *, inclusive: bool = False,
) -> None:  # fmt: skip
    """One stderr line `BT03-NN <metric> <value> <unit> (target <op> <limit> <unit>)`."""
    op = ">=" if better == "higher" else "<=" if inclusive else "<"
    sys.stderr.write(f"{bench_id} {metric} {value:.4f} {unit} (target {op} {limit:g} {unit})\n")


def record(  # noqa: PLR0913, PLR0917 - one measurement: id, metric, value, unit, limit, scale
    recorder: BenchRecorder, bench_id: str, metric: str, value: float, unit: str,
    limit: float, scale: str, *, better: Op = "lower", inclusive: bool = False,
) -> BenchRecord:  # fmt: skip
    """Record one measurement (owner spec 03) and print its line; the record's `passed` is
    the verdict. `inclusive` makes a "lower" threshold a `<=` (BT03-06's "<= 10 %")."""
    if better == "higher":
        passed = value >= limit
    else:
        passed = value <= limit if inclusive else value < limit
    rec = BenchRecord(bench_id, OWNER, metric, value, unit, better, limit, passed, scale)
    recorder.record(rec)
    line(bench_id, metric, value, unit, limit, better, inclusive=inclusive)
    return rec


def record_info(
    recorder: BenchRecorder, bench_id: str, metric: str, value: float, unit: str, scale: str
) -> BenchRecord:
    """Record a measurement without a pass/fail threshold (BT03-05's recorded rate, counts):
    `passed` is true and the threshold 0 (informational rows, spec: 'recorded')."""
    rec = BenchRecord(bench_id, OWNER, metric, value, unit, "higher", 0.0, True, scale)
    recorder.record(rec)
    sys.stderr.write(f"{bench_id} {metric} {value:.4f} {unit} (recorded, no threshold)\n")
    return rec


def full_enabled(config: pytest.Config) -> bool:
    return bench_flag(config, FULL_ENV) == "1"


def require_full(config: pytest.Config, what: str) -> None:
    """Skip unless `HERNESS_BENCH_FULL=1` (full-scale benches are opt-in on an idle host)."""
    if not full_enabled(config):
        pytest.skip(f"full-scale enrichment bench ({what}): set {FULL_ENV}=1 on an idle host")


def require_service(
    config: pytest.Config, what: str, blocker: str, names: Sequence[str]
) -> dict[str, str]:
    """The values of the service variables `names`, or a skip naming `blocker` and the
    missing variables. With `HERNESS_BENCH_FULL=1` and every variable set the row must
    really execute (misconfiguration fails loudly in the row, never passes silently)."""
    values = {name: bench_flag(config, name) or "" for name in names}
    missing = [name for name, value in values.items() if not value]
    if not full_enabled(config) or missing:
        need = ([] if full_enabled(config) else [FULL_ENV + "=1"]) + missing
        pytest.skip(f"{what}: blocked by {blocker}; set {', '.join(need)} on the target PC")
    return values


def scale_label(incidents: int) -> str:
    """`full` when the build holds the spec's 5M incidents (at least 4M), else `small`."""
    return "full" if incidents >= FULL_MIN_INCIDENTS else "small"


def plant_full_scale_xfail(
    request: pytest.FixtureRequest,
    scale: str,
    reason: str,
) -> None:
    """At full scale only, mark the running row as a strict expected failure (a measured miss
    at the spec scale, owned by a follow-up card). Small scale is untouched.
    Strict: when the row passes, the xfail turns into a failure and the marker must go."""
    if scale == "full":
        request.node.add_marker(pytest.mark.xfail(strict=True, reason=reason))
