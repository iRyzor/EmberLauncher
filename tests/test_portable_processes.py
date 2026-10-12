from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import psutil
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles/vendor"))
from stove_linux.portable_processes import PortableGameProcesses, require_idle, matches

class PortableProcessTests(unittest.TestCase):
    def process(self, pid, created):
        process = Mock()
        process.pid = pid
        process.create_time.return_value = created
        process.is_running.return_value = True
        process.status.return_value = psutil.STATUS_RUNNING
        return process
    @patch("stove_linux.portable_processes.matches", return_value=True)
    def test_baseline_is_never_owned_but_reused_pid_is_new_identity(self, matching):
        old = self.process(42, 1)
        new = self.process(42, 2)
        with patch("stove_linux.portable_processes.psutil.process_iter", return_value=[old]) as scan:
            tracker = PortableGameProcesses(object())
            self.assertFalse(tracker.refresh())
            scan.return_value = [new]
            self.assertTrue(tracker.refresh())
            self.assertIs(tracker.owned[42], new)
            new.status.side_effect = psutil.NoSuchProcess(42)
            scan.return_value = []
            self.assertFalse(tracker.refresh())
            old.kill.assert_not_called()
            old.terminate.assert_not_called()
    def test_inaccessible_process_does_not_crash_guard(self):
        process = self.process(42, 1)
        process.name.side_effect = psutil.AccessDenied(42)
        with patch("stove_linux.portable_processes.matches", return_value=False), patch("stove_linux.portable_processes.psutil.process_iter", return_value=[process]):
            require_idle(object())
    def test_only_executable_in_exact_selected_directory_is_matched(self):
        installation = SimpleNamespace(game_dir=Path.cwd()/"game",prefix=Path.cwd())
        process = self.process(42,1)
        process.username.return_value = psutil.Process().username()
        process.cmdline.return_value = [str(installation.game_dir/"game.exe")]
        process.exe.return_value = str(installation.game_dir/"game.exe")
        with patch("stove_linux.portable_processes.sys.platform", "win32"):
            self.assertTrue(matches(installation,process))
            process.cmdline.return_value = [str(Path.cwd()/"game-other/game.exe"),str(installation.game_dir/"game.exe")]
            process.exe.return_value = process.cmdline.return_value[0]
            self.assertFalse(matches(installation,process))

    def test_macos_wine_preloader_argument_requires_exact_prefix(self):
        prefix=Path.cwd()/"prefix"
        installation=SimpleNamespace(prefix=prefix,game_dir=prefix/"drive_c/Smilegate/Games/ChaosZeroNightmare")
        process=self.process(42,1)
        process.username.return_value=psutil.Process().username()
        process.exe.return_value="/opt/wine/bin/wine64-preloader"
        process.cmdline.return_value=[process.exe.return_value,"C:\\Smilegate\\Games\\ChaosZeroNightmare\\game.exe"]
        process.environ.return_value={"WINEPREFIX":str(prefix)}
        with patch("stove_linux.portable_processes.sys.platform","darwin"):
            self.assertTrue(matches(installation,process))
            process.environ.return_value={"WINEPREFIX":str(Path.cwd()/"other-prefix")}
            self.assertFalse(matches(installation,process))
