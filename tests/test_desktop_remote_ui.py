from pathlib import Path
import plistlib
from unittest.mock import patch

import pytest

from deploy.desktop_remote_ui import repaired_source, stage


LEGACY = Path(__file__).parents[1] / "deploy/desktop_legacy_ui.txt"


def source():
    return "// native bridge stays here\n" + LEGACY.read_text() + "\nasync function listen() {}\n"


def test_remote_ui_repair_preserves_native_code_and_rejects_unknown_revisions():
    result = repaired_source(source())
    assert result.startswith("// native bridge stays here\n")
    assert result.endswith("\nasync function listen() {}\n")
    assert "forwardShellRequest(request, browserSession, origin)" in result
    assert "readFile(file)" not in result
    with pytest.raises(ValueError, match="already loads"):
        repaired_source(result)
    with pytest.raises(ValueError, match="Unknown"):
        repaired_source(source().replace("assets.get(url.pathname)", "otherHandler(url)"))


def test_stage_preserves_installed_app_and_never_replaces_an_existing_stage(tmp_path):
    app = tmp_path / "installed.app"
    main = app / "Contents/Resources/app/src/main.mjs"
    main.parent.mkdir(parents=True)
    main.write_text(source())
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "cc.remote.desktop"}))
    native = main.with_name("request-forwarding.mjs")
    native.write_text("native origin checks")
    destination = tmp_path / "staged.app"
    with patch("deploy.desktop_remote_ui.subprocess.run") as sign:
        result = stage(app, destination)
    assert sign.call_count == 3
    assert main.read_text() == source()
    assert (destination / native.relative_to(app)).read_text() == native.read_text()
    assert result["original_main_sha256"] != result["staged_main_sha256"]
    with pytest.raises(ValueError, match="already exists"):
        stage(app, destination)
    with pytest.raises(ValueError, match="outside"):
        stage(app, app / "nested.app")


def test_stage_rejects_a_different_app_before_copying(tmp_path):
    app = tmp_path / "another.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "another.app"}))
    with pytest.raises(ValueError, match="Not the cc-remote"):
        stage(app, tmp_path / "stage.app")
    assert not (tmp_path / "stage.app").exists()
