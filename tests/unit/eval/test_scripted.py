"""Tests for herness.eval.scripted: LLM script format, loader and ScriptBook (T11-21).

UT11-61 covers `load_scripts` ordering and validation errors, UT11-62 matching and
per-key counters, UT11-63 fault windows and exhaustion, and ST11-12 (script part) the
size cap and the alias-bomb guard (TH11-08).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from herness.core.errors import ConfigError, FatalError
from herness.eval import scripted as sc

pytestmark = pytest.mark.unit

_FINAL = {"final": {"text": "done"}}


def _script(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"turns": [_FINAL]}
    base.update(over)
    return base


def _write(path: Path, doc: object) -> Path:
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


def _book(*scripts: dict[str, Any]) -> sc.ScriptBook:
    return sc.ScriptBook(
        [sc.LLMScript.model_validate({**s, "source": f"t#{i}"}) for i, s in enumerate(scripts)]
    )


# ---------------------------------------------------------------- UT11-61 load_scripts


def test_ut11_61_order_by_file_name_then_index(tmp_path: Path) -> None:
    """UT11-61 a list file and a single file load in file-name then document order."""
    _write(tmp_path / "b_single.yaml", _script(match={"role": "chat"}))
    _write(
        tmp_path / "a_list.yml",
        [_script(match={"role": "analyst"}), _script(match={"role": "planner"})],
    )
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    book = sc.load_scripts(tmp_path)
    assert [s.source for s in book.scripts] == ["a_list.yml#0", "a_list.yml#1", "b_single.yaml#0"]
    assert [s.match.role for s in book.scripts] == ["analyst", "planner", "chat"]
    assert book.scripts[0].match.model_role == "*"
    assert book.scripts[0].match.dedup_key == "*"


def test_ut11_61_single_file_path_and_turn_forms(tmp_path: Path) -> None:
    """UT11-61 a single file loads; tool_calls and every final form validate."""
    doc = _script(
        turns=[
            {"tool_calls": [{"name": "run_sql", "arguments": {"sql": "select 1"}}]},
            {"tool_calls": [{"name": "list_tables"}]},
            {"final": {"output": {"answer": 1}}},
            {"final": {"post_finding": {"claim": "x"}}},
            _FINAL,
        ],
        faults=[{"at": 0, "kind": "http_429"}],
        source="ignored-by-loader",
    )
    book = sc.load_scripts(_write(tmp_path / "one.yaml", doc))
    (script,) = book.scripts
    assert script.source == "one.yaml#0"
    assert script.turns[0].tool_calls is not None
    assert script.turns[0].tool_calls[0].arguments == {"sql": "select 1"}
    assert script.turns[1].tool_calls is not None
    assert script.turns[1].tool_calls[0].arguments == {}
    assert script.turns[2].final == {"output": {"answer": 1}}
    assert script.faults[0].count == 1


def test_ut11_61_invalid_turn_with_two_keys_names_file_and_index(tmp_path: Path) -> None:
    """UT11-61 a turn with both tool_calls and final raises ConfigError naming file#index."""
    bad = {"turns": [{"tool_calls": [{"name": "t", "arguments": {}}], "final": {"text": "x"}}]}
    _write(tmp_path / "a.yaml", [_script(), bad])
    with pytest.raises(ConfigError, match=r"a\.yaml#1") as info:
        sc.load_scripts(tmp_path)
    assert info.value.context["file"] == "a.yaml"
    assert info.value.context["index"] == 1


@pytest.mark.parametrize(
    "turn",
    [
        {"final": {"text": "x", "output": {}}},
        {"final": {}},
        {"final": {"output": "not a mapping"}},
        {"final": {"text": {"not": "a string"}}},
        {"final": {"post_finding": "not a mapping"}},
        {"tool_calls": []},
        {},
        {"tool_calls": [{"name": "", "arguments": {}}]},
        {"final": {"text": "x"}, "extra": 1},
    ],
)
def test_ut11_61_invalid_turn_shapes_rejected(tmp_path: Path, turn: dict[str, Any]) -> None:
    """UT11-61 each malformed turn is rejected with ConfigError naming file and index."""
    _write(tmp_path / "s.yaml", {"turns": [turn]})
    with pytest.raises(ConfigError, match=r"s\.yaml#0"):
        sc.load_scripts(tmp_path / "s.yaml")


@pytest.mark.parametrize(
    "doc",
    [
        {"turns": []},
        {"turns": [_FINAL] * 201},
        _script(faults=[{"at": -1, "kind": "hang"}]),
        _script(faults=[{"at": 0, "kind": "boom"}]),
        _script(faults=[{"at": 0, "kind": "hang", "count": 101}]),
        _script(match={"role": "a", "other": 1}),
    ],
)
def test_ut11_61_bounds_rejected(tmp_path: Path, doc: dict[str, Any]) -> None:
    """UT11-61 turn, fault and match bounds are enforced."""
    with pytest.raises(ConfigError, match=r"s\.yaml#0"):
        sc.load_scripts(_write(tmp_path / "s.yaml", doc))


def test_ut11_61_non_mapping_entries_and_bad_files(tmp_path: Path) -> None:
    """UT11-61 non-mapping entries, empty files, bad YAML and bad paths raise ConfigError."""
    with pytest.raises(ConfigError, match=r"l\.yaml#1"):
        sc.load_scripts(_write(tmp_path / "l.yaml", [_script(), "text"]))
    empty = tmp_path / "e.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"e\.yaml#0"):
        sc.load_scripts(empty)
    broken = tmp_path / "x.yaml"
    broken.write_text("turns: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"x\.yaml"):
        sc.load_scripts(broken)
    binary = tmp_path / "bin.yaml"
    binary.write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(ConfigError, match=r"bin\.yaml"):
        sc.load_scripts(binary)
    with pytest.raises(ConfigError, match="file or directory"):
        sc.load_scripts(tmp_path / "missing")


def test_ut11_61_script_count_cap(tmp_path: Path) -> None:
    """UT11-61 more than 500 scripts in total raise ConfigError (TH11-08)."""
    _write(tmp_path / "a.yaml", [_script()] * 300)
    _write(tmp_path / "b.yaml", [_script()] * 201)
    with pytest.raises(ConfigError, match="500"):
        sc.load_scripts(tmp_path)
    _write(tmp_path / "b.yaml", [_script()] * 200)
    assert len(sc.load_scripts(tmp_path).scripts) == 500


# ---------------------------------------------------------------- UT11-62 matching


def test_ut11_62_first_script_matches_both_keys_with_independent_counters() -> None:
    """UT11-62 the org:team:*:ops script matches two keys; counters are per key."""
    book = _book(
        _script(match={"role": "analyst", "dedup_key": "org:team:*:ops"}, turns=[_FINAL] * 3),
        _script(turns=[_FINAL] * 5),
    )
    a, b = "org:team:alpha:ops", "org:team:beta:ops"
    first = [book.next_action("analyst", "standard", a) for _ in range(2)]
    other = book.next_action("analyst", "standard", b)
    assert all(act.script.source == "t#0" for act in [*first, other])
    assert [act.turn_index for act in first] == [0, 1]
    assert [act.call_index for act in first] == [0, 1]
    assert (other.turn_index, other.call_index, other.kind) == (0, 0, "turn")
    assert other.fault is None
    assert book.calls("analyst", a) == 2
    assert book.calls("analyst", b) == 1
    fallback = book.next_action("analyst", "standard", "org:team:alpha:sales")
    assert fallback.script.source == "t#1"
    assert book.next_action("planner", "fast", a).script.source == "t#1"
    assert book.calls("planner", a) == 1
    assert book.calls("nobody", "x") == 0


def test_ut11_62_role_and_model_role_filters_and_mismatch() -> None:
    """UT11-62 role and model_role match exactly or by '*'; no match raises ScriptMismatch."""
    book = _book(
        _script(match={"role": "analyst", "model_role": "deep"}),
        _script(match={"role": "chat"}),
    )
    assert book.next_action("analyst", "deep", "k").script.source == "t#0"
    assert book.next_action("chat", "fast", "k").script.source == "t#1"
    with pytest.raises(sc.ScriptMismatch) as info:
        book.next_action("analyst", "fast", "k", prompt_hash="abc123")
    err = info.value
    assert isinstance(err, FatalError)
    assert (err.role, err.model_role, err.dedup_key) == ("analyst", "fast", "k")
    assert err.call_index == 1
    assert err.prompt_hash == "abc123"
    assert book.calls("analyst", "k") == 1, "an unmatched call does not advance the counter"


def test_ut11_62_script_mismatch_rebuild_keeps_attributes() -> None:
    """UT11-62 ScriptMismatch keeps its identifying attributes through __reduce__."""
    err = sc.ScriptMismatch(
        "script exhausted", role="r", model_role="m", dedup_key="d", call_index=3
    )
    rebuild, args = err.__reduce__()
    assert callable(rebuild)
    back = rebuild(*args)
    assert isinstance(back, sc.ScriptMismatch)
    assert (back.role, back.model_role, back.dedup_key, back.call_index) == ("r", "m", "d", 3)
    assert back.prompt_hash == ""
    assert back.message == "script exhausted"


def test_ut11_62_counters_are_thread_safe() -> None:
    """UT11-62 concurrent calls on one key count every call exactly once."""
    book = _book(_script(turns=[_FINAL] * 200))
    seen: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        for _ in range(20):
            act = book.next_action("analyst", "fast", "k")
            with lock:
                seen.append(act.turn_index)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(seen) == list(range(160))
    assert book.calls("analyst", "k") == 160


# ---------------------------------------------------------------- UT11-63 faults


def test_ut11_63_fault_window_and_exhaustion() -> None:
    """UT11-63 fault at 1 count 2 with 3 turns: faults at 1-2, turns at 0,3,4, call 5 fails."""
    book = _book(_script(turns=[_FINAL] * 3, faults=[{"at": 1, "kind": "http_500", "count": 2}]))
    actions = [book.next_action("analyst", "fast", "k") for _ in range(5)]
    assert [a.kind for a in actions] == ["turn", "fault", "fault", "turn", "turn"]
    assert [a.call_index for a in actions] == [0, 1, 2, 3, 4]
    assert [a.turn_index for a in actions if a.kind == "turn"] == [0, 1, 2]
    assert [a.turn_index for a in actions if a.kind == "fault"] == [1, 1]
    faults = [a.fault for a in actions if a.kind == "fault"]
    assert all(f is not None and f.kind == "http_500" for f in faults)
    with pytest.raises(sc.ScriptMismatch, match="script exhausted") as info:
        book.next_action("analyst", "fast", "k", prompt_hash="h")
    assert info.value.call_index == 5
    assert info.value.prompt_hash == "h"
    assert book.calls("analyst", "k") == 6


# ---------------------------------------------------------------- ST11-12 hostile YAML


def _assert_fast_config_error(path: Path) -> None:
    start = time.perf_counter()
    with pytest.raises(ConfigError):
        sc.load_scripts(path)
    assert time.perf_counter() - start < 2.0


def test_st11_12_oversized_script_rejected(tmp_path: Path) -> None:
    """ST11-12 a 300 KB script file raises ConfigError within 2 s (size cap 256 KB)."""
    path = tmp_path / "big.yaml"
    path.write_text("# " + "x" * (300 * 1024) + "\n" + yaml.safe_dump(_script()), "utf-8")
    _assert_fast_config_error(path)


def test_st11_12_million_aliases_rejected(tmp_path: Path) -> None:
    """ST11-12 a YAML with 10^6 aliases raises ConfigError within 2 s."""
    path = tmp_path / "aliases.yaml"
    path.write_text("a: &a x\nb: [" + ",".join(["*a"] * 1_000_000) + "]\n", "utf-8")
    _assert_fast_config_error(path)


def test_st11_12_small_alias_bomb_rejected(tmp_path: Path) -> None:
    """ST11-12 a sub-1 KB billion-laughs document raises ConfigError within 2 s."""
    lines = ["l0: &l0 [lol, lol, lol, lol, lol, lol, lol, lol, lol, lol]"]
    for i in range(1, 10):
        refs = ", ".join([f"*l{i - 1}"] * 10)
        lines.append(f"l{i}: &l{i} [{refs}]")
    lines.append("turns: *l9")
    path = tmp_path / "bomb.yaml"
    path.write_text("\n".join(lines) + "\n", "utf-8")
    assert path.stat().st_size < 1024
    _assert_fast_config_error(path)


def test_st11_12_cyclic_and_deep_documents_rejected(tmp_path: Path) -> None:
    """ST11-12 a self-referencing anchor and a deeply nested document raise ConfigError."""
    cyclic = tmp_path / "cyclic.yaml"
    cyclic.write_text("turns: &a [*a]\n", "utf-8")
    _assert_fast_config_error(cyclic)
    deep = tmp_path / "deep.yaml"
    deep.write_text("[" * 100_000 + "]" * 100_000, "utf-8")
    _assert_fast_config_error(deep)


def test_st11_12_small_anchors_still_allowed(tmp_path: Path) -> None:
    """ST11-12 modest anchor reuse within the node budget still loads."""
    path = tmp_path / "anchors.yaml"
    lines = [
        "- &s",
        "  turns:",
        "    - final: {text: hi}",
        "- *s",
        "- match: {role: chat}",
        "  turns: [{final: {text: bye}}]",
    ]
    path.write_text("\n".join(lines) + "\n", "utf-8")
    book = sc.load_scripts(path)
    assert [s.source for s in book.scripts] == [
        "anchors.yaml#0",
        "anchors.yaml#1",
        "anchors.yaml#2",
    ]
