"""Conservative Linux process checks before touching a Wine installation."""
import os
from pathlib import Path


def require_idle(installation, proc_root=Path('/proc')):
    import sys
    if sys.platform != "linux":
        from .portable_processes import require_idle as portable_idle
        return portable_idle(installation)
    game = installation.game_dir.resolve()
    prefix = installation.prefix.resolve()
    for process in proc_root.iterdir():
        if not process.name.isdigit():continue
        try:
            if process.stat().st_uid != os.getuid():continue
            command = (process/'cmdline').read_bytes().lower()
            names = (b'chaoszeronightmare', b'ssr-stove', b'stoveupdater', b'stove.exe')
            candidate = any(name in command for name in names)
            if candidate:
                environment = (process/'environ').read_bytes().split(b'\0')
                same_prefix = b'WINEPREFIX=' + os.fsencode(prefix) in environment
            else:
                same_prefix = False
            if same_prefix and candidate:
                raise ValueError('Game or native launcher is still running')
            # A mapped game binary also detects launches without WINEPREFIX.
            try:maps = (process/'maps').read_bytes()
            except PermissionError:
                if candidate:raise
                continue
            if os.fsencode(game) + b'/' in maps:
                raise ValueError('Game files are in use')
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise ValueError('Cannot establish whether game files are in use') from None
