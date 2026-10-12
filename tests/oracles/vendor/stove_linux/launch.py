"""Start the installed game and keep its launcher IPC connection alive."""

import hashlib
import os
from pathlib import Path
import select
import shutil
import subprocess
import threading
import time
import sys
from .pipe_io import PipeReader, write_windows

from .game_auth import GAME_ID, GameSession
from .auth import Region
from .ipc import LauncherHandshake
from .protocol import Decoder

SDK_HASH = "ab7a50b44ca863dea1b4f0e9186175a53dddc5f48fac2b701031a8be0bd1a121"
PIPE = r"\\.\pipe\15BADE76-0E87-422B-A75B-B2049FE5F5F5" + "\\" + GAME_ID


def required_info(session, region=None):
    if not isinstance(session, GameSession) or session.game_id != GAME_ID:
        raise ValueError("A session for the supported game is required")
    # Only fields with proven names and known source values.
    region = region or Region()
    if not isinstance(region, Region):
        raise ValueError("A validated authentication region is required")
    result = {
        "game_id": session.game_id,
        "access_token": session.tokens.access_token,
        "refresh_token": session.tokens.refresh_token,
        "env": "live",
        "gds_isdefault_yn": "n",
        "gds_nation": region.nation,
        "gds_regulation": region.regulation,
        "gds_timezone": region.timezone,
        "gds_utcoffset": str(region.utc_offset),
        "language": region.lang,
    }
    # GameChecker model 0x49f28..0x49f86 adds expires_in to
    # _Xtime_get_ticks()/10000: both are milliseconds. SDK 0xa8c4e
    # adds expire_time to the Unix epoch through AddMilliseconds (0x1b7220).
    # Keep the original receipt time so waiting before launch never extends it.
    if session.tokens.expires_in is not None:
        result["expire_time"] = str(session.received_at_ms + session.tokens.expires_in)
    if session.tokens.expire_in is not None:
        result["expire_in"] = str(session.tokens.expire_in)
    # GameChecker user.user_id -> model +0x260 (0x4a144). The launcher
    # getter 0x6fa700 feeds its REQUIRED_INFO guid at 0x6e04cc.
    if session.user_id:
        result["guid"] = session.user_id
    if session.tokens.member_no is not None:
        result["member_no"] = str(session.tokens.member_no)
    return result


class GameLauncher:
    def __init__(self, installation, runner="wine", helper=None, region=None):
        self.installation = installation
        self.region = region or Region()
        self.runner = None if sys.platform == "win32" else shutil.which(runner)
        self.helper = (
            Path(helper)
            if helper
            else Path(__file__).resolve().parents[4] / "engine/native/pipe-bridge.exe"
        )
        if (sys.platform != "win32" and not self.runner) or not self.helper.is_file():
            raise ValueError("Wine runner and compiled bridge are required")
        sdk = installation.game_dir / "bin/BaseSDK.dll"
        with sdk.open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != SDK_HASH:
                raise ValueError("The installed SDK needs a new IPC profile")
        installation.executable  # Resolve before authenticating.
        # No configuration is replaced. This version supports the SDK fallback
        # namespace verified by the native oracle in the absence of a config.
        users = (
            [Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "../.."]
            if sys.platform == "win32"
            else (installation.prefix / "drive_c/users").glob("*")
        )
        for user in users:
            local = user / "AppData/Local"
            if any(local.glob("STOVEPCSDK3*/StoveConfigs/launcherData.json")):
                raise ValueError(
                    "An existing launcher pipe configuration needs separate resolution"
                )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._game = None
        self._phase = "idle"
        self._detail = ""
        self._heartbeats = 0
        self._installation_lock = None

    def state(self):
        with self._lock:
            return {
                "phase": self._phase,
                "detail": self._detail,
                "heartbeats": self._heartbeats,
            }

    def _status(self, phase, detail, heartbeats=0):
        with self._lock:
            self._phase, self._detail, self._heartbeats = phase, detail, heartbeats

    def start(self, session):
        handshake = LauncherHandshake(required_info(session, self.region))
        with self._lock:
            if self._thread is not None:
                handshake.close()
                raise ValueError("This launcher already attempted game startup")
            from .installation_lock import acquire

            try:
                self._installation_lock = acquire(self.installation.game_dir)
                with (self.installation.game_dir / "bin/BaseSDK.dll").open(
                    "rb"
                ) as file:
                    if hashlib.file_digest(file, "sha256").hexdigest() != SDK_HASH:
                        raise ValueError("The installed SDK needs a new IPC profile")
            except BaseException:
                if self._installation_lock is not None:
                    os.close(self._installation_lock)
                    self._installation_lock = None
                handshake.close()
                raise
            self._phase = "launch_preparing"
            self._detail = "Preparando el canal local del SDK."
            self._thread = threading.Thread(
                target=self._run, args=(handshake,), daemon=True
            )
            try:
                self._thread.start()
            except BaseException:
                os.close(self._installation_lock)
                self._installation_lock = None
                self._thread = None
                handshake.close()
                raise

    def _run(self, handshake):
        bridge = None
        decoder = None
        readers = []
        try:
            env = (
                dict(os.environ)
                if sys.platform == "win32"
                else dict(
                    os.environ,
                    WINEPREFIX=str(self.installation.prefix),
                    WINEDEBUG="-all",
                )
            )
            bridge_command = ([self.runner] if self.runner else []) + [
                str(self.helper.resolve()),
                PIPE,
                "300",
            ]
            bridge = subprocess.Popen(
                bridge_command,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            if sys.platform == "win32":
                stderr_reader = PipeReader(bridge.stderr, 512)
                stdout_reader = PipeReader(bridge.stdout)
                readers = [stderr_reader, stdout_reader]
            deadline = time.monotonic() + 45
            diagnostics = bytearray()
            while b"PIPE_READY\n" not in diagnostics.replace(b"\r", b""):
                if self._stop.is_set() or time.monotonic() >= deadline:
                    raise RuntimeError("Bridge startup ended")
                if sys.platform == "win32":
                    data = stderr_reader.read()
                    if data is None:
                        continue
                else:
                    if not select.select([bridge.stderr], [], [], 0.5)[0]:
                        continue
                    data = os.read(bridge.stderr.fileno(), 512)
                if not data or len(diagnostics) + len(data) > 8192:
                    raise RuntimeError("Bridge is unavailable")
                diagnostics.extend(data)
            # Account values are absent from argv, environment and files.
            if self._stop.is_set():
                return
            game_command = ([self.runner] if self.runner else []) + [
                self.installation.windows_path(self.installation.executable),
                r"bin\ssr-stove-shield.exe",
            ]
            self._game = subprocess.Popen(
                game_command,
                cwd=self.installation.game_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._status(
                "launch_waiting_sdk",
                "El juego se inició. Esperando la conexión de su SDK.",
            )
            decoder = Decoder()
            deadline = time.monotonic() + 180
            while not self._stop.is_set():
                self._game.poll()  # Reap loader exit; child game may still be running.
                if time.monotonic() >= deadline:
                    self._status(
                        "launch_error",
                        "El SDK no mantuvo la conexión dentro del tiempo de prueba.",
                    )
                    return
                if sys.platform == "win32":
                    data = stdout_reader.read()
                    if data is None:
                        continue
                else:
                    if not select.select([bridge.stdout], [], [], 0.5)[0]:
                        continue
                    data = os.read(bridge.stdout.fileno(), 65535)
                if not data:
                    decoder.finish()
                    self._status(
                        "launch_closed",
                        "Se cerró la conexión del SDK. Comprueba el resultado en la ventana del juego.",
                    )
                    return
                for packet in decoder.feed(data):
                    for response in handshake.receive(packet):
                        frame = response.encode()
                        if sys.platform == "win32":
                            write_windows(bridge.stdin, frame, self._stop)
                            continue
                        offset = 0
                        while offset < len(frame):
                            # Anonymous pipe backpressure must not block shutdown
                            # indefinitely if the Wine bridge stops consuming.
                            os.set_blocking(bridge.stdin.fileno(), False)
                            if (
                                self._stop.is_set()
                                or not select.select([], [bridge.stdin], [], 5)[1]
                            ):
                                raise RuntimeError("Bridge write timed out")
                            try:
                                offset += os.write(
                                    bridge.stdin.fileno(), frame[offset:]
                                )
                            except BlockingIOError:
                                continue
                    if handshake.phase == "information_sent":
                        deadline = time.monotonic() + 300
                        self._status(
                            "launch_info_sent",
                            "Los datos de sesión se enviaron al SDK. Comprueba si puedes entrar al juego.",
                            handshake.heartbeats,
                        )
        except Exception:
            # External exceptions may contain packet/account values: fixed text.
            self._status(
                "launch_error",
                "Falló el arranque o el intercambio local con el SDK. Los tokens no se muestran.",
            )
        finally:
            if decoder is not None:
                decoder.close()
            handshake.close()
            if bridge is not None:
                if bridge.poll() is None:
                    bridge.kill()
                try:
                    bridge.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                for stream in (bridge.stdin, bridge.stdout, bridge.stderr):
                    if stream is not None:
                        stream.close()
            for reader in readers:
                reader.close()
            if self._installation_lock is not None:
                os.close(self._installation_lock)
                self._installation_lock = None
            # Never kill wineserver or other processes in the user's game prefix.

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
