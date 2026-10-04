# T11-24 report (Stub decider) — builder report, saved by sub-controller w20-s11
Status: DONE_WITH_CONCERNS · commits a1364df (wip) + 70f0093 feat(tests): T11-24 stub decider server · base 9c8f34a · hooks passed (incl. pytest-unit), no SKIP
(The harness refused the builder's write of this file; content below is its hand-back in substance.)

## Files
- tests/support/stub_decider.py 238/300 (no sibling, §2 row unchanged). StubDeciderServer(StubHTTPServer); StubFault re-exported from tests.support.stub_http (already had the U11-46 signature). Public: MODEL_ID, MODELS_PATH, SYSTEMONE_PATH, content_hash_of, `calls` property.
- tests/unit/support/test_stub_decider.py 347 lines, 24 tests, pytestmark unit. No herness/ change, no conftest/plugin change; nothing under herness/ imports tests/.

## Tests
UT11-75: oracle answer == truth with p = 1-noise (0, 0.1, 0.3); wire fields (noul=P(true); choice/score noise/(K-1); confidence 1-H/lnK; score = sum level*p; legend = criteria); oracle w/o label == hash byte-for-byte; registered OpenJevDecider (register_deciders + registry.get("decider","openjev")) over real loopback_http_client with jev_env fixture: health ok, decide() returns truth at p=0.9; GET /v1/models + 404; >1 MB -> 413, exactly 1 MB served; 7 malformed bodies -> 400; preconditions raise before bind (oracle w/o labels incl. default ctor, noise -0.01/1.0/1.5/NaN, unknown mode, missing parquet); 127.0.0.1-only bind (TH11-10) incl. subclass bind_host 0.0.0.0 refused; service file {"openjev": base_url}.
UT11-76: hash mode, 429@0 (Retry-After: 1) + 529@1, 4 calls -> calls 2/3 byte-identical and valid; independent hash formula check; determinism across instances (hash, oracle+noise); malformed_json -> 200 body `{"answers": ` refused by load_wire_body; count=2 covers two calls; kill@1 -> call 1 TransportError, later connects refused.
All responses validated via jev_wire.load_wire_body + parse_wire_answers.
Checks: ruff format/check clean; mypy --strict --explicit-package-bases on both files 0 (tests/ not in mypy files); lint-imports 13 kept 0 broken; check_module_size 0; RED first (ModuleNotFoundError); tests/unit/support 69 passed with --require-test-ids.

## Builder rulings / deviations
- Fault index = 0-based count of POST /v1/systemone requests (incl. 413/400-rejected); GET /v1/models, 404, control route take none; faults checked before body validation.
- kill: handler calls kill() (closes listener + connections); the call gets no reply.
- Oracle falls back to hash when no label or label not among the options.
- Body limit = jev_wire.MAX_BODY_BYTES; malformed -> 400.
- "choice" carries the chosen label; response "model" constant "openjev-latest" (request model not echoed).
- Truth labels via pyarrow (content_hash, question, answer) into a read-only map; last duplicate wins; private _load_labels (no clash with tests/support/truth.load_truth_labels).
- Counter has its own lock, separate from the base lock used by kill().

## Concerns / spec notes
- Test imports jev_env fixture from tests/unit/enrich/_openjev_support.py (as test_openjev_decider.py does); a move breaks it.
- probabilities = label-keyed dict until spec 03 Q1; score keyed "0".."3".
- Oracle bool with noise >= 0.5 flips the parsed answer (U11-46 formula).
- Build fixture must send as state the redacted text whose sha256[:32] matches truth content_hash (= herness.enrich.text.content_hash).
- openjev/jev_hosted tests not re-pointed to the stub (group ruling).
