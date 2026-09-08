"""Durable Codex archive no-replay journal regressions."""
from __future__ import annotations

import pytest

from cc_remote.wrapper import codex_archives as codex_archives_module
from cc_remote.wrapper.codex_archives import (
    CodexArchiveJournal,
    CodexArchiveJournalError,
)


def test_submitted_archive_survives_restart_without_second_claim(tmp_path):
    journal = CodexArchiveJournal(tmp_path)
    journal.begin(
        "client-1",
        "archive-1",
        "primary",
        "root",
        ("root", "child"),
        {"root": False, "child": False},
    )
    assert journal.claim_submission("client-1", "archive-1") is True
    journal.mark_unknown("client-1", "archive-1")

    restarted = CodexArchiveJournal(tmp_path)
    entry = restarted.get("client-1", "archive-1")
    assert entry is not None and entry["status"] == "unknown"
    assert restarted.claim_submission("client-1", "archive-1") is False


def test_corrupt_archive_journal_fails_closed(tmp_path):
    (tmp_path / "codex-archives.json").write_text("not-json")
    with pytest.raises(CodexArchiveJournalError, match="unreadable"):
        CodexArchiveJournal(tmp_path)


def test_completed_archive_accepts_root_with_best_effort_descendant(tmp_path):
    journal = CodexArchiveJournal(tmp_path)
    journal.begin(
        "client-1",
        "archive-partial",
        "primary",
        "root",
        ("root", "busy-child"),
        {"root": False, "busy-child": False},
    )
    assert journal.claim_submission("client-1", "archive-partial") is True

    completed = journal.complete(
        "client-1",
        "archive-partial",
        ("root",),
    )

    assert completed["status"] == "complete"
    assert completed["archived_ids"] == ["root"]
    restarted = CodexArchiveJournal(tmp_path)
    assert restarted.get("client-1", "archive-partial") == completed


def test_completed_archive_must_include_root(tmp_path):
    journal = CodexArchiveJournal(tmp_path)
    journal.begin(
        "client-1",
        "archive-invalid",
        "primary",
        "root",
        ("root", "child"),
        {"root": False, "child": False},
    )
    journal.claim_submission("client-1", "archive-invalid")

    with pytest.raises(ValueError, match="must include|invalid completed"):
        journal.complete("client-1", "archive-invalid", ("child",))


def test_byte_bound_evicts_oldest_terminal_entry_and_survives_restart(
    tmp_path,
    monkeypatch,
):
    journal = CodexArchiveJournal(tmp_path)
    long_root = "root-" + "a" * 100
    journal.begin(
        "client-1",
        "archive-1",
        "primary",
        long_root,
        (long_root,),
        {long_root: False},
    )
    journal.claim_submission("client-1", "archive-1")
    journal.reject("client-1", "archive-1", "internal", "x" * 300)
    first_size = journal.path.stat().st_size
    monkeypatch.setattr(
        codex_archives_module,
        "_MAX_FILE_BYTES",
        first_size + 16,
    )

    second_root = "root-" + "b" * 100
    journal.begin(
        "client-2",
        "archive-2",
        "primary",
        second_root,
        (second_root,),
        {second_root: False},
    )
    journal.claim_submission("client-2", "archive-2")
    journal.reject("client-2", "archive-2", "internal", "y" * 300)

    assert journal.path.stat().st_size <= first_size + 16
    assert journal.get("client-1", "archive-1") is None
    assert journal.get("client-2", "archive-2") is not None
    restarted = CodexArchiveJournal(tmp_path)
    assert restarted.get("client-1", "archive-1") is None
    assert restarted.get("client-2", "archive-2")["status"] == "rejected"


def test_byte_bound_fails_without_mutating_when_no_terminal_entry_is_evictable(
    tmp_path,
    monkeypatch,
):
    journal = CodexArchiveJournal(tmp_path)
    first_root = "root-" + "a" * 100
    journal.begin(
        "client-1",
        "archive-1",
        "primary",
        first_root,
        (first_root,),
        {first_root: False},
    )
    before = journal.path.read_bytes()
    monkeypatch.setattr(
        codex_archives_module,
        "_MAX_FILE_BYTES",
        len(before) + 16,
    )
    second_root = "root-" + "b" * 100

    with pytest.raises(CodexArchiveJournalError, match="capacity exhausted"):
        journal.begin(
            "client-2",
            "archive-2",
            "primary",
            second_root,
            (second_root,),
            {second_root: False},
        )

    assert journal.path.read_bytes() == before
    assert journal.get("client-1", "archive-1")["status"] == "intent"
    assert journal.get("client-2", "archive-2") is None
    restarted = CodexArchiveJournal(tmp_path)
    assert restarted.get("client-1", "archive-1")["status"] == "intent"


def test_byte_bound_batch_evicts_without_reserializing_whole_journal(
    tmp_path,
    monkeypatch,
):
    journal = CodexArchiveJournal(tmp_path)
    old_keys: list[tuple[str, str]] = []
    for index in range(40):
        client_id = f"client-{index}"
        cmd_id = f"archive-{index}"
        root = f"root-{index}-" + "x" * 80
        old_keys.append((client_id, cmd_id))
        journal.begin(
            client_id,
            cmd_id,
            "primary",
            root,
            (root,),
            {root: False},
        )
        journal.claim_submission(client_id, cmd_id)
        journal.reject(client_id, cmd_id, "internal", "y" * 200)

    original_size = journal.path.stat().st_size
    monkeypatch.setattr(
        codex_archives_module,
        "_MAX_FILE_BYTES",
        max(2048, original_size // 6),
    )
    whole_map_serializations = 0
    original_serializer = CodexArchiveJournal._serialized_bytes

    def count_whole_map_serialization(entries):
        nonlocal whole_map_serializations
        whole_map_serializations += 1
        return original_serializer(entries)

    monkeypatch.setattr(
        CodexArchiveJournal,
        "_serialized_bytes",
        staticmethod(count_whole_map_serialization),
    )

    journal.begin(
        "client-new",
        "archive-new",
        "primary",
        "root-new",
        ("root-new",),
        {"root-new": False},
    )

    assert whole_map_serializations == 0
    assert journal.path.stat().st_size <= codex_archives_module._MAX_FILE_BYTES
    assert journal.get("client-new", "archive-new")["status"] == "intent"
    assert sum(journal.get(*key) is None for key in old_keys) > 20
    restarted = CodexArchiveJournal(tmp_path)
    assert restarted.get("client-new", "archive-new")["status"] == "intent"


def test_terminal_transition_growth_batch_evicts_older_results(
    tmp_path,
    monkeypatch,
):
    journal = CodexArchiveJournal(tmp_path)
    old_keys: list[tuple[str, str]] = []
    for index in range(8):
        client_id = f"client-{index}"
        cmd_id = f"archive-{index}"
        root = f"root-{index}-" + "x" * 40
        old_keys.append((client_id, cmd_id))
        journal.begin(
            client_id,
            cmd_id,
            "primary",
            root,
            (root,),
            {root: False},
        )
        journal.claim_submission(client_id, cmd_id)
        journal.complete(client_id, cmd_id, (root,))

    journal.begin(
        "client-active",
        "archive-active",
        "primary",
        "root-active",
        ("root-active",),
        {"root-active": False},
    )
    journal.claim_submission("client-active", "archive-active")
    size_before_growth = journal.path.stat().st_size
    monkeypatch.setattr(
        codex_archives_module,
        "_MAX_FILE_BYTES",
        size_before_growth,
    )

    result = journal.reject(
        "client-active",
        "archive-active",
        "internal",
        "z" * 512,
    )

    assert result["status"] == "rejected"
    assert journal.path.stat().st_size <= size_before_growth
    assert journal.get("client-active", "archive-active")["status"] == "rejected"
    assert sum(journal.get(*key) is None for key in old_keys) >= 2
    restarted = CodexArchiveJournal(tmp_path)
    assert restarted.get("client-active", "archive-active")["status"] == "rejected"
