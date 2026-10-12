"""Track this launch's game processes; never signal a Wine server or prefix."""

import os
import ctypes
from pathlib import Path
import signal
from stove_linux import native_process
import time


def open_process(pid):
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    library = ctypes.CDLL(None, use_errno=True)
    function = library.pidfd_open
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    descriptor = function(pid, 0)
    if descriptor < 0:
        raise OSError(ctypes.get_errno(), "Cannot open the game process")
    return descriptor


def send_signal(descriptor, number):
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(descriptor, number)
        return
    library = ctypes.CDLL(None, use_errno=True)
    function = library.pidfd_send_signal
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(descriptor, number, None, 0) != 0:
        raise OSError(ctypes.get_errno(), "Cannot signal the game process")


def identity(pid, proc=Path("/proc")):
    raw = (proc / str(pid) / "stat").read_text()
    fields = raw[raw.rfind(")") + 2 :].split()
    return int(fields[19]), fields[0]  # starttime and state (Linux proc stat 22,3)


class GameProcesses:
    def __init__(self, installation, proc=Path("/proc")):
        self.installation = installation
        self.proc = proc
        self.baseline = self.scan()
        self.owned = {}
        self.seen = False
        self.started = time.monotonic()
        self.empty_since = None

    def scan(self):
        found = {}
        game = os.fsencode(self.installation.game_dir.resolve()) + b"/"
        prefix = b"WINEPREFIX=" + os.fsencode(self.installation.prefix.resolve())
        try:
            relative = self.installation.game_dir.resolve().relative_to(
                self.installation.prefix.resolve() / "drive_c"
            )
            windows_game = ("c:/" + relative.as_posix() + "/").casefold()
        except ValueError:
            windows_game = None
        for entry in self.proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                environment = (entry / "environ").read_bytes().split(b"\0")
                if prefix not in environment:
                    continue
                command = (entry / "cmdline").read_bytes().lower()
                # Never select shared Wine infrastructure, even if it descends
                # from the loader that started the game.
                name = command.split(b"\0", 1)[0].rsplit(b"/", 1)[-1]
                if native_process.excluded(name):
                    continue
                maps = (entry / "maps").read_bytes()
                executable_mapping = native_process.mapping_match(game, maps)
                # Wine can unmap the PE file after loading its executable pages.
                # Match argv[0], never arguments mentioning a game file, and
                # require an exact directory boundary within this prefix.
                executable = (
                    command.split(b"\0", 1)[0]
                    .decode(errors="replace")
                    .replace("\\", "/")
                    .casefold()
                )
                native_game = str(self.installation.game_dir.resolve()).casefold() + "/"
                game_command = native_process.path_match(
                    native_game.encode("utf-8"),
                    b"" if windows_game is None else windows_game.encode("utf-8"),
                    executable.encode("utf-8"),
                )
                if not executable_mapping and not game_command:
                    continue
                pid = int(entry.name)
                start, state = identity(pid, self.proc)
                if state != "Z":
                    found[pid] = start
            except (OSError, ValueError):
                continue
        return found

    def refresh(self):
        current = self.scan()
        alive = {}
        for pid in self.owned.keys() | current.keys():
            try:
                start, state = identity(pid, self.proc)
                if state != "Z":
                    alive[pid] = start
            except (OSError, ValueError):
                pass
        result = native_process.lifecycle(self.baseline, self.owned, current, alive,
                                          self.seen, self.empty_since, time.monotonic())
        self.owned = result['owned']
        self.seen = result['seen']
        self.empty_since = result['empty_since']
        self._finished = result['finished']
        return result['running']

    def finished(self):
        self.refresh()
        return self._finished

    def signal_owned(self, number):
        self.refresh()
        for pid, start in list(self.owned.items()):
            descriptor = None
            try:
                # pidfd pins the identity, avoiding signals to a recycled PID.
                descriptor = open_process(pid)
                actual, state = identity(pid, self.proc)
                if actual == start and state != "Z":
                    send_signal(descriptor, number)
            except ProcessLookupError:
                pass
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def stop(self, force=False, timeout=8):
        self.signal_owned(signal.SIGKILL if force else signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.refresh():
                return {"closed": True, "force_required": False}
            time.sleep(0.2)
        return {"closed": False, "force_required": True}


import sys
if sys.platform != "linux":
    from stove_linux.portable_processes import PortableGameProcesses as GameProcesses
