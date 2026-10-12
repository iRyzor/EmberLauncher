import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
from oracle_engine import Engine


class EngineTests(unittest.TestCase):
    def engine(self, root):
        with (
            patch(
                "oracle_engine.Installation.discover",
                return_value=SimpleNamespace(
                    executable=root / "game.exe", game_dir=root, prefix=root
                ),
            ),
            patch(
                "oracle_engine.installed_update_state", return_value={"distribution_version": 55}
            ),
        ):
            (root / "game.exe").touch()
            engine = Engine(str(root))
            engine.settings_path = root / "settings.json"
            return engine

    def test_tokens_never_leave_process_and_logout_discards_them(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self.engine(Path(d))
            engine.session = SimpleNamespace(
                access_token="PRIVATE_ACCESS", refresh_token="PRIVATE_REFRESH"
            )
            self.assertNotIn("PRIVATE", json.dumps(engine.dispatch("state")))
            engine.dispatch("logout")
            self.assertIsNone(engine.session)

    def test_launch_and_update_require_session(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self.engine(Path(d))
            for action in ("launch", "prepare", "check_updates"):
                self.assertIn("error", engine.dispatch(action))
            self.assertIsNone(engine.launcher)

    def test_qr_exchange_hides_challenge_and_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self.engine(Path(d))
            engine.client = Mock()
            engine.client.begin.return_value = SimpleNamespace(
                image="synthetic-image", session="PRIVATE_QR"
            )
            engine.client.poll.return_value = "success"
            engine.client.exchange.return_value = SimpleNamespace(
                access_token="PRIVATE_ACCESS"
            )
            self.assertEqual(engine.dispatch("login")["phase"], "qr_pending")
            result = engine.dispatch("poll")
            self.assertTrue(result["connected"])
            self.assertIsNone(result["image"])
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_background_failure_uses_fixed_message(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self.engine(Path(d))

            def fail():
                raise ValueError("PRIVATE_ACCESS")

            engine.background("verify", fail)
            import time

            for _ in range(100):
                if not engine.job["running"]:
                    break
                time.sleep(0.005)
            self.assertFalse(engine.job["running"])
            self.assertNotIn("PRIVATE", json.dumps(engine.snapshot()))

    def test_unrecognized_action_has_no_effect(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self.engine(Path(d))
            self.assertIn("error", engine.dispatch("arbitrary_shell"))

    def test_missing_installation_still_opens_settings(self):
        with patch("oracle_engine.Installation.discover", side_effect=ValueError("absent")):
            engine = Engine("/nonexistent/installation")
        state = engine.dispatch("state")
        self.assertFalse(state["game_installed"])
        self.assertIsNone(state["version"])
        self.assertIn("Ajustes", engine.dispatch("login")["error"])

    def test_game_exit_releases_launcher_and_restores_ready_state(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self.engine(Path(d))
            engine.session = object()
            engine.launcher = Mock()
            engine.game_processes = Mock()
            engine.game_processes.refresh.return_value = False
            engine.game_processes.finished.return_value = True
            engine.game_processes.seen = True
            launcher = engine.launcher
            state = engine.snapshot()
            launcher.close.assert_called_once()
            self.assertIsNone(engine.launcher)
            self.assertEqual(state["phase"], "ready")
            self.assertFalse(state["game_running"])

    def test_close_does_not_adopt_game_started_outside_ember(self):
        with tempfile.TemporaryDirectory() as d:
            engine = self.engine(Path(d))
            self.assertIn("error", engine.dispatch("stop_game"))
            self.assertIsNone(engine.job)

    def test_cancel_download_rpc_keeps_job_until_worker_acknowledges(self):
        import threading
        from resumable_updates import DownloadCancelled
        from stove_linux.updates import PublishedVersion

        with tempfile.TemporaryDirectory() as folder:
            engine = self.engine(Path(folder))
            engine.session = SimpleNamespace(access_token="PRIVATE_TOKEN")
            engine.version_context = None
            engine.client = Mock()
            entered = threading.Event()
            acknowledged = threading.Event()

            def prepare(installation, published, staging, cancel, progress):
                progress(
                    {
                        "phase": "download",
                        "downloaded_bytes": 10,
                        "total_bytes": 100,
                        "package_index": 1,
                        "package_count": 1,
                        "path": "bin/test.dll",
                    }
                )
                entered.set()
                cancel.wait(2)
                acknowledged.wait(2)
                raise DownloadCancelled()

            with (
                patch(
                    "oracle_engine.request_published_version",
                    return_value=PublishedVersion(55, 56, "PRIVATE_URL"),
                ),
                patch("oracle_engine.prepare_resumable", side_effect=prepare),
            ):
                state = engine.dispatch("prepare")
                self.assertTrue(entered.wait(1))
                state = engine.dispatch("cancel_download")
                self.assertTrue(state["job"]["running"])
                self.assertTrue(state["job"]["cancelling"])
                self.assertNotIn("PRIVATE", json.dumps(state))
                acknowledged.set()
                for _ in range(100):
                    if not engine.job["running"]:
                        break
                    threading.Event().wait(0.01)
                self.assertFalse(engine.job["running"])
                self.assertTrue(engine.job["result"]["cancelled"])
                self.assertFalse(engine.snapshot()["prepared"])
                self.assertIn("error", engine.dispatch("cancel_download"))

    def test_expired_session_preserves_game_bridge_and_close_control(self):
        from session_access import SessionExpired
        import time

        with tempfile.TemporaryDirectory() as folder:
            engine = self.engine(Path(folder))
            engine.session = object()
            engine.launcher = Mock()
            engine.launcher.state.return_value = {
                "phase": "launch_info_sent",
                "detail": "Partida activa",
            }
            engine.game_processes = Mock()
            engine.game_processes.refresh.return_value = True
            engine.game_processes.finished.return_value = False
            engine.game_processes.seen = True
            launcher = engine.launcher

            def expired():
                raise SessionExpired()

            engine.background("check_updates", expired)
            for _ in range(100):
                if not engine.job["running"]:
                    break
                time.sleep(0.005)
            state = engine.snapshot()
            self.assertFalse(state["connected"])
            self.assertTrue(state["reauth_required"])
            self.assertTrue(state["game_running"])
            launcher.close.assert_not_called()
            engine.game_processes.stop.return_value = {
                "closed": True,
                "force_required": False,
            }
            engine.dispatch("stop_game")
            for _ in range(100):
                if not engine.job["running"]:
                    break
                time.sleep(0.005)
            self.assertTrue(engine.job["result"]["closed"])
            launcher.close.assert_called_once()

    def test_select_fresh_destination_saves_settings_without_creating_game(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine = self.engine(root)
            prefix = root / "prefix"
            (prefix / "drive_c").mkdir(parents=True)
            (prefix / "system.reg").touch()
            target = prefix / "drive_c/New/Smilegate"
            with (
                patch("oracle_engine.transfer_profile") as transfer,
                patch("oracle_engine.shutil.which", return_value="/usr/bin/wine"),
            ):
                state = engine.dispatch(
                    "new_installation", {"root": str(target), "runner": "wine"}
                )
            self.assertNotIn("error", state)
            self.assertTrue(state["fresh_destination"])
            self.assertFalse(state["game_installed"])
            self.assertIsNone(state["version"])
            transfer.assert_called_once()
            self.assertFalse((target / "Games/ChaosZeroNightmare").exists())
            settings = json.loads(engine.settings_path.read_text())
            self.assertEqual(settings["root"], str(target.resolve()))

    def test_fresh_destination_cannot_be_selected_during_a_session(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = self.engine(Path(folder))
            engine.session = object()
            with patch("oracle_engine.transfer_profile") as transfer:
                state = engine.dispatch(
                    "new_installation", {"root": "/new/path", "runner": "wine"}
                )
            self.assertIn("error", state)
            transfer.assert_not_called()

    def test_fresh_receipt_never_falls_back_to_update_if_target_appears(self):
        from test_fresh_install import FreshInstallTests
        from stove_linux.auth import AuthError
        import time

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine = self.engine(root)
            installation, prepared, raw = FreshInstallTests().fixture(root)
            value = json.loads((prepared / "prepared.json").read_text())
            value["fresh_install"] = True
            (prepared / "prepared.json").write_text(json.dumps(value))
            engine.installation = installation
            engine.local = {"distribution_version": None}
            engine.prepared = str(prepared)
            installation.game_dir.mkdir(parents=True)
            with (
                patch(
                    "oracle_engine.install_prepared",
                    side_effect=AuthError("El destino ya existe"),
                ) as install,
                patch("oracle_engine.apply_update") as update,
            ):
                engine.dispatch("apply_update")
                for _ in range(100):
                    if not engine.job["running"]:
                        break
                    time.sleep(0.005)
                install.assert_called_once()
                update.assert_not_called()
                self.assertIn("existe", engine.job["error"])

    def test_missing_runtime_prevents_game_token_request_and_start(self):
        with tempfile.TemporaryDirectory() as folder:
            engine = self.engine(Path(folder))
            engine.session = object()
            from stove_linux.auth import AuthError
            with patch("oracle_engine.require_idle"),patch("oracle_engine.require_runtime",side_effect=AuthError("Falta WebView2")),patch("oracle_engine.request_game_session") as session,patch("oracle_engine.GameLauncher") as launcher:
                self.assertIn("WebView2",engine.dispatch("launch")["error"])
                session.assert_not_called();launcher.assert_not_called()

    def test_bootstrap_dependency_failure_preserves_selected_installation(self):
        import time
        from stove_linux.auth import AuthError
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine = self.engine(root)
            selected = engine.installation
            with patch("oracle_engine.create_prefix", return_value=object()),patch("oracle_engine.prepare_runtime", side_effect=AuthError("Runtime no disponible")):
                engine.dispatch("bootstrap", {"prefix":str(root/"new-prefix"), "runner":"wine"})
                deadline = time.monotonic()+2
                while engine.job["running"] and time.monotonic()<deadline: time.sleep(.01)
            self.assertFalse(engine.job["running"])
            self.assertIn("Runtime",engine.job["error"])
            self.assertIs(engine.installation,selected)
            self.assertEqual(engine.root,str(root))

    def test_select_restored_installation_clears_old_uninstall_status(self):
        from stove_linux.game_auth import GAME_ID
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / 'prefix'
            library = prefix / 'drive_c/Smilegate'
            game = library / 'Games/ChaosZeroNightmare'
            (game / 'bin').mkdir(parents=True)
            (game / 'bin/ucldr_ChaosZeroNightmare_GL_loader_x64.exe').write_bytes(b'fixture')
            manifest = game / 'combinedata_manifest'
            manifest.mkdir()
            (manifest / f'GameManifest_{GAME_ID}.upf').write_text(json.dumps({'game_id':GAME_ID,'local_version':54}))
            engine = Engine(str(library))
            engine.settings_path = Path(directory) / 'config/settings.json'
            engine.job = {'kind':'uninstall','running':False,'result':{'uninstalled':True}}
            engine.detail = 'Juego enviado a la papelera.'
            with patch('oracle_engine.shutil.which', return_value='/usr/bin/wine'):
                state = engine.dispatch('settings', {'root':str(library),'runner':'wine'})
            self.assertNotIn('error', state)
            self.assertEqual(state['version'],54)
            self.assertTrue(state['game_installed'])
            self.assertIsNone(state['job'])
            self.assertNotIn('papelera',state['detail'])
            self.assertEqual(state['phase'],'signed_out')
