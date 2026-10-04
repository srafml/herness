# Review: T10-09 Name directory

Worktree HEAD f79e01f (base e9944b6). Read-only review.

## Spec compliance (U10-38 / U10-39 / UT10-46)

| Item | Result |
|---|---|
| `from_files` step 1: CSV via `csv.DictReader`, UTF-8, ≤200 MiB, missing file → WARNING `redact.directory.missing` + empty directory | ✅ (`herness/core/redact_directory.py:45-60`; note below on encoding) |
| step 2: ≥2-token `display_name` → canonical `normalize_value("PERSON", ...)`; variants `first last` (lower, as written), `last, first`, `last,first` (first = all tokens but last) | ✅ `_add_display_entry` (`redact_directory.py:77-87`) |
| step 3: `alt_names` (`;`-separated) lower-cased with the row's canonical | ✅ `redact_directory.py:139-142`; correctly scoped to the same `display_name ≥2 tokens` conditional as step 2 (nested "for each" reading), so a single-token `display_name`'s alt_names are skipped rather than fabricating a canonical — consistent with the invariant and covered by `test_ut10_46_alt_name_skipped_when_display_name_is_single_token` |
| step 4: `extra_names` (any token count) as their own canonical | ✅ `redact_directory.py:143-146` |
| step 5: `display_names_file` lines processed as in step 2 | ✅ `redact_directory.py:147-148` (reuses `_add_display_entry`) |
| step 6: `pyahocorasick.Automaton` key→canonical, `make_automaton()` | ✅ `redact_directory.py:149`; values store `(key_len, canonical)` tuples rather than bare canonical — a necessary implementation detail (recovers match start from `iter`'s end-index-only API), not a behavioral deviation |
| Invariant: every key is a ≥2-token variant, or a single token from `extra_names` | ✅ enforced structurally (single-token `display_name`/alt entries never reach `add_word` outside `extra_names`); tested |
| `find` step 1: `low = text.lower()`; index map when `len(low) != len(text)` | ✅ `redact_directory.py:157-160`, `_lower_with_map`; tested with Turkish dotted İ |
| `find` step 2: `automaton.iter(low)` | ✅ `redact_directory.py:162` |
| `find` step 3: word-boundary via non-alphanumeric neighbors | ✅ `redact_directory.py:168-169`; tested (embedded-substring rejection) |
| `find` step 4: overlap resolution longest-first, then leftmost | ✅ `_resolve_overlaps` (`redact_directory.py:101-111`); tested for both the length-priority and the tie-break case |
| `size` = distinct canonical names, `variant_count` = distinct automaton keys (ruling) | ✅ `redact_directory.py:150-151` |
| Errors: unreadable `directory_file` → `ConfigError("cannot read directory_file")` | ✅ `redact_directory.py:53-58`; also applied to `display_names_file` reads (spec gives one message for "unreadable file", not two) |
| Security: directory content never logged | ✅ only the fixed-string `redact.directory.missing` event is logged; no file content anywhere in logs |
| U10-39 step 1-2: read existing cache (absent → empty); add stripped names with 2-8 tokens, 3-128 chars | ✅ `redact_directory.py:193-205` |
| U10-39 step 3: atomic write only if the set grew | ✅ `redact_directory.py:206-208`, `_write_atomic` (tempfile + `os.replace`, cleans up temp file on failure) |
| U10-39 step 4: return count added | ✅ `redact_directory.py:212` |
| U10-39 errors: `OSError` → `StoreBusy("display_names write failed")` | ✅ `redact_directory.py:209-211`, wraps read+write |
| ≤200 MiB limit enforced without reading oversized files fully | ✅ reads `MAX_DIRECTORY_BYTES + 1` bytes only (`redact_directory.py:48-49`), boundary-correct (`≤` inclusive) |
| UT10-46 coverage: variants, word-boundary, single-token-only-via-extra_names, display file sorted+count | ✅ 17 test functions, all IDed `test_ut10_46_...`, `pytestmark = pytest.mark.unit`; report claims 100% line/branch coverage |
| Acceptance: 100k-name build < 5s | ✅ `@pytest.mark.slow` test asserts `< 5.0`; report measured ~0.90s |
| Module budget 220 (ENG hard limit 400) | ✅ 204 lines |
| Import-linter: new L0 module added to "core base is closed" forbidden_modules | ✅ `pyproject.toml:248` |
| mypy override for untyped `ahocorasick` extension | ✅ `pyproject.toml:226-229`; `pyahocorasick` already a declared dependency (`pyproject.toml:36`, `uv.lock`), not newly added here |
| Ruling: `tests/unit/core/` location | ✅ `tests/unit/core/test_redact_directory.py` |
| Ruling: `herness/core/config.py` untouched | ✅ not present in diff |

## ⚠️ Cannot verify from diff

- Commit message trailer (`Co-Authored-By: ...`) — the diff's git-log line only shows the subject `feat(core): add redaction name directory (T10-09)`; full commit body not shown.

## Minor

- `redact_directory.py:59,68`: CSV and display-names files are decoded with `"utf-8-sig"` rather than the spec's literal `"UTF-8"`. This is a permissive superset (strips an optional BOM) and not a correctness problem, but it is an unflagged, undocumented divergence from the exact encoding named in the algorithm text.
- `redact_directory.py:139-142`: `automaton.add_word` silently lets a later duplicate key overwrite an earlier key's canonical (e.g., two different people sharing an alt-name spelling). The spec doesn't address collision handling and no test exercises it; low real-world likelihood, noted for awareness only.

## Assessment

No Critical or Important findings. The implementation is a faithful, literal reading of U10-38/U10-39, correctly applies the size/variant_count ruling, stays well under the line budget, keeps directory content out of logs, and the reported test suite (17 unit tests, 100% line/branch coverage, acceptance timing ~0.90s) plausibly backs the spec-compliance claims above by inspection of the diff.

**Verdict: Approved**
