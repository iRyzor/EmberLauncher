"""Black-box standalone Rust RPC and read-only parity with the Python oracle."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from stove_linux.manifest import repair_plan

ROOT=Path(__file__).resolve().parents[1]
BINARY=Path(os.environ['EMBER_TEST_ENGINE']) if os.environ.get('EMBER_TEST_ENGINE') else ROOT/'src-tauri/engine'/('stove-engine.exe' if sys.platform=='win32' else 'stove-engine')


def environment(folder):
    result=dict(os.environ)
    for kind in ('CONFIG','CACHE','DATA'):
        result[f'EMBER_{kind}_DIR']=str(Path(folder)/kind.lower())
    for name in ('XDG_CONFIG_HOME','XDG_CACHE_HOME','XDG_DATA_HOME','LOCALAPPDATA','APPDATA'):
        result[name]=str(Path(folder)/name.lower())
    result.pop('EMBER_WINE_RUNNER',None)
    return result


class RustEngineTests(unittest.TestCase):
    def rpc(self, folder, root, requests):
        result=subprocess.run([str(BINARY),'--root',str(root),'--helper',str(ROOT/'engine/native/pipe-bridge.exe')],
            input=''.join(json.dumps(r)+'\n' for r in requests),text=True,encoding="utf-8",capture_output=True,
            timeout=30,env=environment(folder))
        self.assertEqual(result.returncode,0)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_packaged_runtime_identifies_as_rust(self):
        result=subprocess.run([str(BINARY),'--version'],text=True,encoding="utf-8",capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0)
        self.assertEqual(result.stdout.strip(),f"ember-engine {json.loads((ROOT/'package.json').read_text())['version']} rust")

    def test_missing_library_unknown_action_and_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            absent=Path(folder)/'missing-library'
            states=self.rpc(folder,absent,[{'action':'state'},{'action':'unknown'}, {'action':'launch'},{'action':'shutdown'}])
            self.assertEqual(len(states),4)
            for state in states[:3]:
                self.assertFalse(state['connected'])
                self.assertFalse(state['game_installed'])
                self.assertFalse(state['game_running'])
                self.assertEqual(state['platform'],sys.platform)
            self.assertEqual(states[1]['error'],'Operación no disponible.')
            self.assertEqual(states[-1],{'closed':True})
            self.assertFalse(absent.exists())

    def test_fresh_selection_persists_settings_without_creating_game(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix=Path(folder)/'prefix'
            (prefix/'drive_c').mkdir(parents=True)
            (prefix/'system.reg').write_text('synthetic prefix')
            root=prefix/'drive_c/Smilegate'
            runner='native' if sys.platform=='win32' else sys.executable
            states=self.rpc(folder,root,[{'action':'new_installation','payload':{'root':str(root),'runner':runner}}, {'action':'shutdown'}])
            self.assertNotIn('error',states[0])
            self.assertTrue(states[0]['fresh_destination'])
            self.assertFalse((root/'Games/ChaosZeroNightmare').exists())
            saved=json.loads((Path(folder)/'config/settings.json').read_text())
            self.assertEqual(Path(saved['root']).resolve(),root.resolve())
            self.assertEqual(saved['runner'],runner)
            states=self.rpc(folder,root,[{'action':'state'},{'action':'shutdown'}])
            self.assertEqual(states[0]['runner'],runner)
            self.assertFalse(states[0]['connected'])

    def test_manifest_plans_match_python_oracle(self):
        with tempfile.TemporaryDirectory() as folder:
            game=Path(folder)/'game'
            game.mkdir()
            content=b'new content'
            md5=hashlib.md5(content).hexdigest()
            records=['0|D|/bin',f'1|F|/bin/data.txt|55|2|gz|{len(content)}|6|{md5}|{"a"*32}|a|', '2|G|/bin/generated.txt','3|R|/bin/remove.txt']
            raw=json.dumps({'service_code':'STOVE_CHAOSZERO','version_no':55,'files':records,'startpos':0,'extract_size':str(len(content)),'packed_size':'6'}).encode()
            manifest=Path(folder)/'manifest.json'
            manifest.write_bytes(raw)
            for stage in range(3):
                if stage==1:
                    (game/'bin').mkdir()
                    (game/'bin/data.txt').write_bytes(content)
                    (game/'bin/remove.txt').write_bytes(b'old')
                    (game/'bin/generated.txt').write_bytes(b'generated')
                if stage==2:
                    (game/'bin/data.txt').write_bytes(b'modified')
                result=subprocess.run([str(BINARY),'--inspect-plan',str(game),str(manifest),'55'],capture_output=True,text=True,encoding="utf-8",timeout=10)
                self.assertEqual(result.returncode,0)
                self.assertEqual(json.loads(result.stdout),repair_plan(game,raw,55))

    def test_oversize_rpc_and_invalid_json_never_modify_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'missing'
            result=subprocess.run([str(BINARY),'--root',str(root)],input='bad\n'+('x'*16385)+'\n',
                capture_output=True,text=True,encoding="utf-8",timeout=10,env=environment(folder))
            self.assertEqual(result.returncode,0)
            self.assertEqual(json.loads(result.stdout),{'error':'Petición inválida.'})
            self.assertFalse(root.exists())
