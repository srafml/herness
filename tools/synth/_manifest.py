"""Truth manifest and `.synth_root` marker of one generation run (U11-23 steps 6-7).

Private sibling of `tools/synth_data.py` (T11-14 spec note), split off for its 250-line
budget; imported only by `tools.synth_data`. `build_manifest` assembles `TruthManifest`
from the catalog's plant targets, the plan's plant sequence numbers (the epic keys and
Jira ids `plants_delivery` gives E2, E2d, E6p and E6u), the change schedule (T3 share of
follow-ups with `caused_by`) and the run's sums (row counts, dirty counters, T4 noise
ratio). Ids are the record ids the pipeline derives: teams
`servicenow:sys_user_group:<sys_id>`, services and CIs `servicenow:cmdb_ci:<sys_id>`
(impl 02 `core.service`, `core.ci`), epics `jira:issue:<id>`. `question_set_version` is
the active one of `config/decisions.yaml`, read through `load_config(profile="synth")`.
`write_marker` writes `<root>/.synth_root` (`{seed, scale, generator_version,
params_hash}`), the proof `--overwrite` requires (TH11-07).
"""

import json
import os
from pathlib import Path
from typing import Final

from herness.core.config import load_config
from herness.eval.truth import TruthManifest
from tools.synth import GENERATOR_VERSION
from tools.synth.catalog_plan import plant_range
from tools.synth.catalog_rows import Catalog
from tools.synth.params import SynthParams, params_hash
from tools.synth.plants_delivery import T2C_FAMILY, epic_month, planned_counts_t2
from tools.synth.plants_delivery_t6 import T6_DROP, e6_created, find_service, planned_counts_t6
from tools.synth.shards import CONFIG_DIR, AggregateResult, require_under

MARKER: Final = ".synth_root"
# design §5.1.5 plant constants recorded in the truth (plants_ops applies the same values)
T1_METRIC: Final = "mttr_hours"
T1_LEVER: Final = "mttr"
T1_MULTIPLIERS: Final = (2.0, 3.0)  # m(t) rises linearly from 2.0 to 3.0 over the span
T3_PAIRS_FILE: Final = "t3_pairs.parquet"
T2_MEMBERS_FILE: Final = "t2_members.parquet"
T5_MULTIPLIER: Final = 2.8
T6_METRIC: Final = "incident_count"


def _team(sys_id: str) -> str:
    return f"servicenow:sys_user_group:{sys_id}"


def _ci(sys_id: str) -> str:
    return f"servicenow:cmdb_ci:{sys_id}"


def _epic(cat: Catalog, service_sys_id: str, seq: int) -> tuple[str, str]:
    """(issue key, record id) of the plant epic numbered `seq` in the service's project,
    as `plants_delivery_t6.epic_issue` builds them."""
    service = find_service(cat, service_sys_id)
    project = next(p for p in cat.projects if p.key == service.jira_project)
    return f"{project.key}-{seq}", f"jira:issue:{project.id_base + seq}"


def _caused_share(cat: Catalog) -> float:
    flags = [flag for slot in cat.change_schedule for flag in slot.caused_by]
    return sum(flags) / len(flags) if flags else 0.0


def _delivery(cat: Catalog, params: SynthParams) -> dict[str, object]:
    p = cat.plants
    e2_seq, _ = plant_range(cat, planned_counts_t2, ("jira", "issue", epic_month(params)))
    e6_month = e6_created(cat, params).date().replace(day=1)
    e6_seq, _ = plant_range(cat, planned_counts_t6, ("jira", "issue", e6_month))
    e2_key, e2_id = _epic(cat, p.s2, e2_seq)
    decoy_key, _ = _epic(cat, p.e2d_service, e2_seq + 1)
    sides: dict[str, dict[str, object]] = {}
    for side, sys_id, seq, effect in (
        ("paid", p.s6p, e6_seq, -T6_DROP),
        ("unpaid", p.s6u, e6_seq + 1, 0.0),
    ):
        key, record_id = _epic(cat, sys_id, seq)
        sides[side] = {"epic_key": key, "epic_record_id": record_id, "service_id": _ci(sys_id)}
        sides[side] |= {"metric": T6_METRIC, "effect": effect}
    return {
        "T2_roi_epic": {
            "epic_key": e2_key,
            "epic_record_id": e2_id,
            "service_id": _ci(p.s2),
            "cluster_members_file": T2_MEMBERS_FILE,
            "decoy_epic_key": decoy_key,
        },
        "T2c_cluster_fix": {
            "service_id": _ci(p.s2c),
            "cluster_members_file": T2_MEMBERS_FILE,
            "template_id": T2C_FAMILY,
        },
        "T6_outcomes": {**sides, "effective_at": p.effective_at},
    }


def _operations(cat: Catalog, agg: AggregateResult) -> dict[str, object]:
    p = cat.plants
    team_name = next(t.name for t in cat.teams if t.sys_id == p.t1_team)
    return {
        "T1_bad_team": {
            "team_id": _team(p.t1_team),
            "team_name": team_name,
            "service_ids": [_ci(s) for s in p.t1_services],
            "metric": T1_METRIC,
            "lever_usd_model": T1_LEVER,
            "multiplier_start": T1_MULTIPLIERS[0],
            "multiplier_end": T1_MULTIPLIERS[1],
        },
        "T3_change_cluster": {  # C3's changes and follow-ups go to S3's support team
            "ci_id": _ci(p.c3),
            "owning_team_id": _team(find_service(cat, p.s3).support_team_sys_id),
            "pairs_file": T3_PAIRS_FILE,
            "source_field_share": _caused_share(cat),
        },
        "T4_noisy_service": {
            "service_id": _ci(p.s4),
            "generated_noise_ratio": agg.generated_noise_ratio,
        },
        "T5_confounder": {
            "team_id": _team(p.t5_team),
            "service_id": _ci(p.s5),
            "peak_window": list(p.peak_window),
            "volume_multiplier": T5_MULTIPLIER,
        },
    }


def _question_set_version(root: Path) -> str:
    data = (root / "data").as_posix()
    cfg = load_config("synth", overrides=(f"paths.data={data}",), config_dir=CONFIG_DIR)
    return cfg.decisions.question_set_version


def build_manifest(
    seed: int, params: SynthParams, cat: Catalog, agg: AggregateResult, *, root: Path
) -> TruthManifest:
    """The truth manifest of a run into the resolved `root` (lake under `<root>/data`)."""
    plants = _operations(cat, agg) | _delivery(cat, params)
    return TruthManifest.model_validate(
        {
            "seed": seed,
            "scale": params.scale,
            "generator_version": GENERATOR_VERSION,
            "params_hash": params_hash(params),
            "start": params.start,
            "end": params.end,
            "question_set_version": _question_set_version(root),
            "dataset_root": (root / "data").as_posix(),
            "row_counts": dict(sorted(agg.row_counts.items())),
            "dirty": agg.dirty.as_dict(),
            "plants": {name: plants[name] for name in sorted(plants)},
        }
    )


def write_marker(root: Path, manifest: TruthManifest) -> Path:
    """`<root>/.synth_root`, written tmp-then-`os.replace` after the truth files."""
    path = require_under(root, root / MARKER)
    body: dict[str, object] = {"seed": manifest.seed, "scale": manifest.scale}
    body |= {"generator_version": manifest.generator_version, "params_hash": manifest.params_hash}
    tmp = path.with_name(f"{MARKER}.tmp")
    try:
        tmp.write_text(json.dumps(body, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


__all__ = ["MARKER", "build_manifest", "write_marker"]
