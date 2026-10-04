# T05-11 Tracer: re-review, fix round 1 (verify agent)

**Verdict: Approved.** I-1, I-2 and I-3 are closed, along with M-1, M-2, M-3, M-4 and M-6. The fix round introduced no regressions. Two new Minor items remain open; neither blocks approval.

Scope: commit 6ad6b2e on top of d52fd94. It touches `herness/harness/_trace_clean.py` (new, 105 lines), `herness/harness/tracing.py` (341/360 lines) and `tests/unit/harness/test_tracing.py`. M-5 is parked. Controller rulings (not treated as defects): plain field strings are scrubbed but not redacted, and an empty line may follow a zero-byte failed write.

## Evidence I ran myself (read-only)
- **Card tests:** unit, ST05-14 tracer, FT05-01, ST05-19 and BT05-11 with `--require-test-ids` give 35 passed.
- **Coverage:** `_trace_clean.py` 100 % line and branch; `tracing.py` 99 % line, 1 partial branch (missing 122-123 and 152: the flush OSError and the late-race drop).
- **mypy (configured scope `herness`, `tools`):** clean on both modules.
  - mypy reports 7 errors in `tests/unit/harness/test_tracing.py` if you pass that file to it explicitly. Examples: `dict[str, float]` for the Literal-keyed rates, and `**dict[str, object]`. Tests are outside the configured scope, so this is not a gate. Line 58 already existed at 0551118.
- **ruff:** `ruff check` passes, and `ruff format --check` reports the files already formatted.
- **Layering and size:** lint-imports keeps all 13 contracts. `tools.check_module_size` exits 0.
- **Repro scripts** (next to this file):
  - `repro.py` and `repro2.py` from round 0;
  - `repro3.py` for the variants requested in this round;
  - `repro4.py` for N-1.

## Status of each finding
| Finding | Status | Evidence |
|---|---|---|
| I-1 payload keys | Closed | An email key at depth 4 becomes `[EMAIL_…]`. Known-secret keys become `***` in both payload and fields. A non-str (tuple) key is turned to str, then redacted. |
| I-2 cut before scrub | Closed | A secret straddling the cut at 20,000, straddling it with no spaces, straddling it at 200,000, straddling the 64 KiB scrub limit (payload and field), or sitting mid-way past 64 KiB inside a longer string: never a prefix. A card run embedded in a known secret is also masked (`value *** end`). |
| I-3 opaque in fields | Closed | Removed for a plain `opaque=` field, a `ReasoningPart` field, `{"opaque"}` 20 levels deep, and a model inside a payload list. No `"opaque"` in any line. |
| M-1 datetime ISO Z | Closed | A naive datetime gives `…05.000000Z` (read as UTC). A +05:00 datetime is converted to `…22:04:05.000000Z`. A payload datetime also gives Z. |
| M-2 deep field secret | Closed | A secret 30 levels deep in a field becomes `***`. The walk refuses depth > 64: `ConfigError` for fields, `payload_dropped` for a payload. |
| M-3 unserializable fields | Closed | A circular field, or one whose `__str__` raises, gives `ConfigError("trace fields cannot be serialized")`. The same value as a payload gives `payload_dropped` plus a warning. |
| M-4 torn line | Closed | `tracing.py:94-106`: after a write error with an open handle, the next line starts with `\n` (the `torn` flag). An open failure does not set it. The test `test_ut05_46_torn_write_does_not_corrupt_the_next_line` covers this. |
| M-6 run_id on the warning | Closed | `tracing.py:276-278`. |
| M-5 emit/close race | Parked | Not re-checked, as instructed. |

## Regressions
None found:
- NaN is now `"nan"` instead of `"NaN"`. This is still valid JSON, and the test was updated.
- bytes now come out as `"b'…'"` (the str of the bytes) instead of the decoded text. The content is still redacted and scrubbed. This is cosmetic.
- Enum values are unwrapped; Decimal becomes a str.
- Per the builder's numbers, throughput is still far above target: enqueue 0.011 ms against a 0.2 ms target, writer 46.7k events/s against 1,000 events/s.

## New findings (both Minor)

**N-1: in a field string longer than 64 KiB, a pattern-detected credential that crosses the 64 KiB boundary leaks its prefix.** It affects fields only.
- Where: `herness/harness/_trace_clean.py:37-39`. The tail dropped past the scrub window is `max(len(known_values()))`, which covers known secret values only. It ignores CREDENTIAL and URL_TOKEN spans that the detectors find by pattern (for example `ghp_…` with 36 or more characters, or `xox…` up to 200). When the process has no known values, the tail is 0.
- Repro (`repro4.py`, no known values):
  ```python
  tr.emit("retry", note="x"*(64*1024-30) + " " + GHP + " tail")
  ```
  The line contains `ghp_A1b2C3d4E5f6G7h8I9j0K1l2M`, a 29-character fragment of the token.
- Payloads are not affected, because `redact_text` masks CREDENTIAL and URL_TOKEN on the whole string (up to 4,000,000 characters) before the scrub.
- Field strings over 64 KiB are not expected in practice, so this is Minor.
- Fix: widen the dropped tail to at least the longest detector span (about 260 characters), for example `max(longest_known, 512)`, or hard-cut any over-limit field string to `_SCRUB_LIMIT - 512` after the scrub.

**N-2: a payload string longer than 64 KiB is cut to about 64 KiB, even when `max_payload_chars` is larger.**
- Where: `_trace_clean.py:37-39` together with `:103`. The scrubber returns only the first 64 KiB, so the effective limit is `min(max_payload_chars, 64 KiB − tail)`.
- This is safe and does not affect the 20,000 default. But `max_payload_chars` above 65,536 is silently ineffective. Document it in `TraceSettings`, or cap the setting's validator at 64 KiB.

## Other notes
- `_trace_clean.py` imports only `herness.core` modules and pydantic, which is correct for L4. It is private (leading underscore) and within the 150-line ruling.
- **Key collisions:** two keys that redact to the same token keep only the last value. This loses data but leaks nothing, and is acceptable.
- **New tests:** 7 regression tests carry UT05-43, UT05-45 and UT05-46 IDs and assert concrete outputs, not just "no exception".

## Assessment
**Task quality:** Approved
**Reasoning:** All Important findings and M-1, M-2, M-3, M-4 and M-6 are closed. I could not break any of them with the requested variants (keys at depth, straddling cuts at 20k, 200k and 64 KiB, opaque at depth, deep nesting, non-string leaves). N-1 and N-2 are narrow Minor edges in the field path over 64 KiB and do not block the card.
