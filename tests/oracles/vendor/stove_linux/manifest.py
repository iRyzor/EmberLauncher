"""Bounded DPMS manifest verification and a read-only repair plan."""
import hashlib
import json
from .platform_fs import os
from pathlib import PurePosixPath
import re
import stat
from urllib.request import build_opener, HTTPRedirectHandler

from .game_auth import GAME_ID
from .updates import manifest_url, GAME_CDN

LIMIT = 8 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_manifest(version, url=None):
    url = manifest_url(url or f'https://{GAME_CDN}/game/dpms_{GAME_ID}/{GAME_ID}_{version}.json', version)
    # Public CDN request: never forward account headers or cookies.
    with build_opener(NoRedirect()).open(url, timeout=30) as response:
        raw = response.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('Manifest too large')
    return raw


def parse_manifest(raw, version):
    if len(raw) > LIMIT:
        raise ValueError('Manifest too large')
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('service_code') != GAME_ID or type(value.get('version_no')) is not int or value['version_no'] != version:
        raise ValueError('Unexpected manifest identity or version')
    records = value.get('files')
    if not isinstance(records, list) or len(records) > 100000:
        raise ValueError('Invalid manifest files')
    result, seen, ids = [], set(), set()
    for record in records:
        if not isinstance(record, str) or len(record) > 8192:
            raise ValueError('Invalid manifest record')
        fields = [field.strip() for field in record.split('|')]
        if len(fields) < 3 or not fields[0].isdigit() or fields[0] in ids:
            raise ValueError('Invalid manifest record identifier')
        ids.add(fields[0])
        kind, native_path = fields[1:3]
        path = native_path[1:] if native_path.startswith('/') else native_path
        parts = path.split('/')
        if path.split('/')[0].casefold() == 'combinedata_manifest' or any(p.casefold().startswith('.stove-') for p in parts):
            raise ValueError('Reserved launcher path')
        if not path or any(not p or p in ('.', '..') or p.endswith((' ', '.')) or re.search(r'[\\:<>"?*\x00-\x1f\x7f]', p) or p.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *(f'COM{i}' for i in range(1,10)), *(f'LPT{i}' for i in range(1,10))} for p in parts):
            raise ValueError('Unsafe manifest path')
        if path.casefold() in seen:
            raise ValueError('Duplicate manifest path')
        seen.add(path.casefold())
        item = {'kind': kind, 'path': str(PurePosixPath(path))}
        if kind == 'F':
            if len(fields) not in (12,13,16):
                raise ValueError('Unsupported file record')
            if fields[5] != 'gz' or not ((len(fields) == 12 and fields[10:] == ['a','']) or (len(fields) == 13 and fields[10:] == ['a','','']) or (len(fields) == 16 and fields[10:] == ['a','','','','0',''])):
                raise ValueError('Unsupported file encoding or hash profile')
            if any(not fields[i].isdigit() for i in (3,4,6,7)) or any(not re.fullmatch('[0-9a-fA-F]{32}', fields[i]) for i in (8,9)):
                raise ValueError('Invalid file metadata')
            item.update(size=int(fields[6]), packed_size=int(fields[7]), md5=fields[8].lower(), packed_md5=fields[9].lower(), build=int(fields[3]), chunk=int(fields[4]), startpos=int(value.get('startpos',0)))
        elif kind == 'D' and len(fields) == 3:
            pass
        elif kind == 'G' and (len(fields) == 3 or (len(fields) == 16 and fields[3:14] == [''] * 11 and fields[14:] == ['0',''])):
            pass
        elif kind == 'R' and (len(fields) == 3 or (len(fields) == 4 and fields[3] == '')):
            pass
        else:
            raise ValueError('Unsupported manifest instruction')
        result.append(item)
    for key, field in [('extract_size','size'), ('packed_size','packed_size')]:
        expected = value.get(key)
        if not isinstance(expected, (str, int)) or isinstance(expected, bool) or not str(expected).isdigit() or int(expected) != sum(item.get(field,0) for item in result):
            raise ValueError('Inconsistent manifest size')
    return result


def inspect_target(root_fd, item):
    """Open every component relative to the root without following symlinks."""
    directory = os.dup(root_fd)
    descriptor = None
    try:
        parts = item['path'].split('/')
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        info = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            return 'blocked'
        if item['kind'] == 'R':
            return 'remove' if stat.S_ISREG(info.st_mode) else 'blocked'
        if item['kind'] == 'D':
            return 'matching' if stat.S_ISDIR(info.st_mode) else 'blocked'
        if not stat.S_ISREG(info.st_mode):
            return 'blocked'
        if item['kind'] == 'G':
            return 'generated_present'
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return 'blocked'
        digest = hashlib.md5(usedforsecurity=False)
        if before.st_size == item['size']:
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return 'changed_during_check'
        return 'matching' if before.st_size == item['size'] and digest.hexdigest() == item['md5'] else 'modified'
    except FileNotFoundError:
        return 'absent' if item['kind'] == 'R' else 'missing'
    except OSError:
        return 'blocked'
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(directory)


def repair_plan(game_dir, raw, version):
    records = parse_manifest(raw, version)
    root_fd = os.open(game_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        entries = [dict(item, status=inspect_target(root_fd,item)) for item in records]
    finally:
        os.close(root_fd)
    downloads = [item for item in entries if item['kind'] == 'F' and item['status'] in ('missing','modified')]
    return {'game_id':GAME_ID, 'reference_version':version, 'remote_latest_checked':False,
            'read_only':True, 'coverage':'DPMS distribution files only',
            'manifest_sha256':hashlib.sha256(raw).hexdigest(),
            'download_bytes':sum(item['packed_size'] for item in downloads),
            'staging_bytes':sum(item['packed_size'] + item['size'] for item in downloads),
            'blocked':any(item['status'] in ('blocked','changed_during_check') for item in entries),
            'entries':entries}
