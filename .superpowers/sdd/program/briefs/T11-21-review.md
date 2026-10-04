# T11-21 review: Script model and matching (herness/eval/scripted.py)

Reviewed: worktree agent-a55fb4049cd1a2901, base e3b4881, head 87c925b.

**Verdict: Approved** (no Critical or Important findings; three Minor findings listed below).

### Spec Compliance
- ✅ U11-37 `ScriptMatch(role="*", model_role="*", dedup_key="*")`: scripted.py:45-50.
- ✅ `ScriptTurn`: exactly one of `tool_calls: list[ToolCallSpec]` (`name`, `arguments: dict`) or `final`. `final` has exactly one key: `text` must be a str, and `output` or a tool name must be a mapping. See scripted.py:60-87.
- ✅ `ScriptFault(at>=0, kind Literal[5], count 1..100 default 1)`: scripted.py:90-95.
- ✅ `LLMScript(match, turns 1..200, faults=[], source)`: scripted.py:98-104.
- ✅ Every model uses `extra="forbid"` (also strict and frozen): scripted.py:41-42.
- ✅ `load_scripts(path) -> ScriptBook`. It accepts a file or a directory. Files load in sorted file-name order, and each file's scripts load in document order. A file can hold one mapping or a list. `source="<file>#<index>"`, and the loader overwrites any `source` set in the YAML. See scripted.py:265-297.
- ✅ Size cap of 256 KB, checked before the file is read (scripted.py:254-257). The 500-script cap is checked before validation (scripted.py:293-295).
- ✅ Invalid file raises `ConfigError` whose message names `<file>#<index>` and whose context holds `file` and `index`: scripted.py:266-274.
- ✅ `yaml.safe_load` semantics: SafeLoader compose plus construct_document. The expanded-node budget and the cyclic-alias check run before any Python object is built (scripted.py:215-249). This covers TH11-08.
- ✅ U11-38 `next_action -> ScriptAction(kind, script, turn_index, call_index, fault)` and `calls(role, dedup_key)`: scripted.py:142-204.
- ✅ Matching follows the spec. The first script wins. `role` and `model_role` must be `*` or equal to the call's value, and `dedup_key` uses fnmatchcase. No match raises ScriptMismatch without advancing the counter (step 1 comes before step 2).
- ✅ Counters are keyed by `(role, actual dedup_key)`, and one `threading.Lock` covers the match and the counters.
- ✅ Fault window `at <= call_index < at+count` leaves the turn pointer unchanged. Exhaustion raises `ScriptMismatch("script exhausted")`.
- ✅ `ScriptMismatch(FatalError)` carries role, model_role, dedup_key, call_index and prompt_hash. These are registered in `_extra_attrs`, so `__reduce__` and log fields carry them (core/errors.py:111, 336).
- ✅ UT11-61, UT11-62 (the row missing from the brief, spec line 1668: an analyst script for `org:team:*:ops` plus a catch-all, two keys, independent counters) and UT11-63 (6 calls: faults at calls 1-2, turns 0,1,2 at calls 0,3,4, call 5 exhausted) are all present with exact assertions.
- ✅ ST11-12 script part: a 300 KB file, 10^6 aliases, a sub-1 KB billion-laughs document, a cyclic anchor and deep nesting each raise ConfigError within 2 s.
- ✅ Every test name carries its ID with `_`, every docstring starts with its ID, and `pytestmark = pytest.mark.unit` is set.
- ✅ No personal data in messages. File-level errors put only the exception type name in `hint`.

Verified by the reviewer:
- `pytest tests/unit/eval/test_scripted.py -q -p no:logging` with branch coverage: 30 passed in 1.62 s. scripted.py: 191 statements and 52 branches, 100 % line and 100 % branch.
- `mypy herness/eval/scripted.py`: Success.
- `ruff check` and `ruff format --check`: clean.
- `uv run mypy/ruff` would not run because uv resolved the mid-merge `D:\herness\pyproject.toml` (TOML parse error at line 248). I ran them with `.venv\Scripts\python.exe` instead.

- ⚠️ Cannot verify from diff:
  - `next_action` takes an extra keyword-only argument `prompt_hash: str = ""` that the spec signature does not list. This is how `ScriptMismatch.prompt_hash` gets filled. Whether the drivers (U11-40 and U11-42) pass it can only be checked when those cards land.
  - `ToolCallSpec.arguments` defaults to `{}`. The spec does not say whether the field is required.
  - `MAX_EXPANDED_NODES=100_000` is a constant the implementation added; the spec only lists size and count caps. It is a reasonable way to implement TH11-08.

### Strengths
- The alias-bomb guard walks the node graph iteratively with memoized subtree sizes. It measures the true expanded size of shared DAG nodes. Its cycle check (open nodes that are not yet sized, i.e. ancestors on the current path) is correct. It runs before construction, so no object explosion happens.
- Errors are consistent with the existing `settings.py` and `truth.py` loaders.
- Tests assert real behaviour: order, context keys, counter independence, 8-thread exact counting, exact fault and turn indices, and a positive anchor case.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. scripted.py:274 `hint=str(exc)` puts pydantic's `input_value=...` echoes of the script content into `hint`. Scripts are developer-written synthetic fixtures, and settings.py:96 and truth.py:147 use the same pattern, so this is acceptable. A future stricter reading of global constraint 13 would call for `exc.errors(include_input=False)`.
2. tests/unit/eval/test_scripted.py:214 `rebuild(*args)` fails under `mypy` ("Expected iterable as variadic argument"). Tests are outside `[tool.mypy] files`, so no gate breaks. It would be cleaner to annotate or cast `args` to a tuple.
3. scripted.py:254-259 checks the size with `stat()` and then calls `read_bytes()`, which is a small TOCTOU window. A bounded `read(MAX_SCRIPT_BYTES + 1)` would close it. The risk is negligible for local fixtures.
   - Separately, the deep-nesting ST case costs about 1.2 s under coverage (per the report), which leaves the least headroom against the 2 s bound. Watch it on slow CI runners.

### Assessment
**Task quality:** Approved
**Reasoning:** Every signature, bound, ordering, error contract, counter and fault-window rule matches U11-37 and U11-38. UT11-61, UT11-62 and UT11-63 and the ST11-12 script part pass with 100 % coverage and clean gates. The only findings are three pieces of minor polish.
