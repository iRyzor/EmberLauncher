"""Mutual exclusion shared by this launcher's startup and update paths."""
import json
from .platform_fs import os, fcntl
from pathlib import Path


def acquire(game):
    root = os.open(game,os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock = None
    try:
        lock = os.open('.stove-update.lock',os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,0o600,dir_fd=root)
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = Path(game)/'.stove-update-transaction'
        if journal.exists() or journal.is_symlink():
            if journal.is_symlink():raise ValueError('Unsafe pending transaction')
            with (journal/'journal.json').open('rb') as stream:
                raw=stream.read(8*1024*1024+1)
            if len(raw)>8*1024*1024 or json.loads(raw).get('phase') not in ('committed','files_verified','rolled_back'):
                raise ValueError('An update needs recovery before game startup')
        return lock
    except BaseException:
        if lock is not None:os.close(lock)
        raise
    finally:os.close(root)
