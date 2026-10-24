"""Artifacts are scoped metadata and private copies, never a new model turn."""
import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cc_remote.protocol import (
    AssistantMsgEnd, Delta, GetSessionArtifactPreview, GetSessionArtifacts,
    History, HistoryImage, SessionArtifacts, deserialize, serialize,
)
from cc_remote.wrapper import session_artifacts
from cc_remote.wrapper.session_artifacts import (
    ArtifactStorageFull, SessionArtifactStore, image_thumbnail, local_links, public_entry,
)
from tests.test_multisession import _mk_ctx, _mk_machine


def test_artifact_upload_survives_reopen_and_has_isolated_identity(tmp_path):
    store = SessionArtifactStore(tmp_path)
    files = [{"filename": "notes.txt", "data": base64.b64encode(b"keep me").decode()}]
    store.uploads("session-a", "msg", files)
    store.uploads("session-a", "msg", files)
    reopened = SessionArtifactStore(tmp_path)
    rows, _, more = reopened.page("session-a")
    assert len(rows) == 1 and not more
    assert Path(rows[0]["path"]).read_bytes() == b"keep me"
    assert Path(rows[0]["path"]).stat().st_mode & 0o777 == 0o600
    assert reopened.page("session-b")[0] == []
    assert reopened.get("session-b", rows[0]["id"]) is None
    assert public_entry(rows[0])["available"] is True


@pytest.mark.parametrize("name", [
    "界" * 78 + ".txt", "é" * 118 + ".txt", "𠀀" * 59 + ".txt",
    "a" * 236 + ".txt", "界" * 80,
])
def test_long_upload_names_fit_byte_limit_and_keep_preview_extension(tmp_path, name):
    store = SessionArtifactStore(tmp_path)
    files = [{"filename": name, "data": "aGVsbG8="}]
    store.uploads("session", "msg", files)
    rows = store.page("session")[0]
    assert len(rows) == 1 and rows[0]["name"] == name
    path = Path(rows[0]["path"])
    assert len(path.name.encode("utf-8")) <= 255
    assert path.suffix == Path(name).suffix
    assert path.read_bytes() == b"hello"
    reopened = SessionArtifactStore(tmp_path)
    reopened.uploads("session", "msg", files)
    assert reopened.page("session")[0][0]["path"] == str(path)
    assert len([entry for entry in store.root.iterdir() if entry.name != "index.sqlite3"]) == 1


def test_truncated_upload_names_keep_distinct_copies(tmp_path):
    store = SessionArtifactStore(tmp_path)
    names = ["界" * 77 + ending + ".txt" for ending in ("甲", "乙")]
    store.uploads("session", "msg", [
        {"filename": name, "data": base64.b64encode(name.encode()).decode()}
        for name in names
    ])
    rows = store.page("session")[0]
    assert len(rows) == 2 and len({row["path"] for row in rows}) == 2
    for row in rows:
        assert len(Path(row["path"]).name.encode("utf-8")) <= 255
        assert Path(row["path"]).read_text() == row["name"]


@pytest.mark.parametrize("name", [
    "a." + "界" * 79, "界." + "é" * 94 + "x", "界." + "é" * 94,
])
def test_oversized_upload_extensions_still_fit_byte_limit(tmp_path, name):
    store = SessionArtifactStore(tmp_path)
    store.uploads("session", "msg", [{"filename": name, "data": "YQ=="}])
    row = store.page("session")[0][0]
    path = Path(row["path"])
    assert row["name"] == name and path.read_bytes() == b"a"
    assert len(path.name.encode("utf-8")) <= 255


def test_long_name_replay_reuses_existing_legacy_copy(tmp_path):
    import hashlib

    store = SessionArtifactStore(tmp_path)
    name = "a" * 236 + ".txt"
    identity = f"upload:msg:0:{name}"
    digest = hashlib.sha256(f"session\0{identity}".encode()).hexdigest()
    legacy = store.root / f"{digest}-{name[:160]}"
    legacy.write_bytes(b"hello")
    store.add("session", identity, name, str(legacy), "upload", turn_id="msg")
    store.uploads("session", "msg", [{"filename": name, "data": "aGVsbG8="}])
    assert store.page("session")[0][0]["path"] == str(legacy)
    assert legacy.read_bytes() == b"hello"
    assert len([entry for entry in store.root.iterdir() if entry.name != "index.sqlite3"]) == 1


@pytest.mark.asyncio
async def test_long_named_artifact_can_be_previewed(tmp_path):
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session", "session")
    ctx.cwd = str(tmp_path)
    machine.sessions[ctx.key] = ctx
    name = "资料" * 39 + ".txt"
    service = machine._artifacts
    await service.uploads(ctx, "msg", [{"filename": name, "data": "aGVsbG8="}])
    entry = service.store.page(service.context_scope(ctx))[0][0]
    preview = await service.preview(GetSessionArtifactPreview(
        sid=ctx.key, engine="claude", artifact_id=entry["id"],
        request_id="r", client_id="browser"))
    assert not preview.error and preview.content == "hello" and not preview.writable


def test_artifact_pages_are_chronological_and_scoped(tmp_path):
    store = SessionArtifactStore(tmp_path)
    for i in range(95):
        store.add("a", str(i), f"{i}.md", f"/missing/{i}.md", "delivered", created_at=i)
    first, cursor, more = store.page("a")
    assert len(first) == 40 and more
    assert first[0]["name"] == "94.md"
    store.add("a", "historical", "old.md", "/old.md", "delivered", created_at=0)
    second, cursor2, more = store.page("a", cursor)
    assert len(second) == 40 and more
    assert set(r["id"] for r in first).isdisjoint(r["id"] for r in second)
    third, _, more = store.page("a", cursor2)
    assert len(third) == 16 and not more
    assert not public_entry(third[0])["available"]
    with pytest.raises(ValueError):
        store.page("b", cursor)


def test_rekey_and_delete_only_remove_owned_upload_copies(tmp_path):
    store = SessionArtifactStore(tmp_path)
    old = json.dumps(["machine", "codex", "code", "tmp-one"])
    new = json.dumps(["machine", "codex", "code", "account:sid"])
    source = tmp_path / "result.md"
    source.write_text("original")
    store.uploads(old, "msg", [{"filename": "a.txt", "data": "YQ=="}])
    store.text(old, f"[result]({source})", str(tmp_path))
    copied = next(r["path"] for r in store.page(old)[0] if r["source"] == "upload")
    store.move(old, new)
    assert len(store.page(old)[0]) == len(store.page(new)[0]) == 2
    store.text(old, f"[result]({source})", str(tmp_path))
    store.uploads(new, "msg", [{"filename": "a.txt", "data": "YQ=="}])
    assert len(list(store.root.glob("*-a.txt"))) == 1
    assert len(store.page(new)[0]) == 2
    store.drop_session("other-device", "codex", "account:sid")
    assert len(store.page(new)[0]) == 2
    store.drop_session("machine", "codex", "account:sid")
    assert not Path(copied).exists() and source.exists()
    assert store.page(new)[0] == []


def test_artifact_discovery_only_follows_explicit_local_destinations():
    text = '''[代码](</project/a b.py:12>) [网络](https://example.com/a.pdf)
    [相对](docs/report.md) [文件](file:///tmp/report.pdf)
    [锚点](#section) [远程](file://server/x.md)
    ```md
    [例子](/example.md)
    ```
    visualize{"path":"/output/page.html"}'''
    assert local_links(text, "/workspace") == [
        "/project/a b.py", "/workspace/docs/report.md", "/tmp/report.pdf", "/output/page.html"]


def test_artifact_discovery_handles_directory_and_separate_delivery_names():
    text = """三张图已生成，文件还在 `/tmp/diagrams/`：

- `runtime-layers.png`：分层图，使用 `Module.Type`。
- `delivery-flow.png`：交付链路。
- `qwen-call.png`：调用时序。

其他源码：`not-delivered.py`。
"""
    assert local_links(text, "/workspace") == [
        "/tmp/diagrams/runtime-layers.png",
        "/tmp/diagrams/delivery-flow.png",
        "/tmp/diagrams/qwen-call.png",
    ]
    table = """Files are in `./output/`:

| File | Description |
|---|---|
| `报告 中文.pdf` | Report |
| `chart.svg` | Diagram |
"""
    assert local_links(table, "/workspace") == [
        "/workspace/output/报告 中文.pdf", "/workspace/output/chart.svg"]


@pytest.mark.parametrize("outer_pipes", ["both", "neither", "leading", "trailing", "mixed"])
@pytest.mark.parametrize("table", [
    "| File | Description |\n|---|---|\n"
    "| `report.pdf` | From `config.yaml`, `API.call` and `./src/input.svg` |",
    "| Description | **文件名** | Size |\n|---|---|---|\n"
    "| From `missing.yaml` |\n"
    "| From `config.yaml` and `/tmp/source.png` | `report.pdf` | `size.bytes` |",
    "| Description | File name |\n|---|---|\n"
    "| Uses `left\\|right`, `config.yaml` and `/tmp/input/` | `report.pdf` |\n"
    "| From `other.yaml` | `chart.svg` |",
    "| File | Description |\n|---|---|\n"
    "| | From `config.yaml` |\n| No output | See `fallback.pdf` |\n"
    "| `report.pdf` | From `config.yaml` |",
    "| File | Description\n|:---|---:\n| `report.pdf` | From `config.yaml`",
])
def test_artifact_directory_table_only_discovers_filename_cell(table, outer_pipes):
    lines = table.splitlines()
    if outer_pipes != "both":
        for index, line in enumerate(lines):
            # Keep a leading outer pipe when the first cell is empty, so its
            # separator cannot be mistaken for the optional outer delimiter.
            if not line.startswith("| |") and (
                    outer_pipes in {"neither", "trailing"} or (outer_pipes == "mixed" and index % 2)):
                line = line.removeprefix("|")
            if outer_pipes in {"neither", "leading"} or (outer_pipes == "mixed" and index % 2):
                line = line.removesuffix("|")
            lines[index] = line.strip()
        table = "\n".join(lines)
    text = f"Files in `/tmp/output/`:\n\n{table}"
    expected = ["/tmp/output/report.pdf"]
    if "`chart.svg`" in table:
        expected.append("/tmp/output/chart.svg")
    assert local_links(text, "/workspace") == expected


def test_artifact_directory_table_without_unique_filename_column_is_not_delivery():
    for header in ("Description | Status", "File | Filename"):
        text = f"Files in `/tmp/output/`:\n\n| {header} |\n|---|---|\n" \
               "| From `config.yaml` | `API.call` |"
        assert local_links(text, "/workspace") == []


def test_artifact_directory_table_requires_a_matching_separator():
    for separator in ("| : | : |", "|---|", "|---|---|---|", "| arbitrary | text |"):
        text = f"Files in `/tmp/output/`:\n| File | Description |\n{separator}\n" \
               "| `report.pdf` | From `config.yaml` |"
        assert local_links(text, "/workspace") == []


def test_artifact_table_columns_reset_between_directories():
    text = """Files in `/tmp/one/`:
| Description | File |
|---|---|
| From `config.yaml` | `first.pdf` |

Files in `/tmp/two/`:
| 文件 | 说明 |
|---|---|
| `second.pdf` | From `other.yaml` |
"""
    assert local_links(text, "/workspace") == ["/tmp/one/first.pdf", "/tmp/two/second.pdf"]


def test_artifact_pipe_separated_prose_does_not_extend_directory_context():
    text = """Files in `/tmp/output/`:
- `report.pdf`

Choose debug | release; use `./config.yaml` as input.
- `not-delivered.pdf`
"""
    assert local_links(text, "/workspace") == [
        "/tmp/output/report.pdf", "/workspace/config.yaml"]


def test_artifact_single_column_table_can_omit_body_and_delimiter_pipes():
    text = "Files in `/tmp/output/`:\n| File |\n---\n`report.pdf`\n`chart.svg`"
    assert local_links(text, "/workspace") == ["/tmp/output/report.pdf", "/tmp/output/chart.svg"]


@pytest.mark.parametrize("boundary", ["", "# Other files", "> Another topic", "- Another list"])
def test_artifact_table_context_ends_at_blank_lines_and_new_blocks(boundary):
    text = f"""Files in `/tmp/output/`:
File | Description
--- | ---
`report.pdf` | From `config.yaml`
{boundary}
- `not-delivered.pdf`
"""
    assert local_links(text, "/workspace") == ["/tmp/output/report.pdf"]


@pytest.mark.parametrize("directory,expected", [
    ("/tmp/output", "/tmp/output"),
    ("./output", "/workspace/output"),
    ("../output", "/output"),
    ("docs/output", "/workspace/docs/output"),
    ("output", "/workspace/output"),
    ("output.v1", "/workspace/output.v1"),
    ("输出目录", "/workspace/输出目录"),
])
def test_artifact_directory_introduction_does_not_require_trailing_slash(directory, expected):
    text = f"Files are in `{directory}`:\n\n- `report.pdf`\n- `chart.svg`"
    assert local_links(text, "/workspace") == [
        f"{expected}/report.pdf", f"{expected}/chart.svg"]
    table = f"文件在 `{directory}`：\n\n| 文件 | 说明 |\n|---|---|\n| `report.pdf` | 报告 |"
    assert local_links(table, "/workspace") == [f"{expected}/report.pdf"]


def test_artifact_directory_introduction_does_not_reinterpret_files_or_commands():
    assert local_links("Read `/tmp/report.pdf`:\n- `unrelated.png`", "/workspace") == [
        "/tmp/report.pdf"]
    assert local_links("Run `ls /tmp/output`:\n- `unrelated.png`", "/workspace") == []
    assert local_links("In `/tmp/one` or `/tmp/two`:\n- `unrelated.png`", "/workspace") == []
    assert local_links("Example `identifier`:\n- `unrelated.png`", "/workspace") == []


def test_artifact_inline_paths_are_explicit_and_do_not_reinterpret_link_labels():
    text = """结果：`/tmp/my diagram.png`、`./output/report.pdf`、`docs/notes.md:12`。
[`fake/label.png`](actual.png)
`https://example.com/a.png` `//server/a.png` `cat /tmp/read.png`
`/tmp/*.png` `result.png` `API.call` `$OUTPUT/result.png`
"""
    assert local_links(text, "/workspace") == [
        "/workspace/actual.png", "/tmp/my diagram.png",
        "/workspace/output/report.pdf", "/workspace/docs/notes.md"]


@pytest.mark.parametrize("separator", [
    "另一个段落。", "## Another section", "```sh\nls /tmp/diagrams\n```",
    "~~~text\nexample\n~~~", "可能在 `/tmp/one/` 或 `/tmp/two/`：",
])
def test_artifact_directory_context_does_not_escape_its_file_list(separator):
    text = f"""Files in `/tmp/diagrams/`:
- `first.png`
{separator}
- `unrelated.png`
"""
    assert local_links(text, "/workspace") == ["/tmp/diagrams/first.png"]


def test_artifact_inline_discovery_skips_closed_and_unclosed_code_examples():
    text = """Example:
```md
`/tmp/example.png`
Files in `/tmp/example/`:
- `example.svg`
```
Actual file: `/tmp/actual.png`
~~~md
`/tmp/unclosed.png`
"""
    assert local_links(text, "/workspace") == ["/tmp/actual.png"]


def test_artifact_inline_discovery_is_bounded_and_deduplicated():
    text = "\n".join(f"`/tmp/result-{i}.png`" for i in range(300))
    paths = local_links(text, "/workspace")
    assert len(paths) == 128
    assert paths[-1] == "/tmp/result-127.png"
    assert local_links("[result](/tmp/a.png) `/tmp/a.png`", "/workspace") == ["/tmp/a.png"]


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("trailing_slash", ["", "/"])
@pytest.mark.parametrize("presentation", ["list", "table", "table-no-outer-pipes"])
async def test_live_and_historical_delivery_lists_share_the_same_artifact_catalog(
        tmp_path, engine, trailing_slash, presentation):
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session", "session")
    ctx.engine = engine
    ctx.cwd = str(tmp_path)
    output = tmp_path / "diagrams"
    output.mkdir()
    names = ["runtime-layers.png", "delivery-flow.png", "qwen-call.png"]
    for name in names:
        (output / name).write_bytes(_test_image())
    (output / "config.yaml").write_text("source: input")
    text = f"三张图已生成，文件在 `{output}{trailing_slash}`：\n\n"
    if presentation.startswith("table"):
        text += "| 文件 | 说明 |\n|---|---|\n" + "\n".join(
            f"| `{name}` | 从 `config.yaml` 生成。 |" for name in names)
        if presentation == "table-no-outer-pipes":
            text = "\n".join(line.strip("| ") for line in text.splitlines())
    else:
        text += "\n".join(f"- `{name}`：图的说明。" for name in names)
    service = machine._artifacts
    for part in [text[:27], text[27:]]:
        await service.observe(ctx, Delta(message_id="answer", text=part))
    await service.observe(ctx, AssistantMsgEnd(message_id="answer", channel="final"))
    scope = service.context_scope(ctx)
    live = service.store.page(scope)[0]
    assert {row["name"] for row in live} == set(names)
    assert all(public_entry(row)["available"] for row in live)
    history = History(session_id="session", revision="rev", detail="summary", turns=[
        {"id": "turn", "ts": 1000, "blocks": [{"kind": "text", "text": text}]},
    ])
    service.store.history(scope, history, ctx.cwd)
    assert len(service.store.page(scope)[0]) == 3
    # A cold/history-only catalog uses the same discovery, without engine calls.
    cold_scope = service.scope(engine, "code", "cold")
    service.store.history(cold_scope, history, ctx.cwd)
    assert {row["path"] for row in service.store.page(cold_scope)[0]} == {
        str(output / name) for name in names}
    assert service.store.page(service.scope(engine, "code", "unrelated"))[0] == []


def test_artifact_root_symlink_is_rejected(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "session-artifacts").symlink_to(target)
    with pytest.raises(OSError):
        SessionArtifactStore(tmp_path)


@pytest.mark.parametrize("limit", [
    "MAX_SESSION_UPLOAD_BYTES", "MAX_SESSION_UPLOAD_FILES",
    "MAX_TOTAL_UPLOAD_BYTES", "MAX_TOTAL_UPLOAD_FILES",
])
def test_durable_upload_limits_survive_reopen_without_eviction(tmp_path, monkeypatch, limit):
    monkeypatch.setattr(session_artifacts, limit, 3 if limit.endswith("BYTES") else 1)
    files = [{"filename": "note.txt", "data": "aGk="}]
    store = SessionArtifactStore(tmp_path)
    store.uploads("first", "one", files)
    copied = Path(store.page("first")[0][0]["path"])
    store = SessionArtifactStore(tmp_path)
    store.uploads("first", "one", files)  # Replays do not consume quota twice.
    target = "second" if "TOTAL" in limit else "first"
    with pytest.raises(ArtifactStorageFull):
        store.uploads(target, "two", files)
    assert copied.read_bytes() == b"hi"
    assert len(list(store.root.glob("*-note.txt"))) == 1
    assert len(store.page("first")[0]) == 1
    if "SESSION" in limit:
        store.uploads("other-account", "two", files)
        assert len(store.page("other-account")[0]) == 1


def test_quota_rejects_entire_batch_before_copying(tmp_path, monkeypatch):
    monkeypatch.setattr(session_artifacts, "MAX_SESSION_UPLOAD_FILES", 1)
    store = SessionArtifactStore(tmp_path)
    with pytest.raises(ArtifactStorageFull):
        store.uploads("session", "msg", [
            {"filename": "one.txt", "data": "YQ=="},
            {"filename": "two.txt", "data": "Yg=="},
        ])
    assert store.page("session")[0] == []
    assert not list(store.root.glob("*.txt"))


@pytest.mark.parametrize("limit", ["MAX_TOTAL_UPLOAD_BYTES", "MAX_TOTAL_UPLOAD_FILES"])
def test_orphan_and_legacy_copies_count_towards_global_budget(tmp_path, monkeypatch, limit):
    store = SessionArtifactStore(tmp_path)
    orphan = store.root / "interrupted-copy.txt"
    orphan.write_bytes(b"old")
    monkeypatch.setattr(session_artifacts, limit, 3 if limit.endswith("BYTES") else 1)
    reopened = SessionArtifactStore(tmp_path)
    with pytest.raises(ArtifactStorageFull):
        reopened.uploads("session", "msg", [{"filename": "new.txt", "data": "YQ=="}])
    assert reopened.page("session")[0] == []
    assert orphan.read_bytes() == b"old"
    assert not list(store.root.glob("*-new.txt"))


def test_upload_reserves_free_disk_before_creating_copy(tmp_path, monkeypatch):
    store = SessionArtifactStore(tmp_path)
    monkeypatch.setattr(session_artifacts.shutil, "disk_usage", lambda _: SimpleNamespace(
        free=session_artifacts.MIN_FREE_UPLOAD_BYTES))
    with pytest.raises(ArtifactStorageFull, match="reserve"):
        store.uploads("session", "msg", [{"filename": "note.txt", "data": "YQ=="}])
    assert store.page("session")[0] == []
    assert not list(store.root.glob("*.txt"))


def test_quota_tracks_rekey_and_session_deletion(tmp_path, monkeypatch):
    monkeypatch.setattr(session_artifacts, "MAX_SESSION_UPLOAD_FILES", 1)
    monkeypatch.setattr(session_artifacts, "MAX_TOTAL_UPLOAD_FILES", 1)
    store = SessionArtifactStore(tmp_path)
    old = json.dumps(["machine", "claude", "code", "tmp-one"])
    new = json.dumps(["machine", "claude", "code", "account@session"])
    files = [{"filename": "note.txt", "data": "YQ=="}]
    store.uploads(old, "msg", files)
    store.move(old, new)
    store.uploads(old, "msg", files)
    with pytest.raises(ArtifactStorageFull):
        store.uploads(new, "second", files)
    store.drop_session("machine", "claude", "account@session")
    store.uploads("another-session", "third", files)
    assert len(list(store.root.glob("*.txt"))) == 1


def test_concurrent_stores_cannot_overbook_upload_quota(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.setattr(session_artifacts, "MAX_TOTAL_UPLOAD_FILES", 1)
    stores = [SessionArtifactStore(tmp_path), SessionArtifactStore(tmp_path)]
    ready = Barrier(2)

    def upload(i):
        ready.wait(timeout=5)
        try:
            stores[i].uploads(str(i), "msg", [{"filename": "note.txt", "data": "YQ=="}])
        except ArtifactStorageFull:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(upload, range(2))) == [False, True]
    assert len(list(stores[0].root.glob("*.txt"))) == 1
    assert sum(len(stores[0].page(str(i))[0]) for i in range(2)) == 1


@pytest.mark.parametrize("failure", ["fsync", "metadata"])
def test_failed_upload_batch_removes_new_copies_and_rolls_back_index(tmp_path, monkeypatch, failure):
    store = SessionArtifactStore(tmp_path)
    if failure == "fsync":
        original = session_artifacts.os.fsync
        target, method = session_artifacts.os, "fsync"
    else:
        original = store._add
        target, method = store, "_add"
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk write failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method, fail_second)
    with pytest.raises(OSError, match="disk write failed"):
        store.uploads("session", "msg", [
            {"filename": "one.txt", "data": "YQ=="},
            {"filename": "two.txt", "data": "Yg=="},
        ])
    assert store.page("session")[0] == []
    assert not list(store.root.glob("*.txt"))


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
async def test_cold_artifacts_validate_account_space_before_history_or_preview(tmp_path, monkeypatch, engine):
    from cc_remote.viewer_pages import PageScope

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "a"))
    key = "config_dir" if engine == "claude" else "home"
    monkeypatch.setenv(f"CC_REMOTE_{engine.upper()}_PROFILES_JSON", json.dumps({
        "a": {"label": "A", key: str(tmp_path / "a"), "default": True},
        "b": {"label": "B", key: str(tmp_path / "b")},
    }))
    machine, _ = _mk_machine()
    machine._spawn = AsyncMock(side_effect=AssertionError("must not spawn an engine"))
    service = machine._artifacts
    registry = machine._work.for_engine(engine)
    profile = {f"{engine}_profile_id": "a"}
    record = registry.create_session(**profile)
    registry.bind_session(record.work_id, "native-session", **profile)
    registry.update_archived("native-session", True, **profile)
    # Native cwd deliberately disagrees with registered Work metadata.
    machine._codex_rollout_for_wire = Mock(return_value=str(tmp_path / "rollout.jsonl"))
    machine._codex_cwd_for_wire = Mock(return_value=str(tmp_path))
    machine._claude_transcript_path_for_wire = Mock(return_value=str(tmp_path / "transcript.jsonl"))
    machine._claude_catalog_session_info = Mock(return_value=SimpleNamespace(cwd=str(tmp_path)))
    outside = tmp_path / "outside.txt"
    outside.write_text("outside Work")
    inside = Path(record.cwd) / "inside.txt"
    inside.write_text("inside Work")
    machine._history_page_singleflight = AsyncMock(return_value=History(
        session_id="a@native-session", revision="r", detail="summary", turns=[{
            "id": "turn", "ts": 1000, "blocks": [{"kind": "text", "text":
                f"[inside]({inside}) [outside]({outside})"}]}]))
    work_sid, code_sid = "a@native-session", "b@native-session"
    forged = GetSessionArtifacts(sid=work_sid, engine=engine, space="code",
        request_id="r", client_id="client", scan_history=True)
    denied = await service.list(forged)
    assert denied.error and denied.entries == []
    machine._history_page_singleflight.assert_not_called()
    assert service.store.page(service.scope(engine, "code", work_sid))[0] == []
    # Even a catalog cached before this fix cannot authorize a forged preview.
    poisoned_scope = service.scope(engine, "code", work_sid)
    service.store.text(poisoned_scope, f"[file]({outside})", str(tmp_path))
    poisoned = service.store.page(poisoned_scope)[0][0]
    inspect = Mock(wraps=machine._preview_capability_store.inspect_path)
    monkeypatch.setattr(machine._preview_capability_store, "inspect_path", inspect)
    for variant in ("full", "thumbnail"):
        result = await service.preview(GetSessionArtifactPreview(
            sid=work_sid, engine=engine, space="code", artifact_id=poisoned["id"],
            request_id="p", client_id="client", variant=variant))
        assert result.error and not result.content and not result.data
    inspect.assert_not_called()

    scope, cwd = await machine._viewer_page_scope(PageScope(sid=work_sid, engine=engine, space="work"))
    assert scope.space == "work" and cwd == record.cwd
    legitimate = await service.list(forged.model_copy(update={"space": "work"}))
    assert not legitimate.error and len(legitimate.entries) == 2
    for entry in legitimate.entries:
        result = await service.preview(GetSessionArtifactPreview(
            sid=work_sid, engine=engine, space="work", artifact_id=entry.id,
            request_id="p", client_id="client"))
        if entry.name == "inside.txt":
            assert result.content == "inside Work" and not result.error
        else:
            assert result.error and not result.content
    inspect.assert_not_called()
    # Same native id in a different account is Code, not the archived Work row.
    code = await service.list(forged.model_copy(update={"sid": code_sid}))
    assert not code.error
    external = next(entry for entry in code.entries if entry.name == "outside.txt")
    result = await service.preview(GetSessionArtifactPreview(
        sid=code_sid, engine=engine, space="code", artifact_id=external.id,
        request_id="p", client_id="client"))
    assert result.content == "outside Work" and not result.error
    rejected = await service.list(forged.model_copy(update={"sid": code_sid, "space": "work"}))
    assert rejected.error
    machine._spawn.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("failure", ["migration", "registry"])
async def test_cold_artifact_space_lookup_fails_closed(tmp_path, monkeypatch, engine, failure):
    machine, _ = _mk_machine()
    if failure == "migration":
        setattr(machine, f"_{engine}_work_profile_migration_ok", False)
    else:
        monkeypatch.setattr(machine._work.for_engine(engine), "get_by_session",
                            Mock(side_effect=OSError("registry unreadable")))
    machine._history_page_singleflight = AsyncMock()
    result = await machine._artifacts.list(GetSessionArtifacts(
        sid="session", engine=engine, space="code", request_id="r", scan_history=True))
    assert result.error and not result.entries
    machine._history_page_singleflight.assert_not_called()


@pytest.mark.asyncio
async def test_live_discovery_rekey_and_thinking_exclusion(tmp_path):
    machine, _ = _mk_machine()
    ctx = _mk_ctx("tmp-one")
    ctx.cwd = str(tmp_path)
    result = tmp_path / "answer.md"
    result.write_text("answer")
    service = machine._artifacts
    await service.observe(ctx, Delta(message_id="a", text=f"[结果]({result})"))
    await service.observe(ctx, AssistantMsgEnd(message_id="a", channel="final"))
    await service.observe(ctx, Delta(message_id="b", text="[secret](/secret.txt)", channel="thinking"))
    await service.observe(ctx, AssistantMsgEnd(message_id="b", channel="thinking"))
    rows, _, _ = service.store.page(service.context_scope(ctx))
    assert len(rows) == 1 and rows[0]["name"] == "answer.md"
    await machine._rekey_preview_session(ctx, "tmp-one", "real-one")
    assert len(service.store.page(service.context_scope(ctx, "tmp-one"))[0]) == 1
    assert len(service.store.page(service.context_scope(ctx, "real-one"))[0]) == 1


@pytest.mark.asyncio
async def test_catalog_reads_cache_first_then_one_bounded_history_page(tmp_path):
    machine, transport = _mk_machine()
    ctx = _mk_ctx("session", "session")
    ctx.cwd = str(tmp_path)
    machine.sessions[ctx.key] = ctx
    machine._history_page_singleflight = AsyncMock(return_value=History(
        session_id="session", revision="rev", detail="summary", oldest_id="older", has_more=True,
        turns=[{"id": "turn", "ts": 1000, "blocks": [{"kind": "text", "text": "[成果](result.md)"}],
                "imageRefs": [{"image_id": "img", "media_type": "image/png", "width": 1, "height": 1, "byte_size": 1}]}]))
    cmd = GetSessionArtifacts(sid="session", engine="claude", request_id="request", client_id="browser")
    result = await machine._handle_get_session_artifacts(cmd)
    assert result.entries == [] and result.to == "browser"
    machine._history_page_singleflight.assert_not_called()
    result = await machine._handle_get_session_artifacts(cmd.model_copy(update={"scan_history": True}))
    requested, sid = machine._history_page_singleflight.call_args.args
    assert sid == "session" and requested.limit == 12 and requested.detail == "summary"
    assert result.history_more and result.history_before == "older"
    assert len(result.entries) == 2
    assert isinstance(deserialize(serialize(result)), SessionArtifacts)
    assert len(transport.sent) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["full", "thumbnail"])
async def test_catalog_scope_mismatch_and_foreign_preview_are_rejected(tmp_path, variant):
    machine, _ = _mk_machine()
    a, b = _mk_ctx("a", "a"), _mk_ctx("b", "b")
    a.cwd = b.cwd = str(tmp_path)
    machine.sessions.update(a=a, b=b)
    service = machine._artifacts
    await service.uploads(a, "msg", [{"filename": "test.txt", "data": "c2VjcmV0"}])
    entry = service.store.page(service.context_scope(a))[0][0]
    machine._read_file_preview = lambda *args: pytest.fail("must not read a foreign file")
    result = await machine._handle_get_session_artifact_preview(GetSessionArtifactPreview(
        sid="b", engine="claude", artifact_id=entry["id"], request_id="x", client_id="client", variant=variant))
    assert result.error and result.sid == "b"
    for field, value in (("engine", "codex"), ("space", "work")):
        args = dict(sid="a", engine="claude", request_id="x") | {field: value}
        result = await machine._handle_get_session_artifacts(GetSessionArtifacts(**args))
        assert result.error and not result.entries


@pytest.mark.asyncio
async def test_uploaded_file_previews_without_native_session_call(tmp_path):
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session", "session")
    ctx.cwd = str(tmp_path)
    machine.sessions[ctx.key] = ctx
    service = machine._artifacts
    await service.uploads(ctx, "msg", [{"filename": "note.txt", "data": "aGVsbG8="}])
    entry = service.store.page(service.context_scope(ctx))[0][0]
    result = await machine._handle_get_session_artifact_preview(GetSessionArtifactPreview(
        sid="session", engine="claude", artifact_id=entry["id"], request_id="x", client_id="client"))
    assert result.content == "hello" and not result.writable and not result.error


def test_artifact_protocol_bounds():
    cmd = GetSessionArtifacts(sid="a", engine="codex", request_id="r", before=4)
    assert deserialize(serialize(cmd)) == cmd
    with pytest.raises(ValueError):
        GetSessionArtifacts(engine="codex", request_id="r", before=-1)
    thumbnail = GetSessionArtifactPreview(sid="a", engine="codex", request_id="r",
        artifact_id="image", variant="thumbnail")
    assert deserialize(serialize(thumbnail)) == thumbnail
    with pytest.raises(ValueError):
        GetSessionArtifactPreview(engine="codex", request_id="r", artifact_id="image", variant="invalid")


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["full", "thumbnail"])
async def test_historical_images_preview_only_to_the_requester(tmp_path, variant):
    machine, transport = _mk_machine()
    ctx = _mk_ctx("session", "session")
    ctx.cwd = str(tmp_path)
    machine.sessions[ctx.key] = ctx
    service = machine._artifacts
    scope = service.context_scope(ctx)
    service.store.text(scope, "[image](output.png)", ctx.cwd)
    service.store.history(scope, History(session_id="session", revision="rev", turns=[{
        "id": "turn", "ts": 1000, "files": [{"filename": "old.txt"}],
        "blocks": [{"kind": "process", "tool": "image_generation", "status": "succeeded",
                    "input": {"file_path": str(tmp_path / "output.png"),
                              "history_image": {"image_id": "generated"}}}],
    }]), ctx.cwd)
    rows = service.store.page(scope)[0]
    assert len(rows) == 2  # Native image locator upgrades the same output file.
    image = next(row for row in rows if row["image_id"])
    missing = next(row for row in rows if row["identity"].startswith("missing:"))
    assert not public_entry(missing)["available"]
    machine._read_file_preview = lambda *args: pytest.fail("must use the source-bound historical locator")
    machine._handle_get_history_image = AsyncMock(return_value=HistoryImage(
        session_id="session", turn_id="turn", image_id="generated", variant="full",
        request_id="read", revision="rev", media_type="image/png", data=base64.b64encode(_test_image()).decode()))
    response = await service.preview(GetSessionArtifactPreview(
        sid="session", engine="claude", artifact_id=image["id"], request_id="read", client_id="reader", variant=variant))
    assert response.to == "reader" and response.data and not response.error
    assert machine._handle_get_history_image.call_args.args[0].variant == variant
    assert machine._handle_get_history_image.call_args.kwargs == {"publish": False}
    assert len(transport.sent) == 1 and transport.sent[0].type == "file_preview"
    response = await service.preview(GetSessionArtifactPreview(
        sid="session", engine="claude", artifact_id=missing["id"], request_id="missing", client_id="reader"))
    assert response.error and not response.data


def _test_image():
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGB", (640, 320), "#b4c8ed").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["upload", "delivered"])
async def test_misnamed_artifact_image_full_preview_and_thumbnail(tmp_path, source):
    from PIL import Image
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session", "session")
    ctx.cwd = str(tmp_path)
    machine.sessions[ctx.key] = ctx
    service = machine._artifacts
    output = io.BytesIO()
    Image.new("RGB", (64, 32), "red").save(output, format="JPEG")
    data = output.getvalue()
    if source == "upload":
        await service.uploads(ctx, "msg", [{"filename": "export.png", "data": base64.b64encode(data).decode()}])
    else:
        (tmp_path / "export.png").write_bytes(data)
        service.store.text(service.context_scope(ctx), "[picture](export.png)", ctx.cwd)
    row = service.store.page(service.context_scope(ctx))[0][0]
    for variant, media in [("full", "image/jpeg"), ("thumbnail", "image/webp")]:
        response = await service.preview(GetSessionArtifactPreview(
            sid="session", engine="claude", artifact_id=row["id"],
            request_id="read", client_id="reader", variant=variant))
        assert not response.error and response.media_type == media
        assert response.to == "reader" and response.sid == "session"
        with Image.open(io.BytesIO(base64.b64decode(response.data))) as image:
            assert image.size == (64, 32)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["upload", "delivered"])
async def test_thumbnail_is_small_readonly_and_never_opens_full_preview(tmp_path, source):
    from PIL import Image
    machine, transport = _mk_machine()
    ctx = _mk_ctx("session", "session")
    ctx.cwd = str(tmp_path)
    machine.sessions[ctx.key] = ctx
    service = machine._artifacts
    raw = _test_image()
    if source == "upload":
        await service.uploads(ctx, "msg", [{"filename": "image.png", "data": base64.b64encode(raw).decode()}])
    else:
        (tmp_path / "image.png").write_bytes(raw)
        service.store.text(service.context_scope(ctx), "[picture](image.png)", ctx.cwd)
    row = service.store.page(service.context_scope(ctx))[0][0]
    machine._handle_get_file_preview = AsyncMock(side_effect=AssertionError("must not open full preview"))
    response = await service.preview(GetSessionArtifactPreview(
        sid="session", engine="claude", artifact_id=row["id"], request_id="thumb",
        client_id="reader", variant="thumbnail"))
    assert not response.error and response.media_type == "image/webp" and not response.writable
    assert response.to == "reader" and response.sid == "session"
    decoded = base64.b64decode(response.data)
    assert response.size == len(decoded) < 64 * 1024
    with Image.open(io.BytesIO(decoded)) as thumbnail:
        assert thumbnail.size == (160, 80)
    assert len(transport.sent) == 1
    machine._handle_get_file_preview.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["markdown", "inline"])
async def test_automatic_thumbnail_does_not_grant_external_file_access(tmp_path, reference):
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session", "session")
    cwd = tmp_path / "project"
    cwd.mkdir()
    ctx.cwd = str(cwd)
    machine.sessions[ctx.key] = ctx
    service = machine._artifacts
    outside = tmp_path / "private.png"
    outside.write_bytes(_test_image())
    text = f"[image]({outside})" if reference == "markdown" else f"结果：`{outside}`"
    service.store.text(service.context_scope(ctx), text, ctx.cwd)
    row = service.store.page(service.context_scope(ctx))[0][0]
    cmd = GetSessionArtifactPreview(sid="session", engine="claude", artifact_id=row["id"],
        request_id="thumb", client_id="reader", variant="thumbnail")
    response = await service.preview(cmd)
    assert response.error and not response.data
    assert not machine._preview_capability_store.snapshot("claude", "code", "session")
    # A grant for another session is not enough, even with the same cwd.
    machine._preview_capability_store.grant_path("claude", "code", "other", str(outside),
        mode="read", source="user_approved")
    assert (await service.preview(cmd)).error
    machine._preview_capability_store.grant_path("claude", "code", "session", str(outside),
        mode="read", source="user_approved")
    assert not (await service.preview(cmd)).error
    replacement = tmp_path / "replacement.png"
    replacement.write_bytes(_test_image())
    replacement.replace(outside)
    assert (await service.preview(cmd)).error


@pytest.mark.asyncio
async def test_thumbnail_rejects_oversized_files_and_non_images(tmp_path):
    machine, _ = _mk_machine()
    ctx = _mk_ctx("session", "session")
    ctx.cwd = str(tmp_path)
    machine.sessions[ctx.key] = ctx
    for name in ["large.png", "script.svg", "notes.md"]:
        target = tmp_path / name
        target.write_text("not an image")
        if name == "large.png":
            with target.open("ab") as stream:
                stream.truncate(7 * 1024 * 1024)
        machine._artifacts.store.text(machine._artifacts.context_scope(ctx), f"[file]({name})", ctx.cwd)
    for row in machine._artifacts.store.page(machine._artifacts.context_scope(ctx))[0]:
        result = await machine._artifacts.preview(GetSessionArtifactPreview(
            sid="session", engine="claude", artifact_id=row["id"], request_id="thumb", variant="thumbnail"))
        assert result.error and not result.data


def test_thumbnail_pixel_limit_is_checked_before_decode(monkeypatch):
    monkeypatch.setattr("cc_remote.wrapper.session_artifacts.MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ValueError, match="尺寸过大"):
        image_thumbnail(_test_image())


@pytest.mark.asyncio
async def test_artifact_store_failure_does_not_block_native_rekey_or_delete():
    machine, _ = _mk_machine()
    ctx = _mk_ctx("tmp-one")
    class BrokenStore:
        def move(self, *args):
            raise OSError("unavailable sidecar")

        def drop_session(self, *args):
            raise OSError("unavailable sidecar")

    machine._artifacts._store = BrokenStore()
    await machine._rekey_preview_session(ctx, "tmp-one", "native-one")
    await machine._drop_preview_session(ctx.engine, "native-one")


def test_account_device_and_space_are_part_of_artifact_scope():
    machine, _ = _mk_machine()
    scope = machine._artifacts.scope
    assert scope("codex", "code", "primary:sid") != scope("codex", "code", "secondary:sid")
    assert scope("claude", "code", "sid") != scope("claude", "work", "sid")
    assert json.loads(scope("codex", "code", "primary:sid"))[0] == machine.cfg.machine_id


@pytest.mark.asyncio
async def test_readonly_btw_artifacts_keep_owner_isolation(tmp_path):
    from tests.test_btw_lifecycle import _fork

    machine, _ = _mk_machine()
    ctx = _fork(machine)
    ctx.cwd = str(tmp_path)
    await machine._artifacts.uploads(ctx, "upload", [{"filename": "retained.txt", "data": "aGk="}])
    result = await machine._handle(GetSessionArtifacts(
        sid=ctx.key, engine="codex", request_id="list", client_id="tab", owner_id="owner"))
    assert len(result.entries) == 1 and ctx.btw_destroyed
    preview = GetSessionArtifactPreview(
        sid=ctx.key, engine="codex", artifact_id=result.entries[0].id,
        request_id="preview", client_id="tab", owner_id="owner")
    response = await machine._handle(preview)
    assert response.content == "hi" and not response.writable
    rejected = await machine._handle(preview.model_copy(update={"owner_id": "another-owner"}))
    assert rejected.type == "error"
    ctx.sdk.force_reconnect.assert_not_called()
    ctx.sdk.disconnect.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_full", [False, True])
async def test_claude_steering_upload_outlives_its_temporary_prompt_files(tmp_path, monkeypatch, storage_full):
    from types import SimpleNamespace
    from cc_remote.protocol import Steer
    from cc_remote.wrapper import claude_steer

    machine, _ = _mk_machine()
    if storage_full:
        monkeypatch.setattr(session_artifacts, "MAX_TOTAL_UPLOAD_FILES", 0)
    ctx = _mk_ctx("session", "session")
    ctx.cwd, ctx.state, ctx.write_state = str(tmp_path), "running", "writable"
    ctx.sdk = SimpleNamespace(steer=AsyncMock())
    reject = AsyncMock()
    await claude_steer.handle(machine, ctx, Steer(
        sid="session", msg_id="guide", cmd_id="command", client_id="browser", prompt="read this",
        files=[{"filename": "notes.txt", "data": "aGk="}]), reject)
    reject.assert_not_called()
    ctx.sdk.steer.assert_awaited_once()
    staged = ctx.claude_steer_attachment_dirs[:]
    assert staged
    claude_steer.cleanup(ctx)
    assert all(not Path(path).exists() for path in staged)
    rows = machine._artifacts.store.page(machine._artifacts.context_scope(ctx))[0]
    if storage_full:
        assert rows == []  # The optional copy must not block delivery to the SDK.
        return
    row = rows[0]
    assert row["name"] == "notes.txt" and Path(row["path"]).read_text() == "hi"


@pytest.mark.asyncio
async def test_dsh_uses_native_provenance_and_repairs_only_legacy_session_references(tmp_path):
    from tests.test_dsh_runtime import setup_runtime
    machine, _, runtime, _ = setup_runtime()
    ctx = _mk_ctx("dsh@root", "dsh@root")
    ctx.engine, ctx.cwd = "dsh", str(tmp_path)
    service = machine._artifacts
    scope = service.context_scope(ctx)
    text = f"只读取了 [代码](<{tmp_path}/source.cpp>)，没有修改。"
    await service.observe(ctx, Delta(message_id="answer", text=text))
    await service.observe(ctx, AssistantMsgEnd(message_id="answer", channel="final"))
    assert service.store.page(scope)[0] == []
    service.store.text(scope, text, ctx.cwd)  # old index from the deployed version
    service.store.text("other", text, ctx.cwd)
    service.store.add(scope, "upload:1", "notes.md", "/notes.md", "upload")
    history = History(session_id=ctx.key, revision="test", detail="summary", turns=[
        {"id": "turn", "blocks": [{"kind": "text", "text": text}]}])
    service.store.history(scope, history, ctx.cwd, discover_text=False)
    service.store.native_outputs(scope, [], ctx.cwd)
    assert [r["source"] for r in service.store.page(scope)[0]] == ["upload"]
    assert len(service.store.page("other")[0]) == 1
    service.store.native_outputs(scope, [{"path": str(tmp_path / "output.png")}], ctx.cwd)
    service.store.native_outputs(scope, [], ctx.cwd)  # bounded metadata must retain older verified files
    assert {r["name"] for r in service.store.page(scope)[0]} == {"notes.md", "output.png"}
    await runtime.close()
