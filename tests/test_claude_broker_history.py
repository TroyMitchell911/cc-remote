import json

from cc_remote.wrapper.claude_broker_history import (
    MAX_PARTIAL_LINE,
    claude_broker_tail_state,
    first_claude_sdk_user_id,
    parse_claude_broker_lifecycle,
)


def _row(**values) -> bytes:
    return json.dumps(values, separators=(",", ":")).encode() + b"\n"


def test_extracts_human_start_and_engine_terminal_without_tool_result():
    data = b"".join([
        _row(type="user", uuid="user-1", isSidechain=False,
             message={"role": "user", "content": "hello"}),
        _row(type="assistant", uuid="tool-1", isSidechain=False,
             message={"role": "assistant", "stop_reason": "tool_use",
                      "content": [{"type": "tool_use"}]}),
        _row(type="user", uuid="result-1", isSidechain=False,
             message={"role": "user", "content": [{"type": "tool_result"}]}),
        _row(type="assistant", uuid="final-1", isSidechain=False,
             message={"role": "assistant", "stop_reason": "end_turn",
                      "content": [{"type": "text"}]}),
    ])
    parsed = parse_claude_broker_lifecycle(data)
    assert parsed.started == ("user-1",)
    assert parsed.completed == ("final-1",)
    assert parsed.ordered == (
        ("started", "user-1"), ("completed", "final-1"))


def test_multimodal_prompt_counts_but_meta_and_sidechain_do_not():
    data = b"".join([
        _row(type="user", uuid="meta", isMeta=True,
             message={"role": "user", "content": "hidden"}),
        _row(type="user", uuid="side", isSidechain=True,
             message={"role": "user", "content": "agent"}),
        _row(type="user", uuid="real", isSidechain=False,
             message={"role": "user", "content": [{"type": "image"}]}),
    ])
    assert parse_claude_broker_lifecycle(data).started == ("real",)


def test_sdk_user_identity_requires_provenance_and_real_top_level_prompt():
    data = b"".join([
        _row(type="queue-operation", operation="enqueue"),
        _row(type="user", uuid="interrupt", entrypoint="sdk-py",
             message={"role": "user", "content": [{
                 "type": "text", "text": "[Request interrupted by user]",
             }]}),
        _row(type="user", uuid="tool-result", entrypoint="sdk-py",
             message={"role": "user", "content": [{"type": "tool_result"}]}),
        _row(type="user", uuid="sidechain", promptSource="sdk",
             isSidechain=True,
             message={"role": "user", "content": "agent task"}),
        _row(type="user", uuid="real", promptSource="sdk",
             message={"role": "user", "content": "hello"}),
    ])

    assert first_claude_sdk_user_id(data) == "real"
    assert parse_claude_broker_lifecycle(data).started == ("real",)
    assert first_claude_sdk_user_id(_row(
        type="user",
        uuid="unproven",
        message={"role": "user", "content": "hello"},
    )) is None
    assert first_claude_sdk_user_id(b"{broken\n" + data) is None


def test_partial_rows_are_joined_and_malformed_rows_are_skipped():
    row = _row(type="user", uuid="joined", isSidechain=False,
               message={"role": "user", "content": "hello"})
    first = parse_claude_broker_lifecycle(row[:17])
    assert first.started == () and first.partial
    second = parse_claude_broker_lifecycle(
        row[17:] + b"not-json\n", first.partial)
    assert second.started == ("joined",)
    assert second.partial == b""


def test_oversized_unterminated_partial_is_bounded():
    parsed = parse_claude_broker_lifecycle(b"x" * (MAX_PARTIAL_LINE + 1))
    assert parsed.partial == b""


def test_tail_state_follows_the_last_ordered_boundary(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"".join([
        _row(type="user", uuid="first", isSidechain=False,
             message={"role": "user", "content": "one"}),
        _row(type="assistant", uuid="done", isSidechain=False,
             message={"role": "assistant", "stop_reason": "end_turn"}),
        _row(type="user", uuid="second", isSidechain=False,
             message={"role": "user", "content": "two"}),
    ]))
    active, partial = claude_broker_tail_state(str(path))
    assert active is True and partial == b""


def test_native_control_envelopes_do_not_reopen_a_completed_turn(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"".join([
        _row(type="user", uuid="prompt", isSidechain=False,
             message={"role": "user", "content": "hello"}),
        _row(type="assistant", uuid="done", isSidechain=False,
             message={"role": "assistant", "stop_reason": "end_turn"}),
        _row(type="user", uuid="model-command", isSidechain=False,
             message={"role": "user", "content": (
                 "<command-name>/model</command-name>"
                 "<command-args>claude-fable-5</command-args>")}),
        _row(type="user", uuid="model-result", isSidechain=False,
             message={"role": "user", "content": (
                 "<local-command-stdout>Set model to Fable 5"
                 "</local-command-stdout>")}),
    ]))

    parsed = parse_claude_broker_lifecycle(path.read_bytes())
    assert parsed.ordered == (("started", "prompt"), ("completed", "done"))
    active, partial = claude_broker_tail_state(str(path))
    assert active is False and partial == b""
