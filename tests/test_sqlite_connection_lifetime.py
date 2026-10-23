"""Session browsing must release SQLite handles without waiting for cyclic GC."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from cc_remote.wrapper.history_store import HistoryIndexStore
from cc_remote.wrapper.preview_capabilities import PreviewCapabilityStore
from cc_remote.wrapper.turn_changes import TurnChangeArchive


@pytest.mark.skipif(sys.platform == "win32", reason="requires Unix file limits")
@pytest.mark.parametrize("reader", [
    "rollout", "presence", "archive", "parents", "exact", "agents", "provider",
    "history", "changes", "previews",
])
def test_repeated_session_reads_do_not_exhaust_file_descriptors(tmp_path, reader):
    home = tmp_path / "native"
    home.mkdir()
    (home / "sessions").mkdir()
    rollout = home / "sessions" / "rollout-parent.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "parent", "originator": "Codex Desktop",
                    "model_provider": "openai"},
    }) + "\n")
    with closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, "
                   "archived INTEGER, source TEXT, model_provider TEXT)")
        db.execute("INSERT INTO threads VALUES (?, ?, 0, 'cli', 'openai')",
                   ("parent", str(rollout)))

    # Confine both the smaller OS limit and disabled GC to a disposable child.
    # These are real SQLite/WAL reads, not a mock connection-count assertion.
    script = textwrap.dedent("""
        import gc
        from pathlib import Path
        import resource
        import sys
        from cc_remote.wrapper import codex_sessions as sessions
        from cc_remote.wrapper.codex_agent_activity import CodexAgentActivity
        from cc_remote.wrapper.codex_provider_repair import canonical_thread_provider_is_restored
        from cc_remote.wrapper.history_store import HistoryIndexStore
        from cc_remote.wrapper.preview_capabilities import PreviewCapabilityStore
        from cc_remote.wrapper.turn_changes import TurnChangeArchive

        root = Path(sys.argv[1])
        home = root / 'native'
        reader = sys.argv[2]
        history = HistoryIndexStore(root / 'history')
        changes = TurnChangeArchive(root / 'changes')
        previews = PreviewCapabilityStore(root / 'previews')
        agents = CodexAgentActivity()
        reads = {
            'rollout': lambda: sessions.codex_thread_rollout_record('parent', codex_home=home),
            'presence': lambda: sessions.codex_session_presence('parent', codex_home=home),
            'archive': lambda: sessions.codex_thread_archive_states(['parent'], codex_home=home),
            'parents': lambda: sessions.codex_thread_parent_maps(codex_home=home),
            'exact': lambda: sessions.codex_exact_catalog_rows(['parent'], codex_home=home),
            'agents': lambda: agents.read(home, {'parent'}),
            'provider': lambda: canonical_thread_provider_is_restored('parent', codex_home=home),
            'history': lambda: history.invalidate_session('missing'),
            'changes': lambda: changes.latest_final('missing', 'turn'),
            'previews': lambda: previews._read_entries(),
        }
        read = reads[reader]
        expected = read()
        if reader not in {'history', 'changes'}:
            assert expected is not None and expected is not False, (reader, expected)
        gc.collect()
        gc.disable()
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(128, hard) if hard >= 0 else 128, hard))
        for index in range(256):
            assert read() == expected, (reader, index)
        # An unrelated file read must still work after refreshing the catalog.
        assert (home / 'sessions' / 'rollout-parent.jsonl').read_text()
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), reader],
        cwd=Path(__file__).resolve().parents[1], capture_output=True,
        text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("store_type", [
    HistoryIndexStore, TurnChangeArchive, PreviewCapabilityStore,
])
def test_store_connections_commit_rollback_and_close(tmp_path, store_type):
    store = store_type(tmp_path)
    with store._connect() as db:
        db.execute("CREATE TABLE lifetime_check (value TEXT)")
        db.execute("INSERT INTO lifetime_check VALUES ('committed')")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")

    with pytest.raises(RuntimeError, match="abort"):
        with store._connect() as db:
            db.execute("INSERT INTO lifetime_check VALUES ('rolled back')")
            raise RuntimeError("abort")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    with store._connect() as db:
        assert [row[0] for row in db.execute("SELECT value FROM lifetime_check")] == [
            "committed",
        ]
