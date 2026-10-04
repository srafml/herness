# T11-21 report: Script model and matching

Status: DONE_WITH_CONCERNS (minor, see Concerns)
Commit: 87c925b feat(eval): add LLM script model and ScriptBook (T11-21)
Worktree: D:\herness\.claude\worktrees\agent-a55fb4049cd1a2901

## Files
- herness/eval/scripted.py (new, 290 lines; budget 300, hard limit 400)
- tests/unit/eval/test_scripted.py (new, 30 test cases)
No pyproject change needed (herness.eval already in the import-linter layers and mypy files).

## Implemented
- ScriptMatch(role="*", model_role="*", dedup_key="*"); ToolCallSpec(name non-empty, arguments: dict = {});
  ScriptTurn (exactly one of tool_calls (non-empty list) / final); ScriptFault(at>=0, kind Literal[5], count 1..100);
  LLMScript(match (default all-*), turns 1..200, faults=[], source). All models extra="forbid", strict, frozen (same base as settings.py).
- final validation: exactly one key; `text` -> str; `output` or any tool name -> mapping. Extra keys inside the mapping
  (e.g. numbers_from / numbers used later by render_turn U11-39) are left free-form.
- load_scripts(path): file or directory (non-recursive, *.yaml / *.yml, sorted by file name); a file is one mapping or a
  list of mappings; source = "<file name>#<index>" (any `source` key in the YAML is overwritten by the loader).
  Errors -> ConfigError whose message starts with "<file>#<index>" and whose context carries file and index;
  pydantic detail goes to `hint` (same pattern as load_eval_config). Missing path, non-UTF-8, YAML errors, empty
  document (reported as #0 "must be a mapping"), non-mapping entries, >500 scripts in total -> ConfigError.
- ScriptBook(scripts): next_action(role, model_role, dedup_key, *, prompt_hash="") -> ScriptAction (frozen dataclass:
  kind, script, turn_index, call_index, fault); calls(role, dedup_key). Algorithm as spec U11-38, one threading.Lock
  around match + counters. Fault action carries the unchanged turn pointer as turn_index. An unmatched call does NOT
  advance the counter (call_index on that ScriptMismatch = calls so far); an exhausted call does advance it (step 2 precedes step 4).
- ScriptMismatch(FatalError) with attributes role, model_role, dedup_key, call_index, prompt_hash, registered in
  _extra_attrs so __reduce__ rebuilds them. Kept in scripted.py per the §2 module map (`# noqa: N818`, name fixed by spec).

## Design choices
- prompt_hash: next_action takes a keyword-only optional `prompt_hash: str = ""` and passes it onto ScriptMismatch
  (default ""). Drivers (U11-40, U11-42) compute the hash and pass it; scripted.py imports no T05 types.
- Alias guard: `_parse` builds a yaml.SafeLoader, composes the node graph (get_single_node), then walks it iteratively
  with memoised subtree sizes; an expanded size > MAX_EXPANDED_NODES (100_000) or a cyclic anchor (`&a [*a]`) raises
  before construct_document, so no Python object explosion happens. Same safe constructor as yaml.safe_load.
  RecursionError from very deep nesting is caught -> ConfigError. Size cap (256 KB, st_size) is checked before reading.
  100_000 nodes is far above any legitimate script (200 turns x a few dozen nodes); note a plain alias-free 256 KB file
  could in theory exceed it (e.g. ~130K-element flow list) - that is rejected too, acceptable for scripts.
- ToolCallSpec.arguments defaults to {} (spec lists `arguments: dict` without saying required).
- Empty directory -> empty ScriptBook (every call raises ScriptMismatch); spec silent.

## Tests (tests/unit/eval/test_scripted.py, pytestmark = unit)
- UT11-61: order by file name then index; single-file path + all turn forms; invalid two-key turn names a.yaml#1;
  9 malformed turn shapes; 6 bound violations; non-mapping/empty/broken/non-UTF-8/missing path; 500-script cap.
- UT11-62: org:team:*:ops script matches two keys with independent counters, catch-all fallback; role/model_role
  filtering + ScriptMismatch attributes incl. prompt_hash; __reduce__ rebuild (pickle is banned by TID251); 8-thread counter test.
- UT11-63: fault at 1 count 2, 3 turns: kinds turn,fault,fault,turn,turn; turns 0,1,2 at calls 0,3,4; call 5 "script exhausted".
- ST11-12 (script part), each asserting ConfigError within 2 s: 300 KB file; 10^6-alias file (3 MB, size cap);
  sub-1 KB billion-laughs (expansion guard); cyclic anchor + 100K-deep nesting; plus a positive case that modest anchors still load.

## RED / GREEN
- RED: `uv run pytest tests/unit/eval/test_scripted.py` -> ImportError: cannot import name 'scripted' from 'herness.eval'.
- GREEN: 30 passed in 1.61s; coverage herness/eval/scripted.py 191 stmts, 52 branches, 100% line and branch.

## Gates
- ruff format --check: 131 files already formatted; ruff check: All checks passed
- mypy (strict): Success: no issues found in 59 source files
- lint-imports: Contracts: 10 kept, 0 broken
- tools.check_module_size: exit 0; tools.check_type_ownership: exit 0
- pytest -m "(unit or integration) and not slow": 1255 passed, 5 deselected, 1 xfailed (pre-existing IT00-02 xfail)

## Concerns
- Deep-nesting case costs ~0.45 s (1.2 s under coverage) because of PyYAML's recursive composer hitting RecursionError;
  within the 2 s bound but the least headroom of the ST11-12 cases.
- MAX_EXPANDED_NODES (100_000) is an implementation constant not named in the spec (TH11-08 lists only size/count caps).
- ScriptMismatch lives in herness/eval/scripted.py (module map) rather than herness/core/errors.py.
