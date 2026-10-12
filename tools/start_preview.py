"""Start local review UI and private backend; terminate only children we own."""
import os
import argparse
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--root", help="Explicit library; omit to use saved settings")
args = parser.parse_args()
key = secrets.token_urlsafe(24)
env = dict(os.environ, EMBER_PREVIEW_KEY=key)
backend = subprocess.Popen([str(root / "src-tauri/engine/stove-engine"), "--http", "5179", "--key", key, "--helper", str(root / "engine/native/pipe-bridge.exe"), *(["--root", args.root] if args.root else [])], cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
frontend = subprocess.Popen(["npm", "run", "dev"], cwd=root, env=env, start_new_session=True)
stopping = False
def stop(*_):
    global stopping
    stopping = True
    if frontend.poll() is None: os.killpg(frontend.pid, signal.SIGTERM)
    if backend.poll() is None: backend.terminate()
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
try:
    while not stopping and frontend.poll() is None:
        time.sleep(0.1)
finally:
    stop()
    for child in (frontend, backend):
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if child is frontend: os.killpg(child.pid, signal.SIGKILL)
            else: child.kill()
            child.wait()
