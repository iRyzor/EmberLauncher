import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
from resumable_updates import (
    DownloadCancelled,
    stage_resumable,
    prepare_resumable,
    pending_download,
)
from stove_linux.auth import AuthError
from stove_linux.updates import PublishedVersion
from fixtures import manifest


class Response(io.BytesIO):
    def __init__(self, data, status=200, content_range=None):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Range": content_range}


class Opener:
    def __init__(self, packed, ignore_range=False, wrong_range=False):
        self.packed = packed
        self.requests = []
        self.ignore_range = ignore_range
        self.wrong_range = wrong_range

    def open(self, request, **kwargs):
        self.requests.append(request)
        value = request.get_header("Range")
        if value and not self.ignore_range:
            offset = int(value.split("=")[1].split("-")[0])
            header = f"bytes {offset}-{len(self.packed) - 1}/{len(self.packed)}"
            return Response(
                self.packed[offset:], 206, "wrong" if self.wrong_range else header
            )
        return Response(self.packed)


class ResumableTests(unittest.TestCase):
    def item(self, original=b"abc"):
        packed = gzip.compress(original)
        return dict(
            build=32,
            chunk=3,
            startpos=0,
            size=len(original),
            packed_size=len(packed),
            md5=hashlib.md5(original).hexdigest(),
            packed_md5=hashlib.md5(packed).hexdigest(),
        ), packed

    def test_resume_from_persisted_bytes_in_new_invocation(self):
        item, packed = self.item()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "download.gz").write_bytes(packed[:10])
            opener = Opener(packed)
            result = stage_resumable(item, root, opener=opener)
            self.assertEqual(result.read_bytes(), b"abc")
            self.assertEqual(opener.requests[0].get_header("Range"), "bytes=10-")
            # A subsequent run hashes the completed file and makes no request.
            cached = Opener(b"corrupt")
            self.assertEqual(stage_resumable(item, root, opener=cached), result)
            self.assertEqual(cached.requests, [])

    def test_ignored_range_restarts_instead_of_appending(self):
        item, packed = self.item()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "download.gz").write_bytes(packed[:10])
            result = stage_resumable(
                item, root, opener=Opener(packed, ignore_range=True)
            )
            self.assertEqual(result.read_bytes(), b"abc")
            self.assertEqual((root / "download.gz").read_bytes(), packed)

    def test_wrong_content_range_rejected_without_changing_partial(self):
        item, packed = self.item()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "download.gz").write_bytes(packed[:10])
            with self.assertRaises(ValueError):
                stage_resumable(item, root, opener=Opener(packed, wrong_range=True))
            self.assertEqual((root / "download.gz").read_bytes(), packed[:10])
            self.assertFalse((root / "verified.bin").exists())

    def test_cancel_mid_download_keeps_bytes_for_resume(self):
        item, packed = self.item(b"x" * 300)
        cancel = threading.Event()

        class SmallResponse(Response):
            def read(self, size=-1):
                return super().read(min(5, size))

        opener = SimpleNamespace(open=lambda *args, **kw: SmallResponse(packed))
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def progress(received, total):
                if received >= 5:
                    cancel.set()

            with self.assertRaises(DownloadCancelled):
                stage_resumable(item, root, cancel, progress, opener)
            self.assertEqual((root / "download.gz").read_bytes(), packed[:5])
            self.assertFalse((root / "verified.bin").exists())
            cancel.clear()
            result = stage_resumable(item, root, cancel, opener=Opener(packed))
            self.assertEqual(result.read_bytes(), b"x" * 300)

    def test_corrupt_cached_output_is_revalidated_and_replaced(self):
        item, packed = self.item()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "verified.bin").write_bytes(b"xyz")
            self.assertEqual(
                stage_resumable(item, root, opener=Opener(packed)).read_bytes(), b"abc"
            )

    def test_bad_compressed_digest_never_produces_verified_file(self):
        item, packed = self.item()
        item["packed_md5"] = "0" * 32
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                stage_resumable(item, root, opener=Opener(packed))
            self.assertFalse((root / "verified.bin").exists())
            self.assertFalse((root / "download.gz").exists())

    def test_connection_failure_retries_then_resume_uses_partial(self):
        item, packed = self.item()
        opener = Opener(packed)
        calls = 0

        class BrokenResponse(Response):
            def read(self, size=-1):
                if self.tell():
                    raise ConnectionResetError()
                return super().read(7)

        def open_request(request, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                return BrokenResponse(packed)
            return opener.open(request)

        with (
            tempfile.TemporaryDirectory() as folder,
            patch("resumable_updates.time.sleep"),
        ):
            result = stage_resumable(
                item, folder, opener=SimpleNamespace(open=open_request)
            )
            self.assertEqual(result.read_bytes(), b"abc")
            self.assertEqual(calls, 2)
            self.assertEqual(opener.requests[0].get_header("Range"), "bytes=7-")

    def test_repeated_network_failure_preserves_partial(self):
        item, packed = self.item()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("resumable_updates.time.sleep"),
        ):
            root = Path(folder)
            (root / "download.gz").write_bytes(packed[:7])

            def failed(*args, **kwargs):
                raise URLError("private request")

            with self.assertRaises(URLError):
                stage_resumable(item, root, opener=SimpleNamespace(open=failed))
            self.assertEqual((root / "download.gz").read_bytes(), packed[:7])

    @patch("resumable_updates.fetch_manifest")
    def test_preparation_restarted_reuses_cache_and_never_writes_game(self, fetch):
        item, packed = self.item()
        data = json.loads(manifest())
        data["version_no"] = 56
        fetch.return_value = json.dumps(data).encode()
        published = PublishedVersion(55, 56, "private-manifest-query")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            game = root / "game"
            game.mkdir()
            installation = SimpleNamespace(game_dir=game)
            cancel = threading.Event()

            def interrupted(*args):
                cancel.set()
                raise DownloadCancelled()

            with patch("resumable_updates.stage_resumable", side_effect=interrupted):
                with self.assertRaises(DownloadCancelled):
                    prepare_resumable(installation, published, root / "stage", cancel)
            self.assertEqual(
                pending_download(installation, root / "stage"),
                {"published_version": 56},
            )
            self.assertEqual(list((root / "stage").rglob("prepared.json")), [])
            cancel.clear()

            def stage(item, folder, *args):
                folder.mkdir()
                path = folder / "verified.bin"
                path.write_bytes(b"abc")
                return path

            with patch("resumable_updates.stage_resumable", side_effect=stage):
                result = prepare_resumable(
                    installation, published, root / "stage", cancel
                )
            self.assertEqual(len(list((root / "stage").glob("update-*"))), 1)
            self.assertIsNone(pending_download(installation, root / "stage"))
            self.assertEqual(list(game.iterdir()), [])
            receipt = (Path(result["prepared_directory"]) / "prepared.json").read_text()
            self.assertNotIn("private", receipt)
            self.assertEqual(fetch.call_count, 2)

    def test_symlink_cache_cannot_overwrite_external_file(self):
        item, packed = self.item()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            external = root / "external"
            external.write_bytes(b"safe")
            cache = root / "cache"
            cache.mkdir()
            (cache / "download.gz").symlink_to(external)
            with self.assertRaises(ValueError):
                stage_resumable(item, cache, opener=Opener(packed))
            self.assertEqual(external.read_bytes(), b"safe")
