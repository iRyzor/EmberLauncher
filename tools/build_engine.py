"""Build this host's standalone production Rust engine."""
from pathlib import Path
import shutil
import os
import subprocess
import sys
root = Path(__file__).resolve().parents[1]
subprocess.run(["cargo", "build", "--release", "--locked", "--manifest-path", "engine/rust/Cargo.toml"], cwd=root, check=True)

executable = "stove-engine.exe" if sys.platform == "win32" else "stove-engine"
destination = root / "src-tauri/engine" / executable
destination.parent.mkdir(parents=True, exist_ok=True)
temporary = destination.with_name(destination.name + ".new")
shutil.copy2(root / "engine/rust/target/release" / ("ember-engine.exe" if sys.platform == "win32" else "ember-engine"), temporary)
os.replace(temporary, destination)
