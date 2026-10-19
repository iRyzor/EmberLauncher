"""Replay Win32 API failures in the production bridge, in a disposable Wine prefix."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def check():
    if sys.platform != "linux":
        raise SystemExit("This replay check requires Linux, Wine and MinGW")
    for tool in ("wine", "wineboot", "wineserver", "x86_64-w64-mingw32-gcc"):
        if not shutil.which(tool):
            raise SystemExit(f"Missing test prerequisite: {tool}")
    with tempfile.TemporaryDirectory(prefix="ember-network-replay-") as folder:
        folder = Path(folder).resolve()
        env = dict(os.environ, WINEPREFIX=str(folder / "prefix"), WINEDEBUG="-all")
        for key in ("WINESERVER", "WINELOADER", "WINEARCH", "WINEDLLOVERRIDES"):
            env.pop(key, None)
        executable = folder / "network-policy.exe"
        subprocess.run(["x86_64-w64-mingw32-gcc", "-std=c11", "-O2", "-Wall", "-Wextra",
                        "-Werror", str(ROOT / "tests/native/network_policy.c"), "-o", str(executable),
                        "-liphlpapi", "-ladvapi32"], check=True)
        try:
            subprocess.run(["wineboot", "-i"], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=True)
            # File-backed output avoids inherited Wine service handles holding pipes open.
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                result = subprocess.run(["wine", str(executable)], env=env, stdin=subprocess.DEVNULL,
                                        stdout=stdout, stderr=stderr, timeout=30)
                stdout.seek(0)
                stderr.seek(0)
                out, err = stdout.read(), stderr.read()
                assert result.returncode == 0 and out.strip() == b"NETWORK_POLICY_TESTS_OK", (result.returncode, out, err)
        finally:
            subprocess.run(["wineserver", "-k"], env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    print(json.dumps({"api_recovery_replay": True, "disabled_service_blocks_launch": True,
                      "native_windows_skips_recovery": True, "account_accessed": False}))


if __name__ == "__main__":
    check()
