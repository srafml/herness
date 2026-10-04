# T03-02 review: Settings model (base 951e5c9, head 1875c00)

**Verdict: Approved**

### Spec Compliance
- ✅ U03-09 DecisionsConfig: all 13 fields with the right types and defaults, and no `deciders` field (R-76). Cross-field rules are present: the version pattern (checked through `QuestionSet`), no duplicates in the chain, `primary_decider` not in the chain, band ascending, weights sum 1 ± 1e-6, `k_min <= k_max`. It also checks every other §9 row that needs no external state: ≤ 64 questions, unique ids, `assign_min_sim < full_sim`, `spot_check_min <= spot_check_max`, `per_round <= candidates <= pool`, the abbreviation key pattern and no expansion equal to a key, and `use_decider` needs `change_caused_pair`.
- ✅ U03-10 QuestionConfig: it has the `Question` fields except `fingerprint`, plus `acceptance` and `primary_decider`. The `Question` rules are applied through `_check_question_fields`. I checked the field list against `herness/core/types/decisions.py:49`.
- ✅ U03-11 AcceptanceCriteria / AcceptanceDefaults: 6 fractions in [0, 1] and `max_mae` in [0, 3]. At least one criterion must be set. `bool_` has the alias `bool`. The three per-type defaults match §9 exactly.
- ✅ U03-150 DecidersSettings: every `deciders.*` key, type and default in §9 is present. The secret-ref pattern matches the §9 text exactly and is declared locally. OpenJev accepts only the hosts 127.0.0.1 and localhost. Jev requires https. Userinfo is refused on both.
- ✅ U03-151 check_decider_refs: parameters are positional-only and it returns `list[dict[str,str]]`. It gives `error` for jev and `warn` for openjev at all three paths (`decisions.escalation_chain`, `decisions.primary_decider`, `decisions.questions[i].primary_decider`). The `# T10-12:` registration comment carries the exact lambda.
- ✅ The §9 keys, defaults and ranges all match. I checked every row. The extra bounds (for example `ge=0` on spot_check_min/max, `ge=1` on k_min/k_max/proto_per, weights in [0, 1]) only tighten the rules and do not conflict with them.
- ✅ Every model inherits `_Model`, so every model gets `extra="forbid"`, `strict=True` and `frozen=True`, plus `allow_inf_nan=False` and `hide_input_in_errors=True`.
- ✅ settings.py imports only stdlib, pydantic and `herness.core.types`. The import-linter contracts were updated: the new top layer, core-closed, settings-leaves, enrich-settings-light, and store-no-upward.
- ✅ The tests map to UT03-08 (×5, including the shipped files), UT03-09 (×26, covering every case the UT03-09 row lists) and ST03-17 (×8). Every name and docstring carries its ID, and every file has `pytestmark = unit`.
- ⚠️ Cannot verify from diff:
  - `herness config validate --offline` and `ConfigError` conversion: deferred to T10 by ruling.
  - §9 rules that need other specs or run-time state: `embedding.path` and `laya.current_file` under the data root and existing, jev needing an egress profile, spec 05 roles existing, and the laya.fast parity check. The report records all of them as deferred. T03-03 and T10 must pick them up.
  - Coverage of 100 % and the gate results are taken from the report. I did not re-run the suite.

Focused check I ran (risk: URL bypass). I probed OpenJev `base_url` in the worktree.
- Rejected: `127.0.0.1.evil.com`, `localhost@evil.com`, `127.0.0.1\@evil.com`, `[::1]`, `127.0.0.1\t.evil.com` (tab stripped by urlsplit, host then non-loopback), `0x7f000001`, `127.1`, `localhost.`, port 99999, `http:127.0.0.1`, `//127.0.0.1`.
- Accepted: `LOCALHOST`, ` http://127.0.0.1:8100` with a leading space, and the same URL with a trailing `\n` (see Minor).
- Jev: `HTTPS://…` is accepted because the scheme is lowercased, which is fine. `https://` with no host is rejected.
- Question error messages name keys and patterns, not input values.

### Strengths
- `hide_input_in_errors=True` keeps a mistyped secret out of `ValidationError` text, and a test asserts it (`test_ut03_09_deciders_rules_reject`).
- The URL check uses `hostname` membership, not a prefix or substring match, so it holds up against the usual spoofing tricks.
- The `_rules()` hook keeps cross-field checks short and uniform.
- Question rules are reused by validating a real `Question`, so the rules cannot drift from U03-02.
- UT03-08 checks both ways that the YAML values equal the model defaults: `cfg == minimal` and `DecidersSettings() == design`.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. `herness/enrich/settings.py:385` — The message reads `"{path} names {name} but deciders.{name}.enabled is false"`, so it echoes the value at `path` (`jev` / `openjev`). U03-151 says messages name keys, never values. The literal is harmless, but keys-only wording would meet the letter of the spec, for example `"{path} requires deciders.{name}.enabled"`.
2. `herness/enrich/settings.py:306-313, 336-338` — The URL is validated on the urlsplit-normalised form but stored raw. Leading spaces and trailing CR/LF/TAB pass. The host still resolves as loopback, and the egress transport is the second guard (R-06), so this is not exploitable. It could still break the client at run time. Consider rejecting surrounding whitespace, for example with `StringConstraints(pattern=r"^\S+$")`.
3. `herness/enrich/settings.py:308` — `_ = parts.port` raises a bare `"Port out of range 0-65535"`, and the message does not name `openjev.base_url` / `jev.base_url`. Wrap it so the key is named, as the other rules do.
4. `herness/enrich/settings.py:293-296` — `question_set_version` is checked by building `QuestionSet(version=…, questions=())`. If `QuestionSet` ever gains an unrelated rule, such as a non-empty question list, every version will be reported as a bad pattern. The check is correct today but brittle.
5. `herness/enrich/settings.py:109, 272` — The models are frozen, but `options` and `abbreviations` are plain `dict`s, which can still be mutated in place and make the models unhashable. The same pattern exists in `Question` (T03-01), so this is consistent, and it is noted only against the "Invariants: Frozen" wording.
6. `herness/enrich/settings.py:104-118` — `QuestionConfig` redeclares the `Question` fields rather than deriving them. Validating through `Question` stops the rules drifting, but a new `Question` field would be silently absent here until someone adds it. A test comparing `Question.model_fields - {"fingerprint"}` with `QuestionConfig.model_fields - {acceptance, primary_decider}` would catch that cheaply.
7. `herness/enrich/settings.py` is 393 lines against a §2 budget of 320. This is accepted by program ruling and listed only for the record.

### Assessment
**Task quality:** Approved
**Reasoning:** Every §9 key, default and rule within this card's scope is present and exact. The strict, frozen, forbid-extra config is uniform, the U03-09/U03-150/U03-151 validators behave as specified, and the URL and secret-ref checks hold up against adversarial inputs. The remaining items are wording and hardening polish.
