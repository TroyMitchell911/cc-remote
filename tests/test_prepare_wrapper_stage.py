"""Offline regression tests for legacy immutable Wrapper stage preparation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat

import pytest

from deploy import prepare_wrapper_stage as stage_module
from deploy.prepare_wrapper_stage import (
    MANIFEST_NAME,
    StagePreparationError,
    prepare_stage,
    select_uv_command,
)


def _release_tree(
    root: Path,
    *,
    lock: bytes,
    python_pin: str = "3.13.9",
    protocol: int = 45,
) -> Path:
    (root / "deploy").mkdir(parents=True)
    (root / "cc_remote").mkdir()
    (root / "web" / "dist").mkdir(parents=True)
    (root / "requirements-wrapper.lock").write_bytes(lock)
    (root / "deploy" / "python-version.txt").write_text(f"{python_pin}\n")
    (root / "deploy" / "uv-version.txt").write_text("0.11.16\n")
    (root / "cc_remote" / "protocol.py").write_text(
        f"PROTOCOL_VERSION = {protocol}\n"
    )
    (root / "web" / "dist" / "cc-remote-build.json").write_text(
        json.dumps({"version": "3.0.0", "protocol": protocol}) + "\n"
    )
    return root


def _validated(runtime: Path) -> dict[str, str]:
    return {
        "python_version": "3.13.9",
        "runtime": str(runtime),
        "product_version": "3.0.0",
    }


def test_identical_nono_stage_defers_venv_copy_and_binds_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    old = _release_tree(tmp_path / "old", lock=b"same-lock\n")
    stage = _release_tree(tmp_path / "stage", lock=b"same-lock\n")
    runtimes = tmp_path / "runtimes"
    runtimes.mkdir()
    monkeypatch.setattr(
        stage_module,
        "_validate_environment",
        lambda **_kwargs: _validated(runtimes),
    )

    payload = prepare_stage(
        old=old,
        stage=stage,
        runtimes=runtimes,
        release="release-20260901T000000Z-v45-test",
        explicit_uv=None,
        defer_identical=True,
    )

    assert payload["strategy"] == "reuse"
    assert payload["uv"] is None
    assert payload["requirements_lock_sha256"] == hashlib.sha256(
        b"same-lock\n"
    ).hexdigest()
    assert not (stage / ".venv").exists()
    manifest = json.loads((stage / "deploy" / MANIFEST_NAME).read_text())
    assert manifest == payload
    assert stat.S_IMODE((stage / "deploy" / MANIFEST_NAME).stat().st_mode) == 0o640


def test_changed_contract_builds_a_fresh_copy_linked_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    old = _release_tree(tmp_path / "old", lock=b"old-lock\n")
    stage = _release_tree(tmp_path / "stage", lock=b"new-lock\n")
    runtimes = tmp_path / "runtimes"
    runtimes.mkdir()

    def fake_build(**kwargs) -> str:
        venv = kwargs["stage"] / ".venv"
        (venv / "bin").mkdir(parents=True)
        python = venv / "bin" / "python"
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        return "0.11.16"

    monkeypatch.setattr(stage_module, "_build_venv", fake_build)
    monkeypatch.setattr(
        stage_module,
        "_validate_environment",
        lambda **_kwargs: _validated(runtimes),
    )

    payload = prepare_stage(
        old=old,
        stage=stage,
        runtimes=runtimes,
        release="release-20260901T000000Z-v45-test",
        explicit_uv=None,
        defer_identical=True,
    )

    assert payload["strategy"] == "rebuild"
    assert payload["uv"] == "0.11.16"
    assert stat.S_IMODE((stage / ".venv").stat().st_mode) == 0o750
    assert stat.S_IMODE((stage / ".venv" / "bin" / "python").stat().st_mode) == 0o750


def test_failed_rebuild_removes_only_the_new_incomplete_venv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    old = _release_tree(tmp_path / "old", lock=b"old-lock\n")
    stage = _release_tree(tmp_path / "stage", lock=b"new-lock\n")
    runtimes = tmp_path / "runtimes"
    runtimes.mkdir()

    def fake_build(**kwargs) -> str:
        (kwargs["stage"] / ".venv" / "bin").mkdir(parents=True)
        return "0.11.16"

    def fail_validation(**_kwargs) -> dict[str, str]:
        raise StagePreparationError("injected validation failure")

    monkeypatch.setattr(stage_module, "_build_venv", fake_build)
    monkeypatch.setattr(stage_module, "_validate_environment", fail_validation)

    with pytest.raises(StagePreparationError, match="injected validation failure"):
        prepare_stage(
            old=old,
            stage=stage,
            runtimes=runtimes,
            release="release-20260901T000000Z-v45-test",
            explicit_uv=None,
            defer_identical=False,
        )

    assert not (stage / ".venv").exists()
    assert not (stage / "deploy" / MANIFEST_NAME).exists()
    assert (stage / "requirements-wrapper.lock").read_bytes() == b"new-lock\n"


def test_uv_selection_prefers_an_exact_binary_without_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    old = tmp_path / "old"
    old.mkdir()
    first = tmp_path / "uv-old"
    second = tmp_path / "uv-pinned"
    first.touch(mode=0o755)
    second.touch(mode=0o755)
    monkeypatch.setattr(
        stage_module,
        "_candidate_uv_paths",
        lambda _explicit, _old: [first, second],
    )
    monkeypatch.setattr(
        stage_module,
        "_uv_version",
        lambda command, _environment: (
            "0.11.16" if command[0] == str(second) else "0.9.7"
        ),
    )

    command = select_uv_command(
        explicit=None,
        old=old,
        expected_version="0.11.16",
        environment={},
    )

    assert command == [str(second), "--no-config"]


def test_uv_selection_bootstraps_and_rechecks_the_repository_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    old = tmp_path / "old"
    old.mkdir()
    bootstrap = tmp_path / "uv-bootstrap"
    bootstrap.touch(mode=0o755)
    monkeypatch.setattr(
        stage_module,
        "_candidate_uv_paths",
        lambda _explicit, _old: [bootstrap],
    )
    monkeypatch.setattr(
        stage_module,
        "_uv_version",
        lambda command, _environment: (
            "0.11.16" if "tool" in command else "0.9.7"
        ),
    )

    command = select_uv_command(
        explicit=None,
        old=old,
        expected_version="0.11.16",
        environment={},
    )

    assert command == [
        str(bootstrap),
        "tool",
        "run",
        "--from",
        "uv==0.11.16",
        "uv",
        "--no-config",
    ]


def test_stage_preparation_rejects_relative_runtime_root_without_creating_it(
    tmp_path: Path,
):
    old = _release_tree(tmp_path / "old", lock=b"same\n")
    stage = _release_tree(tmp_path / "stage", lock=b"same\n")
    relative = Path("relative-runtime-that-must-not-be-created")

    with pytest.raises(StagePreparationError, match="absolute path"):
        prepare_stage(
            old=old,
            stage=stage,
            runtimes=relative,
            release="release-20260901T000000Z-v45-test",
            explicit_uv=None,
            defer_identical=True,
        )

    assert not relative.exists()


def test_stage_builder_uses_hashes_binary_wheels_and_copy_link_mode():
    source = (Path(__file__).parents[1] / "deploy" / "prepare_wrapper_stage.py").read_text()
    assert '"--require-hashes"' in source
    assert '"--only-binary=:all:"' in source
    assert '"--link-mode",\n            "copy"' in source
    assert '"--managed-python"' in source
    assert '"--relocatable"' in source
