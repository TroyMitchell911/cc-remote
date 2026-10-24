"""Stage a compatibility repair for the legacy macOS Electron prototype.

Only the known bundled-Web handler is replaced. Native browser integrations,
permissions, cookies and connection settings keep their existing ownership.
This stages a separate signed app; it never stops or replaces a running app.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys


REMOTE_UI = """async function serveAssets(browserSession) {
  // Keep HTTP provenance handling, but always load the configured Relay's UI.
  // Bundled pages and their build manifest cannot recover after protocol upgrades.
  if (origin) await browserSession.clearStorageData({
    origin, storages: ['serviceworkers', 'cachestorage'],
  });
  const handler = request => forwardShellRequest(request, browserSession, origin);
  browserSession.protocol.handle('https', handler);
  browserSession.protocol.handle('http', handler);
}
"""


def repaired_source(source: str) -> str:
    start = source.index("async function serveAssets(")
    end = source.index("\nasync function listen()", start)
    legacy = source[start:end]
    # Require the complete known handler, not a loose search-and-replace in an
    # arbitrary app. Unknown app revisions need a source-level review first.
    expected = Path(__file__).with_name("desktop_legacy_ui.txt").read_text()
    if legacy == REMOTE_UI:
        raise ValueError("Desktop already loads the Relay UI")
    if legacy != expected:
        raise ValueError("Unknown Desktop asset handler; refusing to patch")
    return source[:start] + REMOTE_UI + source[end:]


def stage(app: Path, destination: Path) -> dict[str, str]:
    app = app.resolve(strict=True)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Stage destination already exists")
    if app in destination.resolve().parents:
        raise ValueError("Stage must be outside the installed app")
    info = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    if info.get("CFBundleIdentifier") != "cc.remote.desktop":
        raise ValueError("Not the cc-remote Desktop prototype")
    relative = Path("Contents/Resources/app/src/main.mjs")
    original = (app / relative).read_bytes()
    patched = repaired_source(original.decode())
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
    shutil.copytree(app, destination, symlinks=True)
    (destination / relative).write_text(patched)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(destination)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(destination)], check=True)
    return {
        "app": str(app), "stage": str(destination), "ui_source": "configured-relay",
        "original_main_sha256": hashlib.sha256(original).hexdigest(),
        "staged_main_sha256": hashlib.sha256(patched.encode()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--stage", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This repair is only for the macOS Desktop prototype")
    print(json.dumps(stage(args.app, args.stage), indent=2))


if __name__ == "__main__":
    main()
