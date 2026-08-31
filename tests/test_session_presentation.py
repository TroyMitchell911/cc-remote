"""Zero-token tests for durable cross-client presentation receipts."""
from __future__ import annotations

import json

import pytest

from cc_remote.wrapper.session_presentation import (
    SessionPresentationStore,
    SessionPresentationStoreError,
)


def test_completion_receipts_round_trip_and_reject_stale_acknowledgements(
    tmp_path,
):
    store = SessionPresentationStore(tmp_path)
    first = store.mark_completion("claude", "session-1", "turn-1")
    assert first.completion_unread is True
    assert first.completion_revision == 1

    # Re-emitting the same native terminal boundary is idempotent.
    assert store.mark_completion("claude", "session-1", "turn-1") == first
    second = store.mark_completion("claude", "session-1", "turn-2")
    assert second.completion_revision == 2
    assert second.completion_id == "turn-2"

    stale = store.acknowledge_completion("claude", "session-1", "turn-1")
    assert stale == second
    acknowledged = store.acknowledge_completion(
        "claude", "session-1", "turn-2")
    assert acknowledged.completion_unread is False
    assert acknowledged.completion_revision == 3
    assert SessionPresentationStore(tmp_path).get(
        "claude", "session-1") == acknowledged


def test_goal_dismissal_is_scoped_to_one_exact_generation(tmp_path):
    store = SessionPresentationStore(tmp_path)
    store.dismiss_goal("codex", "session-1", "goal-first")
    assert store.reconcile_goal("codex", "session-1", "goal-first") is True

    assert store.reconcile_goal(
        "codex", "session-1", "goal-replacement") is False
    assert store.get("codex", "session-1").dismissed_goal_id is None
    store.dismiss_goal("codex", "session-1", "goal-replacement")
    assert store.reconcile_goal("codex", "session-1", None) is False
    assert store.get("codex", "session-1").dismissed_goal_id is None


def test_session_presentation_rekeys_clears_and_deletes(tmp_path):
    store = SessionPresentationStore(tmp_path)
    store.mark_completion("codex", "tmp-session", "turn-1")
    store.dismiss_goal("codex", "tmp-session", "goal-1")
    store.move("codex", "tmp-session", "real-session")
    assert store.get("codex", "tmp-session").completion_id is None
    assert store.get("codex", "real-session").completion_id == "turn-1"
    assert store.get("codex", "real-session").dismissed_goal_id == "goal-1"

    cleared = store.clear_completion("codex", "real-session")
    assert cleared.completion_id is None
    assert cleared.completion_unread is False
    assert cleared.completion_revision == 2
    store.delete("codex", "real-session")
    assert SessionPresentationStore(tmp_path).get(
        "codex", "real-session"
    ).completion_revision == 0


def test_session_presentation_rejects_unknown_payload_fields(tmp_path):
    (tmp_path / "session-presentation.json").write_text(
        json.dumps({
            "version": 1,
            "sessions": {
                "session-1": {
                    "completion_id": "turn-1",
                    "completion_unread": True,
                    "completion_revision": 1,
                    "dismissed_goal_id": None,
                    "updated_at": 1,
                    "future_secret": "must-not-pass",
                },
            },
        }),
        encoding="utf-8",
    )
    with pytest.raises(SessionPresentationStoreError):
        SessionPresentationStore(tmp_path)


def test_session_presentation_rejects_inconsistent_profile_revisions(tmp_path):
    (tmp_path / "session-presentation.json").write_text(
        json.dumps({
            "version": 4,
            "profile_revision": 3,
            "profile_revisions": {"claude": 0, "codex": 4},
            "sessions": {},
        }),
        encoding="utf-8",
    )
    with pytest.raises(SessionPresentationStoreError, match="unreadable"):
        SessionPresentationStore(tmp_path)


def test_session_presentation_rejects_symlinks(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"sessions":{}}', encoding="utf-8")
    (tmp_path / "session-presentation.json").symlink_to(target)
    with pytest.raises(SessionPresentationStoreError):
        SessionPresentationStore(tmp_path)


def test_presentation_is_engine_scoped_and_v1_bare_ids_are_quarantined(
    tmp_path,
):
    path = tmp_path / "session-presentation.json"
    path.write_text(json.dumps({
        "version": 1,
        "sessions": {
            "ambiguous-native": {
                "completion_id": "turn-a",
                "completion_unread": True,
                "completion_revision": 1,
                "dismissed_goal_id": None,
                "updated_at": 1,
            },
            "old-profile@native": {
                "completion_id": "turn-c",
                "completion_unread": True,
                "completion_revision": 1,
                "dismissed_goal_id": None,
                "updated_at": 2,
            },
        },
    }), encoding="utf-8")

    store = SessionPresentationStore(tmp_path)
    assert store.get("claude", "ambiguous-native").completion_id is None
    assert store.get("codex", "ambiguous-native").completion_id is None
    assert store.legacy_ids() == frozenset({"ambiguous-native"})
    assert store.get("codex", "old-profile@native").completion_id == "turn-c"
    assert store.get("claude", "old-profile@native").completion_id is None

    claimed = store.claim_legacy("claude", "ambiguous-native")
    assert claimed is not None and claimed.completion_id == "turn-a"
    assert store.legacy_ids() == frozenset()
    restored = SessionPresentationStore(tmp_path)
    assert restored.get("claude", "ambiguous-native").completion_id == "turn-a"
    assert restored.get("codex", "ambiguous-native").completion_id is None
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 4


def test_claiming_legacy_does_not_replace_newer_scoped_receipt(tmp_path):
    path = tmp_path / "session-presentation.json"
    path.write_text(json.dumps({
        "version": 1,
        "sessions": {
            "same-id": {
                "completion_id": "old-turn",
                "completion_unread": True,
                "completion_revision": 1,
                "dismissed_goal_id": "old-goal",
                "updated_at": 1,
            },
        },
    }), encoding="utf-8")
    store = SessionPresentationStore(tmp_path)
    store.mark_completion("codex", "same-id", "new-turn")

    claimed = store.claim_legacy("codex", "same-id")

    assert claimed is not None and claimed.completion_id == "new-turn"
    assert store.get("codex", "same-id").completion_id == "new-turn"
    assert store.legacy_ids() == frozenset()


def test_claiming_legacy_can_target_a_profile_scoped_codex_wire_id(tmp_path):
    path = tmp_path / "session-presentation.json"
    path.write_text(json.dumps({
        "version": 1,
        "sessions": {
            "native-id": {
                "completion_id": "old-turn",
                "completion_unread": True,
                "completion_revision": 1,
                "dismissed_goal_id": None,
                "updated_at": 1,
            },
        },
    }), encoding="utf-8")
    store = SessionPresentationStore(tmp_path)

    store.claim_legacy("codex", "native-id", "primary@native-id")

    assert store.get("codex", "primary@native-id").completion_id == "old-turn"
    assert store.get("codex", "native-id").completion_id is None
    assert store.legacy_ids() == frozenset()


def test_v3_quarantine_survives_codex_profile_migration(tmp_path):
    path = tmp_path / "session-presentation.json"
    path.write_text(json.dumps({
        "version": 1,
        "sessions": {
            "ambiguous-id": {
                "completion_id": "turn-a",
                "completion_unread": True,
                "completion_revision": 1,
                "dismissed_goal_id": None,
                "updated_at": 1,
            },
        },
    }), encoding="utf-8")
    store = SessionPresentationStore(tmp_path)

    assert store.migrate_codex_profile_sessions(
        lambda sid: f"primary@{sid}", profile_revision=3,
    ) == 0

    restored = SessionPresentationStore(tmp_path)
    assert restored.legacy_ids() == frozenset({"ambiguous-id"})
    assert restored.get("codex", "primary@ambiguous-id").completion_id is None


def test_presentation_profile_migration_is_replay_safe(tmp_path):
    store = SessionPresentationStore(tmp_path)
    store.mark_completion("codex", "old@native", "turn-c")
    store.mark_completion("claude", "old@native", "turn-h")

    assert store.migrate_codex_profile_sessions(
        lambda sid: sid.replace("old@", "new@", 1),
        profile_revision=7,
    ) == 1
    assert store.migrate_codex_profile_sessions(
        lambda _sid: "must-not-run",
        profile_revision=7,
    ) == 0
    restored = SessionPresentationStore(tmp_path)
    assert restored.get("codex", "new@native").completion_id == "turn-c"
    assert restored.get("claude", "old@native").completion_id == "turn-h"
