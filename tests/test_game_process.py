import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
import os
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from game_process import GameProcesses, identity


class GameProcessTests(unittest.TestCase):
    def fake(self, root, pid, start, name="game.exe"):
        entry = root / "proc" / str(pid)
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "environ").write_bytes(b"WINEPREFIX=" + os.fsencode(root) + b"\0")
        (entry / "cmdline").write_bytes(name.encode() + b"\0")
        (entry / "maps").write_bytes(
            b"1000-2000 r-xp 0000 00:00 1 " + os.fsencode(root / "game/bin/game.exe")
        )
        fields = ["S", "1"] + ["0"] * 17 + [str(start)]
        (entry / "stat").write_text(f"{pid} (game) " + " ".join(fields))

    def test_existing_game_and_wine_server_are_never_owned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fake(root, 101, 100)
            tracker = GameProcesses(
                SimpleNamespace(game_dir=root / "game", prefix=root), root / "proc"
            )
            self.fake(root, 102, 200)
            self.fake(root, 103, 300, "wineserver")
            self.assertTrue(tracker.refresh())
            self.assertEqual(tracker.owned, {102: 200})

    @patch("game_process.os.close")
    @patch("game_process.send_signal")
    def test_pid_reuse_is_rejected_after_pidfd_open(self, send, close):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "proc").mkdir()
            tracker = GameProcesses(
                SimpleNamespace(game_dir=root / "game", prefix=root), root / "proc"
            )
            self.fake(root, 102, 200)
            tracker.refresh()

            def pin(pid):
                self.fake(root, pid, 201)
                return 42

            with patch("game_process.open_process", side_effect=pin):
                tracker.signal_owned(signal.SIGTERM)
            send.assert_not_called()
            close.assert_called_once_with(42)

    def test_only_owned_test_process_is_terminated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            installation = SimpleNamespace(game_dir=root / "game", prefix=root)
            tracker = GameProcesses(installation)
            owned = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(60)"]
            )
            other = subprocess.Popen(
                [sys.executable, "-c", "import time;time.sleep(60)"]
            )
            try:
                tracker.owned = {owned.pid: identity(owned.pid)[0]}
                tracker.seen = True
                result = tracker.stop(timeout=3)
                self.assertTrue(result["closed"])
                self.assertIsNone(other.poll())
                owned.wait(timeout=2)
                self.assertEqual(owned.returncode, -signal.SIGTERM)
            finally:
                for process in (owned, other):
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=2)

    def test_non_executable_mappings_are_not_owned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "proc").mkdir()
            tracker = GameProcesses(
                SimpleNamespace(game_dir=root / "game", prefix=root), root / "proc"
            )
            self.fake(root, 102, 200)
            (root / "proc/102/maps").write_bytes(
                b"1000-2000 r--p 0000 00:00 1 "
                + os.fsencode(root / "game/bin/game.exe")
            )
            self.assertFalse(tracker.refresh())

    def test_wine_game_survives_loader_exit_without_pe_mappings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "proc").mkdir()
            tracker = GameProcesses(
                SimpleNamespace(game_dir=root / "drive_c/Games/CZN", prefix=root),
                root / "proc",
            )
            self.fake(root, 102, 200, r"C:\Games\CZN\loader.exe")
            self.fake(root, 103, 300, r"C:\Games\CZN\ssr-stove-shield.exe")
            for pid in (102, 103):
                (root / "proc" / str(pid) / "maps").write_bytes(b"")
            self.assertTrue(tracker.refresh())
            self.assertEqual(tracker.owned, {102: 200, 103: 300})
            (root / "proc/102/stat").unlink()
            self.assertTrue(tracker.refresh())
            self.assertFalse(tracker.finished())
            self.assertEqual(tracker.owned, {103: 300})

    def test_windows_path_requires_same_prefix_and_exact_game_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "proc").mkdir()
            tracker = GameProcesses(
                SimpleNamespace(game_dir=root / "drive_c/Games/CZN", prefix=root),
                root / "proc",
            )
            for pid, name in enumerate(
                (
                    r"C:\Games\CZN-other\game.exe",
                    r"C:\Games\CZN\crashpad_handler.exe",
                    r"C:\Games\CZN\ssr-stove-shield.exe",
                    "other.exe",
                ),
                101,
            ):
                self.fake(root, pid, pid, name)
                (root / "proc" / str(pid) / "maps").write_bytes(b"")
            (root / "proc/103/environ").write_bytes(b"WINEPREFIX=/other\0")
            (root / "proc/104/cmdline").write_bytes(
                b"other.exe\0C:\\Games\\CZN\\ssr-stove-shield.exe\0"
            )
            self.assertFalse(tracker.refresh())

    def test_force_close_is_separate_when_term_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tracker = GameProcesses(
                SimpleNamespace(game_dir=root / "game", prefix=root)
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('ready',flush=True);time.sleep(60)",
                ],
                stdout=subprocess.PIPE,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), b"ready")
                tracker.owned = {process.pid: identity(process.pid)[0]}
                tracker.seen = True
                self.assertTrue(tracker.stop(timeout=0.1)["force_required"])
                self.assertIsNone(process.poll())
                self.assertTrue(tracker.stop(force=True, timeout=3)["closed"])
                process.wait(timeout=2)
                self.assertEqual(process.returncode, -signal.SIGKILL)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
                process.stdout.close()
