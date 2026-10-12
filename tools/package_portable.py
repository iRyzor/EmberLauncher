"""Collect compiled binaries into a standalone local distribution."""

from pathlib import Path
import shutil
import tarfile
import os
import json
import sys
import zipfile

if sys.platform not in ("linux", "win32"):
    raise SystemExit("Portable archives are supported on Linux and Windows")
windows = sys.platform == "win32"
launcher_name = "EmberLauncher.exe" if windows else "ember-launcher"
engine_name = "stove-engine.exe" if windows else "stove-engine"

root = Path(__file__).resolve().parents[1]
folder = root / "release/EmberLauncher"
for relative in ("engine", "native"):
    (folder / relative).mkdir(parents=True, exist_ok=True)


def replace_copy(source, destination):
    temporary = destination.with_name(destination.name + ".new")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)


replace_copy(
    root / "src-tauri/target/release" / ("ember-launcher.exe" if windows else "ember-launcher"),
    folder / launcher_name
)
replace_copy(root / "src-tauri/engine" / engine_name, folder / "engine" / engine_name)
replace_copy(root / "engine/native/pipe-bridge.exe", folder / "native/pipe-bridge.exe")
replace_copy(root / "engine/native/webview-probe.exe", folder / "native/webview-probe.exe")
public_files = ["README.md", "CONTRIBUTING.md", "SECURITY.md"]
for name in public_files:
    replace_copy(root / name, folder / name)
(folder / "docs").mkdir(exist_ok=True)
public_docs = ["COMPATIBILITY.md", "PRIVACY.md", "BUILDING.md"]
for document in public_docs:
    replace_copy(root / "docs" / document, folder / "docs" / document)
(folder / "docs/screenshots").mkdir(exist_ok=True)
public_images = ["game.png", "library.png"]
for image in public_images:
    replace_copy(root / "docs/screenshots" / image, folder / "docs/screenshots" / image)
(folder / "public").mkdir(exist_ok=True)
replace_copy(root / "public/ember.svg", folder / "public/ember.svg")
version = json.loads((root / "package.json").read_text())["version"]
# Use an explicit allowlist; reused staging folders may contain private files.
public_paths = [launcher_name, "engine/" + engine_name,
                "native/pipe-bridge.exe", "native/webview-probe.exe", *public_files]
public_paths += ["public/ember.svg"]
public_paths += ["docs/" + name for name in public_docs]
public_paths += ["docs/screenshots/" + name for name in public_images]
if windows:
    with zipfile.ZipFile(root / f"release/EmberLauncher-{version}-windows-x64.zip",
                         "w", zipfile.ZIP_DEFLATED) as archive:
        for relative in public_paths:
            archive.write(folder / relative, arcname="EmberLauncher/" + relative)
else:
    with tarfile.open(root / f"release/EmberLauncher-{version}-linux-x86_64.tar.gz", "w:gz") as archive:
        for relative in public_paths:
            archive.add(folder / relative, arcname="EmberLauncher/" + relative)
print(folder)
