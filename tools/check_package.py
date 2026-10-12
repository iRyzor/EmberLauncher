"""Smoke-test bundled engine without authentication, game startup, or real settings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
root = Path(__file__).resolve().parents[1]
name = "stove-engine.exe" if sys.platform == "win32" else "stove-engine"
with tempfile.TemporaryDirectory(prefix="ember-package-check-") as folder:
    temporary = Path(folder)
    env = dict(os.environ)
    for name_env in ["XDG_CONFIG_HOME","XDG_CACHE_HOME","XDG_DATA_HOME","LOCALAPPDATA","APPDATA"]:
        env[name_env] = str(temporary / name_env.lower())
    for kind in ("CONFIG","CACHE","DATA"):
        env[f"EMBER_{kind}_DIR"] = str(temporary / kind.lower())
    result = subprocess.run([str(root / "src-tauri/engine" / name), "--root", str(temporary / "missing-library"), "--helper", str(root / "engine/native/pipe-bridge.exe")], input='{"action":"state"}\n{"action":"shutdown"}\n', text=True, capture_output=True, env=env, timeout=30)
    if result.returncode:
        raise RuntimeError("Packaged engine did not start")
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses) == 2
    assert responses[0]["connected"] is False
    assert responses[0]["game_installed"] is False
    assert responses[0]["platform"] == sys.platform
    assert not (temporary / "missing-library").exists()
print("Bundled engine state/shutdown: OK; no account or game accessed")
