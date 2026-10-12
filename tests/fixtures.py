"""Synthetic files shared by standalone engine checks; no STOVE installation required."""
import json, hashlib, sqlite3
from contextlib import closing
from types import SimpleNamespace
from stove_linux.game_auth import GAME_ID

def manifest(path='/bin/test.dll'):
    return json.dumps({'service_code':GAME_ID,'version_no':55,'extract_size':'3','packed_size':'12',
        'files':[f'1 | F | {path} | 55 | 1 | gz | 3 | 12 | {hashlib.md5(b"abc").hexdigest()} | {"0"*32} | a | | | | 0 |']}).encode()

def transaction_fixture(root):
        game=root/'game';game.mkdir();(game/'bin').mkdir()
        (game/'bin/test.dll').write_bytes(b'old')
        (game/'obsolete').write_bytes(b'backup')
        (game/'untracked').write_bytes(b'keep')
        prepared=root/'prepared';prepared.mkdir();(prepared/'verified.bin').write_bytes(b'abc')
        data=json.loads(manifest());data['files'] += ['2|R|/obsolete|','3|G|/bin/.keep']
        raw=json.dumps(data).encode();(prepared/'manifest.json').write_bytes(raw)
        (prepared/'prepared.json').write_text(json.dumps({'manifest_sha256':hashlib.sha256(raw).hexdigest(),
            'staged':[{'path':'bin/test.dll','staged_file':'verified.bin'}]}))
        return game,prepared

def commit_fixture(root):
        game,prepared=transaction_fixture(root)
        folder=game/'combinedata_manifest';folder.mkdir()
        upf=folder/f'GameManifest_{GAME_ID}.upf'
        upf.write_text(json.dumps({'game_id':GAME_ID,'local_version':54,'locale':'en','files':[]}))
        with closing(sqlite3.connect(folder/'cacheii.db')) as db:
            db.execute('CREATE TABLE installed_info (file_name TEXT NOT NULL,file_size BIGINT NOT NULL,file_hash VARCHAR(32) NOT NULL,PRIMARY KEY(file_name))')
            db.execute('INSERT INTO installed_info VALUES(?,?,?)',('old',3,'OLD'))
            db.commit()
        receipt=json.loads((prepared/'prepared.json').read_text());receipt.update(distribution_version=54,published_version=55)
        (prepared/'prepared.json').write_text(json.dumps(receipt))
        return game,prepared,SimpleNamespace(game_dir=game,prefix=root)
