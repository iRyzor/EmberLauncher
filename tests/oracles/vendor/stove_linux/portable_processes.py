"""Process discovery for native Windows and Wine on macOS."""

import os
from pathlib import Path
import sys
import time

import psutil
from . import native_process

EXCLUDED = (
    "wineserver",
    "winedevice",
    "explorer.exe",
    "services.exe",
    "crashpad_handler.exe",
)


def matches(installation, process):
    try:
        if process.username() != psutil.Process().username():
            return False
        command = process.cmdline()
        executable = process.exe()
        name = Path(executable).name.casefold()
        if native_process.excluded(name.encode("utf-8")):
            return False
        game = (
            str(installation.game_dir.resolve())
            .replace("\\", "/")
            .casefold()
            .rstrip("/")
            + "/"
        )
        paths = [executable, command[0] if command else ""]
        if sys.platform == "darwin" and name in ("wine", "wine64", "wine-preloader", "wine64-preloader") and len(command) > 1:
            paths.append(command[1])
        if sys.platform != "win32":
            if process.environ().get("WINEPREFIX") != str(
                installation.prefix.resolve()
            ):
                return False
            relative = installation.game_dir.relative_to(
                installation.prefix / "drive_c"
            )
            windows = "c:/" + relative.as_posix().casefold() + "/"
        else:
            windows = game
        return any(native_process.path_match(
            game.encode("utf-8"), windows.encode("utf-8"),
            p.replace("\\", "/").casefold().encode("utf-8"),
        ) for p in paths)

    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return False


def require_idle(installation):
    for process in psutil.process_iter():
        if matches(installation, process):
            raise ValueError("Game files are in use")
        name = ""
        try:
            name = process.name().casefold()
            names = {name}
            if sys.platform != "win32" and name in ("wine", "wine64", "wine-preloader", "wine64-preloader"):
                names.update(value.replace("\\", "/").rsplit("/", 1)[-1].casefold() for value in process.cmdline()[:2])
            if (
                names.intersection(("stove.exe", "stoveupdater.exe"))
                and process.username() == psutil.Process().username()
            ):
                if sys.platform == "win32" or process.environ().get(
                    "WINEPREFIX"
                ) == str(installation.prefix.resolve()):
                    raise ValueError("Native launcher is still running")
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            if any(value in name for value in ("chaos", "ssr-stove", "stove.exe")):
                raise ValueError(
                    "Cannot establish whether game files are in use"
                ) from None


class PortableGameProcesses:
    def __init__(self, installation):
        self.installation = installation
        self.baseline = {}
        for process in psutil.process_iter():
            if matches(installation, process):
                try:
                    self.baseline[process.pid] = process.create_time()
                except psutil.NoSuchProcess:
                    pass
        self.owned = {}
        self.seen = False
        self.empty_since = None

    def refresh(self):
        handles = dict(self.owned)
        owned, current, alive = {}, {}, {}
        for pid, process in self.owned.items():
            try:
                owned[pid] = process.create_time()
            except psutil.NoSuchProcess:
                pass
        for process in psutil.process_iter():
            if matches(self.installation, process):
                try:
                    current[process.pid] = process.create_time()
                    handles[process.pid] = process
                except psutil.NoSuchProcess:
                    pass
        for pid, process in handles.items():
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    alive[pid] = process.create_time()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        result = native_process.lifecycle(self.baseline, owned, current, alive,
                                          self.seen, self.empty_since, time.monotonic())
        self.owned = {pid:handles[pid] for pid in result['owned']}
        self.seen = result['seen']
        self.empty_since = result['empty_since']
        self._finished = result['finished']
        return result['running']

    def finished(self):
        self.refresh()
        return self._finished

    def stop(self, force=False, timeout=8):
        self.refresh()
        for process in list(self.owned.values()):
            try:
                if sys.platform == "win32" and not force:
                    close_windows(process)
                elif force:
                    process.kill()
                else:
                    process.terminate()
            except psutil.NoSuchProcess:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.refresh():
                return {"closed": True, "force_required": False}
            time.sleep(0.2)
        return {"closed": False, "force_required": True}


def close_windows(process):
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, process.pid)
    if not handle:
        raise psutil.NoSuchProcess(process.pid)
    try:
        if not process.is_running():
            raise psutil.NoSuchProcess(process.pid)
        user = ctypes.WinDLL("user32", use_last_error=True)
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user.EnumWindows.argtypes = [callback, wintypes.LPARAM]

        def visit(window, _):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(window, ctypes.byref(pid))
            if pid.value == process.pid:
                user.PostMessageW(window, 0x10, 0, 0)
            return True

        user.EnumWindows(callback(visit), 0)
    finally:
        kernel.CloseHandle(handle)
