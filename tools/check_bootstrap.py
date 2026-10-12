"""Exercise packaged prefix initialization on a disposable Linux Wine prefix."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="ember-bootstrap-check-") as folder:
    temporary = Path(folder)
    prefix = temporary / "prefix"
    env = dict(os.environ, XDG_CONFIG_HOME=str(temporary/"config"), XDG_CACHE_HOME=str(temporary/"cache"), XDG_DATA_HOME=str(temporary/"data"))
    process = subprocess.Popen([str(root/"src-tauri/engine/stove-engine"),"--root",str(prefix/"drive_c/Smilegate")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    def request(action, payload=None):
        process.stdin.write(json.dumps({"action":action,"payload":payload})+"\n"); process.stdin.flush()
        return json.loads(process.stdout.readline())
    try:
        state = request("bootstrap", {"prefix":str(prefix),"runner":"wine"})
        deadline = time.monotonic()+420
        while state.get("job",{}).get("running") and time.monotonic()<deadline:
            time.sleep(.2);state=request("state")
        assert not state.get("error"), state.get("error")
        job = state.get("job")
        assert job and not job["running"] and not job.get("error"), job
        assert job["result"]["prefix_created"]
        assert state["fresh_destination"] and not state["game_installed"]
        assert (prefix/"system.reg").is_file()
        assert not (prefix/"drive_c/Smilegate/STOVE").exists()
        profile = next((temporary/"config/ember-launcher").glob("protocol-*.json"))
        assert set(json.loads(profile.read_text())) == {"profile_hash","caller_detail","version_suffix"}
        request("shutdown"); process.wait(timeout=15)
        print("Packaged engine: new Wine prefix + independent profile + fresh destination OK")
    finally:
        if process.poll() is None: process.kill(); process.wait()
