# T11-24 review (Stub decider): verify agent

### Spec Compliance
- ✅ Spec compliant. Each U11-46 item was checked against tests/support/stub_decider.py (head 70f0093):
  - ✅ Kind: subclass of StubHTTPServer (T11-23), not a Decider and not in herness.core.registry (group ruling 1). The static name "openjev" and the T03-11 Protocol are covered by test_ut11_75_openjev_decider_end_to_end, which runs registry.get("decider","openjev") against the stub over real loopback HTTP.
  - ✅ Signature: `(mode="oracle", truth_labels=None, noise=0.0, *, port=0, faults=(), service_name="openjev")` (:153-162). StubFault is re-exported from stub_http and has the (at, kind, count=1) shape.
  - ✅ Preconditions: oracle without truth_labels, noise outside [0,1) (NaN included) and an unknown mode all raise before the socket binds (:163-171).
  - ✅ GET /v1/models returns `{"object":"list","data":[{"id":"openjev-latest"}]}` (:190-191).
  - ✅ The POST /v1/systemone body is capped at 1 MB: over 1 MB returns 413, and a body of exactly 1 MB is served (:200-201, jev_wire.MAX_BODY_BYTES).
  - ✅ content_hash is sha256(state utf-8).hexdigest()[:32] (:54-56).
  - ✅ K options: noul true/false, choice uses the keys of criteria, score uses "0".."3" (:66-79).
  - ✅ Hash mode: sha256(f"{content_hash}:{qid}"); index = int(h[0:8],16) % K; top = 0.5 + 0.5*int(h[8:16],16)/0xFFFFFFFF; the rest is spread evenly (:82-93). An independent test recomputes this (test_ut11_76_hash_answer_follows_the_formula).
  - ✅ Oracle: the truth answer gets 1-noise and each other label gets noise/(K-1) (:88-93, :233-238). With no label, oracle falls back to hash mode, and the test checks the two responses are byte-identical.
  - ✅ Response shape: `{"model":"openjev-latest","answers":...,"usage":{"input_tokens":len(state)//4,"output_tokens":0}}` (:230-231).
  - ✅ noul is `{"noul": P(true)}`. choice is `{choice, probabilities (dict), confidence = 1-H(p)/ln K}`. score is `{score = sum level*p, legend = criteria list, probabilities "0".."3", confidence}` (:104-123). probabilities is a dict, as required until spec 03 Q1 is settled.
  - ✅ Faults use the POST request index: 429 with Retry-After: 1, 529, malformed_json (HTTP 200 with body `{"answers": `), and kill (closes the listener and the connections; the call gets no reply) (:207-215). count is handled by StubFault.covers.
  - ✅ Labels are read once through pyarrow into an immutable MappingProxyType keyed by (content_hash, question) (:59-63, :175-177). A separate lock guards the call counter (:178-179, :194-196).
  - ✅ Determinism: answers are pure functions of (content_hash, qid), and the test checks byte-identical output across instances for hash mode and for oracle with noise.
  - ✅ TH11-10: the stub binds 127.0.0.1 only, and a subclass that sets bind_host 0.0.0.0 is refused. No other network, no GPU.
  - ✅ The diff touches only the two tests/ files. A grep of herness/ for `from tests` or `import tests` finds nothing.
  - ✅ UT11-75 checks everything its row lists: the response validates through load_wire_body and parse_wire_answers (U03-50 is the fixture schema under the group ruling), the answer equals the truth, and the probability is 1-noise for noise 0, 0.1 and 0.3.
  - ✅ UT11-76 checks everything its row lists: hash mode with 429@0 and 529@1 over 4 calls gives [429,529,200,200], Retry-After is "1", and calls 2 and 3 return byte-identical bodies that parse.
  - ✅ Test IDs: every function is named test_ut11_75_/test_ut11_76_, every docstring starts with the ID, and `pytestmark = pytest.mark.unit` is set. `--require-test-ids` passes.
  - ✅ Module budget: 238/300.
- ⚠️ Cannot verify from the diff: the "spec 03 §3.3 fixture" acceptance against recorded fixtures, because tests/fixtures/openjev does not exist. Ruling 2 accepts validating through parse_wire_answers instead. The test should be re-pointed when the recorded fixtures land.

Verifier runs (worktree at 70f0093):
- `PYTHONUTF8=1 uv run pytest tests/unit/support/test_stub_decider.py -q -p no:logging --require-test-ids`: 24 passed in 11.6 s
- `uv run ruff check` on both files: all checks passed. `ruff format --check`: already formatted.
- `uv run mypy --strict --explicit-package-bases` on both files: no issues.
- The only warning is uv's VIRTUAL_ENV mismatch notice, which comes from the environment, not the code.

### Strengths
- The wire output is checked through the real U03-50 parser and through the registered OpenJevDecider end to end, not only against hand-written dicts.
- There is an independent formula check for hash mode, and oracle-with-noise output is checked byte-for-byte across instances.
- Faults are resolved before the body is validated, so fault indexes stay stable whatever the payload. GET routes do not consume an index, which the tests check (`calls == 0`).
- Edge cases are handled without dividing by zero: K=1 (single-option choice), noise=0, and top=1.0 (zero probabilities are skipped in the entropy).

### Deviations in the builder's report (judged)
- Fault index counts every POST /v1/systemone, including 413 and 400 rejections, and faults are checked first. Accepted: the spec says "by request index" and this is the simplest reading.
- Oracle falls back to hash mode when the truth label is not among the question's options. Accepted, see Minor 1.
- The response always sends model "openjev-latest" and does not echo the request's model. Accepted: this is what U11-46 specifies.
- When label rows are duplicated, the last one wins silently. Accepted, see Minor 2.
- Bool oracle with noise >= 0.5 flips the parsed answer. This follows the U11-46 formula as written, not a defect. Worth a note for T11-25 and the build-fixture users, who should keep noise below 0.5.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. tests/support/stub_decider.py:235-238: in oracle mode, a label that exists but is not among the question's options (for example a vocabulary drift between truth_labels.parquet and the question set) silently falls back to hash answers. The build fixture would then get plausible but wrong answers with no signal. Consider counting these (for example a `mismatched_labels` counter) or logging them once, so a truth/question drift shows up.
2. tests/support/stub_decider.py:59-63: duplicate (content_hash, question) rows silently keep the last answer. If two different answers conflict, that points to a synth bug. Raising, or at least documenting this in the docstring, would be safer.
3. tests/support/stub_decider.py:149-150 (docstring): it says "every one takes an index, even a rejected body". Bodies over 8 MB are rejected by the base handler (_stub_http_core.py:133) before respond() runs, so they take no index. This is harmless, but the docstring is slightly inaccurate.
4. tests/unit/support/test_stub_decider.py:28,45: the test imports the fixture `jev_env` from another test module (tests/unit/enrich/_openjev_support.py) and adds `__all__ = ["jev_env"]` to keep the import. If that module moves, this breaks. A shared fixture belongs in a conftest or tests/support. The builder reported this; it does not block.
5. tests/support/stub_decider.py:108-109: in hash mode, if int(h[8:16]) == 0 then top = 0.5 exactly. For noul with index 1 ("false"), P(true) = 0.5 then parses as "true" (parse_wire_answers uses p >= 0.5). The probability is about 2^-32, so this can be ignored. It is noted only for completeness.

### Assessment
**Task quality:** Approved
**Reasoning:** Every U11-46 value and behaviour matches the spec verbatim. UT11-75 and UT11-76 check what their rows require through the real wire parser and the registered decider. Tests, ruff and mypy --strict are clean when run independently. The remaining items are minor diagnostics and robustness polish.
