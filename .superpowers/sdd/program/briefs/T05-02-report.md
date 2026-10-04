# T05-02 build report (transcribed by the sub-controller)
Status: DONE_WITH_CONCERNS. Commit d0d95f1 feat(core): add harness tool, evidence and verification types (T05-02)
Tests:
- Card tests UT05_04..07, PT05_01, UT05_47 and UT05_69: 47 passed.
- Wider suite: 176 passed. The 1 failure is UT00-48, expected until T05-03.
- ruff, format, mypy (16 files) and lint-imports are clean.
- Coverage: evidence 100%, tooling 99%.
Remaining ownership output: OWN010 missing AgentResult, LoopCheckpoint, LoopLimits, LoopSignal and LoopState, plus 2 x OWN031.
Files, lines against budget:
- tooling.py 236/300
- evidence.py 189/200
- harness/__init__.py 29/60
- types/__init__.py 75/150
- tests/support/harness_fakes.py 113 (FakeOps, FakeLedger, FakeVectors, RecordingTracer)
- test_types_harness_tooling.py 259; test_types_harness_evidence.py 270
Choices where the spec was silent:
1. TOOL_CONTENT_MAX_CHARS is private (_TOOL_CONTENT_MAX_CHARS) to avoid OWN011.
2. All protocols are @runtime_checkable; attribute members are read-only properties.
3. AsyncTool.__call__ is async.
4. TraceEmitter.emit keeps the parameter name `type`, with noqa A002.
5. A ToolContext build mismatch raises ConfigError directly from the validator.
6. UTC fields: a naive datetime is rejected; an aware one is converted to UTC.
7. ToolResult: content is truncated to 11,999 chars + "…" and sets truncated; from_error caps the message at 2,000 and the hint at 500.
8. SqlLimits: strict ints with ge=1; timeout_s > 0.
9. NumberRef rejects bools; row_key floats must be finite (found by PT05-01).
10. Evidence recomputes query_id with ids.query_id.
11. PT05-01 uses a plain @given, because no "commit" profile exists.
Concerns:
- harness/__init__.py uses a compact layout (fmt: off, noqa I001) to fit the 60-line budget; the standard layout would be about 90 lines after T05-03.
- The impl 00 types/__init__.py budget will get tight later.
