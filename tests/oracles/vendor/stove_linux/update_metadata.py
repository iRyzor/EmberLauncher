"""Build native metadata replacements without modifying the installed database."""
import json
import sqlite3
from pathlib import Path

from .game_auth import GAME_ID
from .manifest import parse_manifest

SCHEMA = [('file_name','TEXT'),('file_size','BIGINT'),('file_hash','VARCHAR(32)')]


def build_metadata(game, work, raw, version, expected):
    folder = Path(game)/'combinedata_manifest'
    upf = folder/f'GameManifest_{GAME_ID}.upf'
    database = folder/'cacheii.db'
    for path in (folder,upf,database):
        if path.is_symlink():raise ValueError('Unsafe native metadata')
    value = json.loads(upf.read_bytes())
    if value.get('game_id') != GAME_ID or value.get('local_version') != expected:
        raise ValueError('Installed version changed')
    if any(Path(str(database)+suffix).exists() for suffix in ('-wal','-shm','-journal')):
        raise ValueError('Native inventory has an active journal')
    records = parse_manifest(raw,version)
    source = sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)
    output = Path(work)/'inventory.sqlite'
    try:
        columns = [(row[1],row[2]) for row in source.execute('PRAGMA table_info(installed_info)')]
        if columns != SCHEMA:raise ValueError('Unsupported native inventory schema')
        target = sqlite3.connect(output)
        try:
            source.backup(target)
            with target:
                target.execute('DELETE FROM installed_info')
                target.executemany('INSERT INTO installed_info(file_name,file_size,file_hash) VALUES(?,?,?)',
                    [(item['path'],item['size'],item['md5'].upper()) for item in records if item['kind']=='F'])
            if target.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise ValueError('Invalid rebuilt inventory')
        finally:target.close()
    finally:source.close()
    value['local_version']=version
    value['project_url']=f'https://chaoszero-dl.game.onstove.com/game/dpms_{GAME_ID}/{GAME_ID}_{version}.json'
    new_upf = Path(work)/'version.upf'
    new_upf.write_text(json.dumps(value,ensure_ascii=False,indent=2))
    return [(f'combinedata_manifest/cacheii.db',output),
            (f'combinedata_manifest/GameManifest_{GAME_ID}.upf',new_upf)]


def checkpoint_inventory(installation):
    """Normalize an idle WAL cache before starting a transaction.

    This changes SQLite's storage layout, never its logical rows or version.
    Active SQLite readers/writers or native game processes cause rejection.
    """
    from .update_guard import require_idle
    require_idle(installation)
    database=installation.game_dir/'combinedata_manifest/cacheii.db'
    if database.is_symlink() or not database.is_file():raise ValueError('Unsafe inventory')
    connection=sqlite3.connect(database.resolve().as_uri()+'?mode=rw',uri=True,timeout=0.1)
    try:
        connection.execute('PRAGMA busy_timeout=100')
        columns=[(row[1],row[2]) for row in connection.execute('PRAGMA table_info(installed_info)')]
        if columns != SCHEMA:raise ValueError('Unsupported inventory schema')
        connection.execute('PRAGMA locking_mode=EXCLUSIVE')
        checkpoint=connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if checkpoint[0] != 0:raise ValueError('Native inventory is busy')
        if connection.execute('PRAGMA journal_mode=DELETE').fetchone()[0] != 'delete':
            raise ValueError('Cannot normalize native inventory')
        if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('Invalid native inventory')
    finally:connection.close()
