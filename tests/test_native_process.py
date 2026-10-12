"""Process predicates compared with a Python oracle without signalling a game."""
import ctypes
import json
import random
import unittest
from stove_linux import native_process, python_process


class NativeProcessTests(unittest.TestCase):
    def test_requires_rust_process_abi(self):
        self.assertIsNotNone(native_process.library)
        self.assertEqual(native_process.library.ember_process_abi(), 2)

    def test_normalized_path_rules_match_oracle(self):
        for game in ['/game/', '/game space/', '/日本語/', '/straße/']:
            native = game.casefold().encode()
            windows = ('c:' + game).casefold().encode()
            for candidate in [game+'client.exe', 'c:'+game+'client.exe', game+'bin/client.exe',
                              game+'client.dll', game+'crashpad_handler.exe',game+'services.exe',
                              game+'../other.exe',game+'./client.exe', '/other/client.exe',
                              game.rstrip('/')+'-other/client.exe']:
                value = candidate.casefold().encode()
                self.assertEqual(native_process.path_match(native,windows,value),
                                 python_process.path_match(native,windows,value))

    def test_mapping_rules_match_oracle_for_permissions_paths_and_deleted_files(self):
        rng = random.Random(20261011)
        game = b'/game space/'
        for _ in range(500):
            maps = []
            for _ in range(rng.randrange(1,10)):
                prefix = rng.choice([game,b'/other/game space/',b'/game space-other/',b'/other/'])
                permissions = rng.choice([b'r-xp',b'r--p',b'rwxp',b'---p'])
                suffix = rng.choice([b'.exe',b'.DLL',b'.so',b'.exe (deleted)',b'.exe.bad'])
                maps.append(b'1000-2000 '+permissions+b' 0000 00:00 1 '+prefix+b'bin/file'+suffix)
            raw = b'\n'.join(maps)
            self.assertEqual(native_process.mapping_match(game,raw),python_process.mapping_match(game,raw))
        self.assertFalse(native_process.mapping_match(b'/game/',b'1000-2000 r-xp 0000 00:00 1 /other/game/client.exe'))

    def test_infrastructure_is_not_a_game(self):
        for value in [b'wineserver',b'WINESERVER',b'c:/game/crashpad_handler.exe',b'services.exe',b'ssr-stove-shield.exe']:
            self.assertEqual(native_process.excluded(value),python_process.excluded(value))
        self.assertFalse(native_process.path_match(b'/game/',b'c:/game/',b'c:/game/crashpad_handler.exe'))

    def test_invalid_native_inputs_fail_without_side_effects(self):
        lib = native_process.library
        self.assertEqual(lib.ember_process_check(99,None,0,None,0,None,0),-1)
        self.assertEqual(lib.ember_process_check(0,None,1,None,0,None,0),-1)


    def test_lifecycle_matches_oracle_across_process_reuse_and_loader_exit(self):
        rng = random.Random(11)
        baseline = {1:100, 2:200}
        owned, seen, empty = {}, False, None
        for tick in range(1000):
            current = {pid:rng.choice([100,200,300,400]) for pid in range(1,6) if rng.randrange(3)}
            alive = {pid:start for pid,start in {**owned,**current}.items() if rng.randrange(4)}
            args = (baseline,owned,current,alive,seen,empty,tick/4)
            result = native_process.lifecycle(*args)
            self.assertEqual(result,python_process.lifecycle(*args))
            owned, seen, empty = result['owned'],result['seen'],result['empty_since']

    def test_lifecycle_grace_identity_and_preexisting_processes(self):
        def step(owned, current, alive, seen=False, empty=None, now=0):
            return native_process.lifecycle({1:100},owned,current,alive,seen,empty,now)
        self.assertFalse(step({}, {1:100}, {1:100})['running'])
        self.assertTrue(step({}, {1:200}, {1:200})['running'])
        self.assertFalse(step({1:200}, {}, {1:300}, True)['running'])
        self.assertTrue(step({2:400}, {}, {2:400}, True)['running'])
        self.assertFalse(step({}, {}, {}, True, 10, 11.999)['finished'])
        self.assertTrue(step({}, {}, {}, True, 10, 12)['finished'])
        self.assertIsNone(step({}, {2:400}, {2:400}, True, 10, 12)['empty_since'])
        self.assertFalse(step({}, {}, {}, False, None, 100)['finished'])
        self.assertEqual(step({}, {1:100.0}, {1:100.0})['owned'], {})
        huge = 2**60
        self.assertFalse(native_process.lifecycle({}, {2:huge}, {}, {2:huge+1}, True,None,0)['running'])


    def test_lifecycle_rejects_invalid_metadata_without_writing(self):
        base = dict(baseline={}, owned={}, current={}, alive={}, seen=False, empty_since=None, now=0)
        target = ctypes.create_string_buffer(b'x'*(1024*1024+4096))
        for field,value in [('baseline',{'0':1}),('owned',{'2':'bad'}),('seen',1),('now','bad'),('empty_since',False)]:
            raw = json.dumps({**base,field:value}).encode()
            source = ctypes.create_string_buffer(raw)
            self.assertEqual(native_process.library.ember_process_lifecycle(source,len(raw),target,len(target)),-1)
            self.assertEqual(target.raw[:16],b'x'*16)
        raw = json.dumps(base).encode()
        source = ctypes.create_string_buffer(raw)
        self.assertEqual(native_process.library.ember_process_lifecycle(source,len(raw),target,1),-1)
        self.assertEqual(native_process.library.ember_process_lifecycle(None,1,target,len(target)),-1)
        self.assertEqual(target.raw[:16],b'x'*16)
