"""Build the protocol ABI used exclusively by independent Python tests."""
from pathlib import Path
import shutil
import subprocess
import sys
root = Path(__file__).resolve().parents[1]
subprocess.run(["cargo", "build", "--release", "--locked", "--manifest-path", "engine/native-protocol/Cargo.toml"], cwd=root, check=True)
name = {"win32": "ember_protocol.dll", "darwin": "libember_protocol.dylib"}.get(sys.platform, "libember_protocol.so")
destination = root / "tests/oracles/runtime"
destination.mkdir(exist_ok=True)
shutil.copy2(root / "engine/native-protocol/target/release" / name, destination / name)
