"""Verified gzip package staging; never writes to the game installation."""
import gzip
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.request import build_opener

from .manifest import NoRedirect
from .updates import GAME_CDN
from .game_auth import GAME_ID


def package_url(item):
    build, chunk, start = item['build'], item['chunk'], item['startpos']
    if any(type(v) is not int or v < 0 for v in (build, chunk, start)):
        raise ValueError('Invalid package coordinates')
    suffix = f'v{start}/{build}_{chunk}.gz' if start > build else f'v{build}/{chunk}.gz'
    return f'https://{GAME_CDN}/game/dpms_{GAME_ID}/{suffix}'


def stage_package(item, directory, opener=None, progress=None):
    """Unique private staging directory, bounded download and decompression."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError('Unsafe staging directory')
    work = Path(tempfile.mkdtemp(prefix='package-', dir=directory))
    os.chmod(work, 0o700)
    packed, output = work / 'download.gz', work / 'verified.bin'
    try:
        digest = hashlib.md5(usedforsecurity=False)
        total = 0
        opener = opener or build_opener(NoRedirect())
        with opener.open(package_url(item), timeout=30) as response, packed.open('xb') as target:
            if response.status != 200:
                raise ValueError('Unexpected package response')
            while data := response.read(min(1024 * 1024, item['packed_size'] - total + 1)):
                total += len(data)
                if total > item['packed_size']:
                    raise ValueError('Package larger than manifest')
                digest.update(data)
                target.write(data)
                if progress:
                    progress(total, item['packed_size'])
        if total != item['packed_size'] or digest.hexdigest() != item['packed_md5']:
            raise ValueError('Package integrity mismatch')
        digest = hashlib.md5(usedforsecurity=False)
        total = 0
        with gzip.open(packed, 'rb') as source, output.open('xb') as target:
            while data := source.read(min(1024 * 1024, item['size'] - total + 1)):
                total += len(data)
                if total > item['size']:
                    raise ValueError('Expanded package larger than manifest')
                digest.update(data)
                target.write(data)
            target.flush()
            os.fsync(target.fileno())
        if total != item['size'] or digest.hexdigest() != item['md5']:
            raise ValueError('Expanded package integrity mismatch')
        packed.unlink()
        return output
    except BaseException:
        for path in (packed, output):
            path.unlink(missing_ok=True)
        work.rmdir()
        raise
