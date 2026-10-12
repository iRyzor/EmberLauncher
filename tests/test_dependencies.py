import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/"tests/oracles"),str(Path(__file__).resolve().parents[1]/"tests/oracles/vendor")]
import dependencies as deps
from stove_linux.auth import AuthError

class Response(io.BytesIO):
    def __init__(self,data):
        super().__init__(data);self.headers={"Content-Length":str(len(data))}
    def geturl(self):return "https://msedge.sf.dl.delivery.mp.microsoft.com/file.exe"

class DependencyTests(unittest.TestCase):
    def test_redirects_are_https_and_microsoft_only(self):
        for url in ["http://go.microsoft.com/x","https://go.microsoft.com.evil.invalid/x","https://user@go.microsoft.com/x","https://go.microsoft.com:444/x","https://other.invalid/x"]:
            with self.assertRaises(AuthError):deps.checked_url(url)
        self.assertEqual(deps.checked_url(deps.INSTALLER_URL),deps.INSTALLER_URL)
    def test_download_cache_rejects_corruption_and_incomplete_file(self):
        with tempfile.TemporaryDirectory() as folder:
            data=b"MZ"+b"x"*(1024*1024)
            opener=Mock();opener.open.side_effect=lambda *a,**k:Response(data)
            with patch("dependencies.build_opener",return_value=opener):
                first=deps.download_installer(folder)
                self.assertEqual(first.read_bytes(),data)
                deps.download_installer(folder);self.assertEqual(opener.open.call_count,1)
                first.write_bytes(b"MZcorrupt")
                deps.download_installer(folder);self.assertEqual(opener.open.call_count,2)
                first.unlink()
                bad=Response(data);bad.headers["Content-Length"]=str(len(data)+1)
                opener.open.return_value=bad;opener.open.side_effect=None
                with self.assertRaises(AuthError):deps.download_installer(folder)
                self.assertFalse(first.exists())
                self.assertFalse(list((Path(folder)/"runtimes").glob('.webview2-*')))
    def test_symlink_cache_never_executes_or_overwrites_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/"runtimes").mkdir();original=root/"keep";original.write_text("keep")
            (root/"runtimes/MicrosoftEdgeWebView2RuntimeInstallerX64.exe").symlink_to(original)
            with self.assertRaises(AuthError):deps.download_installer(root)
            self.assertEqual(original.read_text(),"keep")
    def fixture(self,root):
        game=root/"game";(game/"bin").mkdir(parents=True)
        (game/"bin/WebView2Loader.dll").touch()
        return SimpleNamespace(prefix=root,game_dir=game,windows_path=lambda p:str(p))
    def test_probe_accepts_only_bounded_public_version(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation=self.fixture(root);(root/"webview-probe.exe").touch()
            with patch("dependencies.shutil.which",return_value="wine"),patch("dependencies.subprocess.run",return_value=SimpleNamespace(returncode=0,stdout=b"WEBVIEW2_VERSION=155.0.4283.45\r\n")) as run:
                self.assertEqual(deps.probe_runtime(installation,"wine",root/"pipe-bridge.exe"),"155.0.4283.45")
                if sys.platform!="win32":self.assertEqual(run.call_args.kwargs["env"]["WINEPREFIX"],str(root))
                run.return_value.stdout=b"PRIVATE_ACCESS_TOKEN"
                self.assertIsNone(deps.probe_runtime(installation,"wine",root/"pipe-bridge.exe"))
                run.side_effect=subprocess.TimeoutExpired("probe",20)
                self.assertIsNone(deps.probe_runtime(installation,"wine",root/"pipe-bridge.exe"))
    def test_game_running_blocks_all_changes(self):
        with patch("dependencies.require_idle",side_effect=ValueError("running")),patch("dependencies.download_installer") as download,patch("dependencies.wine_compatibility") as compat:
            with self.assertRaises(ValueError):deps.prepare_runtime(object(),"wine","bridge",Path("cache"))
            download.assert_not_called();compat.assert_not_called()
    def test_existing_runtime_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation=self.fixture(root)
            with patch("dependencies.require_idle"),patch("dependencies.probe_runtime",return_value="155.0.4283.45"),patch("dependencies.wine_compatibility"),patch("dependencies.download_installer") as download:
                result=deps.prepare_runtime(installation,"wine",root/"pipe-bridge.exe",root/"cache")
                self.assertTrue(result["runtime_ready"]);self.assertFalse(result["runtime_installed"])
                download.assert_not_called()
    def test_failed_installer_never_reports_ready_and_can_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);installation=self.fixture(root)
            with patch("dependencies.require_idle"),patch("dependencies.probe_runtime",return_value=None),patch("dependencies.wine_compatibility"),patch("dependencies.shutil.which",return_value="wine"),patch("dependencies.download_installer",return_value=root/"installer.exe"),patch("dependencies.subprocess.run",return_value=SimpleNamespace(returncode=1)):
                for _ in range(2):
                    with self.assertRaises(AuthError):deps.prepare_runtime(installation,"wine",root/"pipe-bridge.exe",root/"cache")
    def test_scoped_wine_configuration_is_not_applied_to_windows(self):
        with patch("dependencies.sys.platform","win32"),patch("dependencies.subprocess.run") as run:
            deps.wine_compatibility(object(),"native");run.assert_not_called()
