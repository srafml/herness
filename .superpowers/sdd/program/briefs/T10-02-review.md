# T10-02 review: YAML reading and layer sources

Verdict: **Needs fixes**. There are 2 Important findings and 0 Critical findings. The module-size overrun is already covered by a controller ruling (trim to 390 or fewer); trimming candidates are listed below.

### Spec Compliance

Per unit:
- ✅ U10-15 `load_yaml_file`
  - The missing-file and too-large messages are exact. `st_size` is checked before any read. A BOM is stripped (`utf-8-sig`). An empty document gives `{}`.
  - A top level that is not a mapping, and invalid YAML, each give the exact messages.
  - An anchor or alias on any node raises the exact message with `<name>:<line>`. My probe confirmed this for scalars, mappings, sequence items and mapping keys.
  - Duplicate keys raise the exact message. `1`/`true` key collisions are rejected too, which is correct because PyYAML would otherwise let one value shadow the other.
  - Merge keys `<<` are rejected: with an alias it is the anchor error; with an inline mapping it is `invalid YAML`, because the merge tag has no constructor when the key is built first.
  - `!!python/*` tags give `invalid YAML`.
  - Exception: deep nesting escapes as `RecursionError` (Important 1).
- ✅ U10-16 `FilesYamlSource`
  - `version` rule, root-key subset, fixed stem list, `version` kept only for `sources`, `eval.yaml` optional, `injection_patterns.txt` (1 MiB cap, required, comment and blank filtering), unexpected `*.yaml` file.
  - The messages match the spec.
- ✅ U10-17 `ProfileYamlSource`
  - Missing overlay, required and dropped `version`, section names (`profile` is excluded).
  - `profiles may not set security.data_policy`: any `data_policy` key is rejected, including `{}` and null.
  - `profile synth cannot enable egress`.
  - `HERNESS_SYNTH_CONFIG`: profile `synth` required, only `mappings` allowed, deep-merged after the overlay.
- ✅ U10-18 `GuardedEnvSource` / `FilteredDotEnvSource`
  - Skip list and `HERNESS_SECRET__` skip.
  - `security.* is file-only; remove <NAME>`. The check ignores case, and also catches bare `HERNESS_SECURITY` and `HERNESS_PROFILE__*`, which enforces the "no `profile` key" postcondition.
  - `.env` is read only when `HERNESS_ENV=dev`: from `<config_dir parent>/.env`, with comments, double quotes, a 64 KiB cap and malformed-line errors that give the line number only.
  - The 500-variable cap is enforced.
  - TH10-02 holds for both the env and `.env` paths.
- ✅ U10-19 `parse_overrides`
  - Split at the first `=`, with the exact `--set needs key=value:` message.
  - Segment regex `[A-Za-z0-9_-]{1,64}` (uses `[0-9]`, not `\d`).
  - At most 12 segments, values of at most 4,096 characters, at most 100 overrides.
  - The value is parsed by the strict loader: aliases, anchors and duplicates are rejected, and the error names the key path only.
  - A later override replaces an earlier one. Descending into an earlier scalar or list raises `ConfigError`.
  - Exception: deep nesting in a value escapes as `RecursionError` (Important 1).
  - The `security.`/`profile` ban is correctly left to U10-09 step 3.
- ✅ U10-21 `BootstrapConfig` / `load_bootstrap`
  - Frozen dataclass with the three fields. Profile resolved as in U10-09 step 1.
  - The `security` subtrees are deep-merged and validated with `SecurityConfig`. `ValidationError` becomes a `ConfigError` with `include_input=False`, so no value is echoed.
  - R-06 host rule:
    - Disabled sources and disabled nested mappings contribute nothing.
    - SDK sources contribute only `hosts`.
    - Non-SDK sources also contribute the `base_url` hostnames found at any depth.
    - Hosts are lower-cased, deduplicated and sorted.
  - Step 6 is applied.
  - The overlay `data_policy` check is also applied here, which is stricter than the spec and closes TH10-03 at bootstrap too.
- ✅ List-to-tuple handling: the sources emit plain YAML, and `herness.core.settings._Section` runs `_lists_to_tuples` as a `mode="before"` validator (settings.py:142-148). A probe confirmed `SecurityConfig` accepts YAML lists and lower-cases `destinations`.

Per test row:
- ✅ UT10-05: `test_ut10_05_*` (missing `sources.yaml` exact message; `eval` becomes None).
- ❌ UT10-06: no `test_ut10_06_*` function exists (Important 2). The "unknown root key" half is exercised, but under the UT10-05 name at test_config_sources.py:210.
- ✅ UT10-07: duplicate key at line 7; message contains `duplicate key`, `:7` and the file name.
- ✅ UT10-08: anchor and alias in `metrics.yaml`, with the exact message and line.
- ✅ UT10-10: the env half, including mixed case. The `--set` half is deferred to T10-03 by ruling.
- ✅ UT10-13: overlay `data_policy` (exact message), plus shape rules.
- ✅ UT10-14: merged; `sources` rejected; profile `local` rejected.
- ✅ UT10-23: all four rejections; each message contains the key path.
- ✅ UT10-24: every clause of the row is asserted (hosts of enabled sources only; lower-cased; Snowflake `base_url` absent and its `hosts` present; ServiceNow both present), plus nested disabled mappings and lists.
- ✅ PT10-06: hypothesis round trip, 150 examples.
- ✅ ST10-01: env and dev `.env`; the exact message is asserted and egress stays off.
- ➖ ST10-02: deferred to T10-03 by ruling.
- ✅ ST10-03: through the source layer and through `load_bootstrap`.
- ✅ ST10-05: billion laughs, a 6 MiB file and a duplicate `enabled` key, each rejected in < 1 s.

⚠️ Cannot verify from the diff:
- The gates reported green: ruff, mypy, lint-imports, check_type_ownership, 50 passed, 99 % / 100 % coverage. I only ran a targeted probe (below).
- U10-21 "< 50 ms": no test or benchmark covers it.
- The strict list fields of other owners' models under the future `HernessConfig`: whether each owner model converts lists itself is T10-03's concern. It is not exercised here.
- SDK detection keys on the source *name* (U10-21/U10-58 wording). If connector settings (impl 01) key sources by a `kind` field distinct from the name, `_source_hosts` would derive `base_url` hosts for SDK sources. This matches the spec text as written; T10-17 should confirm it.

Probe run (scratchpad script, read-only):
- Every `load_yaml_file` anchor, alias, merge and tag case above behaved as described.
- A file containing `a: ` + `[`×50000 + `]`×50000 raised `RecursionError`.
- `{b: `×20000 also raised `RecursionError`.
- `parse_overrides(["a=" + "["*2000 + "]"*2000])`, a value under 4,096 characters, raised `RecursionError`.

### Strengths
- Anchor rejection is done at `compose_node` on the peeked event. It therefore catches anchors on every node kind, including scalars and keys, and aliases, before any expansion. Billion laughs is rejected in about 0 ms.
- The size cap is checked by `st_size` before the read. The 6 MiB test asserts < 1 s.
- Error messages are spec-exact and assert-exact in the tests. No value is echoed: `--set`, env and `ValidationError` messages carry names only.
- The file-only rule is case-insensitive and also covers the bare `HERNESS_SECURITY` and `HERNESS_PROFILE__*` forms.
- The R-06 host derivation is careful: it prunes disabled sub-mappings, walks lists, and catches `urlsplit` `ValueError`. It is fully covered by a single table-driven UT10-24 test.
- The test harness wires the sources in exactly the U10-08 priority order, so precedence is tested for real rather than mocked.

### Issues

#### Critical (Must Fix)
None.

#### Important (Should Fix)

1. **Deeply nested YAML escapes as `RecursionError`, not `ConfigError` (TH10-04 D; U10-15 and U10-19 error contracts).** `herness/core/config_sources.py:76-84` (`_parse`) catches only `yaml.YAMLError`.
   - A 5 MiB-or-smaller config file of nested flow collections crashes `load_yaml_file` and `load_bootstrap` with `RecursionError`. The CLI would print a traceback instead of the fatal-config path.
   - The same happens for a `--set` or `HERNESS_*` value under the 4,096-character cap (2,000 brackets). That breaks U10-19's "ConfigError naming the key path only".
   - Fix: bound the nesting depth in `_StrictLoader.compose_node`, for example with a depth counter that rejects anything deeper than about 100 with `ConfigError("<name>:<line>: YAML nesting too deep")`. Alternatively, and at minimum, map `RecursionError` in `_parse` to `<name>:<line>: invalid YAML`.
   - Add a nested-flow-collection case to ST10-05 and a `--set` case to UT10-23.

2. **UT10-06 has no test function (global constraint: each ID in the card's Tests row needs at least one function).** The card's Tests row is "UT10-05–UT10-08", which includes UT10-06. The "unknown root key" rule is tested only under the UT10-05 name (`tests/unit/core/test_config_sources.py:199-214`, the `herness.yaml ... unknown root key sources` parameter).
   - Fix: add `test_ut10_06_unknown_root_key_names_key` for the unknown root key in `herness.yaml` and the unknown section in the overlay. Assert that the key path is named and that the value is not echoed (use a distinctive value).
   - The "unknown nested field" half needs `HernessConfig` plus the U10-09 `ValidationError` conversion. Record it explicitly as deferred to T10-03, the same way as the `--set` half of UT10-10.

#### Minor (Nice to Have)

1. `config_sources.py:87-96`: the file is stat'ed and then read in full, which is a TOCTOU gap: a file that grows between the two calls is read unbounded. Reading with `f.read(max_bytes + 1)` and rejecting when `len > max_bytes` enforces the cap on the bytes actually read.
2. The `--set` key part is unbounded and echoed in full.
   - `config_sources.py:134` (`invalid key path`) and `config_sources.py:152` (`--set needs key=value: {key}`) echo it; a 5,000-character key was echoed in full in my probe.
   - Fix: truncate the label, for example to 200 characters, or cap the item length.
3. `config_sources.py:140`: the message says "already set to a scalar" when the earlier value was a list (`a.b=[1]`, then `a.b.c=2`). Use "is not a mapping".
4. `config_sources.py:273`: the overlay version error is labelled `hybrid.yaml: version must be 1`, while the other overlay errors use `profiles/hybrid.yaml`. The test at test_config_sources.py:264 pins the inconsistent label. Use `label` for both.
5. `config_sources.py:350` and `config_sources.py:366`: `enabled` is tested with `is False`, which follows the spec literally. `enabled: "false"` or `enabled: 0` is treated as enabled here, but pydantic's lax mode would coerce it to disabled in the full config. The bootstrap allowlist can then be wider than the full-config allowlist.
   - The risk is low, because hosts are listed by the operator. Consider treating any falsy non-None `enabled` as disabled, or note the gap for T10-17 and C20.
6. Test IDs borrowed from T10-03's rows can give false traceability once T10-03 lands. These rows are "load" rows owned by T10-03 and cannot be fully met at source level (UT10-09 needs the gate and `cfg.profile`; UT10-12 needs `local` with egress in `herness.yaml` through `load`):
   - `test_ut10_09_*` (test_config_sources.py:238)
   - `test_ut10_12_*` (:288, :408)
   - `test_ut10_02_*` (:329, :365, :380)
   - `test_ut10_03_*` (:347)

   T10-03 must still add real `load_config` tests for them. Consider `test_rf_*` names, or say so in the docstrings.
7. `test_ut10_10_harness_validation_is_pydantic` (test_config_sources.py:516) tests the test harness's own `dict` field typing, not product behaviour. Drop it, or turn it into a real product assertion.
8. Weak assertions:
   - `test_ut10_08_alias_without_anchor_rejected` (:55) asserts only `ConfigError`.
   - `_timed_reject` (tests/security/test_st10_config_sources.py:56-60) asserts only `ConfigError`, not which mitigation fired.

   Assert the message: `anchors and aliases` for billion laughs, `too large` for the 6 MiB file, `duplicate key 'enabled'` for the duplicate. A regression that changed which rule fires would then show.
9. No test covers a merge key `<<`. Add `b: {<<: {k: 1}}` and `<<: *x` cases to UT10-08 to pin the current rejection.

### Module size (ruling: budget 390; the file is 398)

These trims are concrete and preserve behaviour, about 10 to 14 lines together:
- Let `_fail` do `raise ConfigError(msg) from None`. Inside an `except` block this still suppresses the context. Each of the 5 `msg = ...; raise ConfigError(msg) from None` pairs then becomes one `_fail(...)` line (lines 83-84, 91-92, 98-99, 128-129, 394-395), saving 5 lines.
- Replace the root-key loops with set differences: `extra = out.keys() - set(ROOT_SECTIONS)` in `FilesYamlSource` (lines 250-252), and the same in `ProfileYamlSource` (lines 274-276). This saves about 2 lines.
- Drop `_PROFILE_BY_NAME` (line 45) and write `if value not in PROFILES: _fail(...)` followed by `return cast("ProfileName", value)` in `resolve_profile`. This saves about 1 line.
- Inline the `what = ...` temporary into the `_fail` f-string in `GuardedEnvSource._layer` (lines 308-310), and the `keys` temporary in `check_overlay_security`. This saves about 2 lines.
- Move `current_load_context()` (line 182, 4 lines) out only if T10-03 does not need it. Otherwise keep it: it is the sanctioned public accessor.
- Optional: extract the `ValidationError` → `ConfigError` conversion (lines 391-395) into a public helper that T10-03 reuses for U10-09 step 4. This is DRY across the two callers but saves no lines here.

### Assessment
**Task quality:** Needs fixes

**Reasoning:** The implementation is spec-exact and the TH10-02, TH10-03 and TH10-04 mitigations are solid, with one gap: nested YAML and `--set` values escape the `ConfigError` contract as `RecursionError`. UT10-06 has no test function. Both fixes are small, and the module trim (to 390 or fewer) can go in the same fix round.


---

## Re-review round 1 (fix commit 214cd19, diff vs 0311abc)

**Verdict: Approved.** All earlier findings are resolved, and the module budget is met: `herness/core/config_sources.py` is 390 lines against the raised budget of 390 (spec §2, line 87 updated in the same commit).

### Earlier findings

| # | Finding | Status | Evidence |
|---|---------|--------|----------|
| I-1 | Deep nesting escapes as `RecursionError` | ✅ Fixed | The depth counter in `_StrictLoader.compose_node` (limit 64) raises `<name>:<line>: invalid YAML (nested deeper than 64)`. `_parse` also maps `RecursionError` to the invalid-YAML error as a backstop. New tests: ST10-05 `test_st10_05_deep_nesting` and UT10-23 `test_ut10_23_deeply_nested_value_is_config_error`. The latter covers a depth-10 value that still parses. |
| I-2 | UT10-06 has no test | ✅ Fixed | `test_ut10_06_unknown_keys_name_path_without_value` asserts exact messages and that a sentinel value is not echoed. The nested-field half is recorded as carry-over to T10-03. |
| m-1 | Stat, then an unbounded read | ✅ Fixed | `_read_text` reads at most `max_bytes + 1` bytes from an open handle. |
| m-2 | Unbounded key echo | ✅ Fixed | The `--set` key, profile name, unknown-key names and file-only env name are truncated to 100 characters. The test asserts a message under 200 characters for a 5,000-character key. |
| m-3 | "scalar" wording | ✅ Fixed | The message now says "non-mapping value". The list case is tested. |
| m-4 | Overlay version label | ✅ Fixed | The message is `profiles/<name>.yaml: version must be 1`, and the test is updated. |
| m-5 | `enabled` truthiness | ✅ Fixed | `_enabled` requires a bool. See the ruling below. |
| m-6 | Borrowed T10-03 IDs | ✅ Fixed | Seven tests are renamed to `test_rf_*` with docstrings starting "RF". The rows are listed as T10-03 carry-over. |
| m-7 | Test of the harness itself | ✅ Fixed | The test is removed. |
| m-8 | Weak assertions | ✅ Fixed | `_timed_reject` takes a pattern and each ST10-05 case pins its message. The bare-alias test asserts the exact message. |
| m-9 | `<<` untested | ✅ Fixed | `test_ut10_08_merge_keys_rejected` covers both the alias form and the inline form. |
| Budget | 398 lines against a 360 budget | ✅ Fixed | 390 of 390 lines. |

### Probe re-run (same scratchpad script, read-only)

| Case | Result |
|------|--------|
| 50,000 nested `[` | `ConfigError: deep_nest.yaml:1: invalid YAML (nested deeper than 64)` in 0.11 s |
| 20,000 nested `{b:` | same error, in 0.002 s |
| `--set a=` + 2,000 nested brackets | `ConfigError: --set a: invalid YAML value` |
| Anchors on scalars, mappings, sequence items and keys | anchor error |
| `<<` merge, alias form | anchor error |
| `<<` merge, inline form | `invalid YAML` |
| Duplicate keys | duplicate-key error |
| `!!python` tag | `invalid YAML` |

The long-key message is now truncated, and no `RecursionError` appears anywhere.

### Ruling: `enabled` must be a real YAML bool

This is acceptable, and it fails closed.

- **The full config already rejects these values.** The owner model is `SourceSettings.enabled: bool` under `ConfigDict(strict=True)` (`herness/connectors/settings_base.py:51` and `:233`). Strict mode rejects `"false"`, `0`, `0.0` and `"no"`-as-string. The bootstrap now agrees with the full config instead of reading those values as enabled.
- **It does not contradict the spec.** U10-21 and U10-58 only say "does not have `enabled: false`" and are silent on values that are not bools. Raising `ConfigError` is covered by U10-21's error contract ("`ConfigError` as U10-15 and U10-09").
- **TH10-02 is unaffected.** TH10-02 concerns env and `--set` changing `security.*`, and the host allowlist is TH10-48. For TH10-48 this change can only narrow the allowlist or stop start-up. It can never widen it.
- **Watch item.** The check applies to *every* nested mapping that has an `enabled` key inside a non-SDK source. If a future owner model declares a nested `enabled` that is not a bool, for example an enum, bootstrap would reject a config the full model accepts. No such field exists today; `enabled` appears only in `settings_base.py:233`.

### ⚠️ Spec-level note for the controller (not a builder defect; it predates this fix)

A source with no `enabled` key is treated as **enabled** by the bootstrap host rule (`node.get("enabled", True)`), which follows the U10-21 and U10-58 text literally ("does not have `enabled: false`"). But `SourceSettings.enabled` defaults to **False** (`settings_base.py:233`). So the bootstrap allowlist includes the hosts of sources that the full config treats as disabled. This contradicts the builder's claim that the bootstrap allowlist "can never be wider" than the full config's.

Recommend a spec ruling for T10-17 and U10-58: either "only sources with `enabled: true`" or "the default is enabled". Also align the full-config path of U10-58 step 1 with whichever is chosen.

### Nits (non-blocking)
- The grouped limit constants (`_MAX_PATTERNS_BYTES, ... = ...`, `_MAX_OVERRIDES, ... = ...`) lost their `Final` annotation. It is harmless, but inconsistent with the other constants.
- `GuardedEnvSource._layer` passes the untruncated env name as the `_assign` label, for the "invalid key path" and "invalid YAML value" messages. Env names are OS-bounded, so this is cosmetic.

**Task quality:** Approved

**Reasoning:** Every earlier Important and Minor finding is fixed with a test pinning it. The RecursionError probe is now clean. The module is at its 390-line budget. The strict `enabled` change fails closed and matches the strict owner model.
