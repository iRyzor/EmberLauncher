"""Persistent, verified package cache. Downloads never alter installed game files."""

from stove_linux.platform_fs import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import tempfile
from urllib.error import URLError
from urllib.request import Request, build_opener

from stove_linux.auth import AuthError
from stove_linux.downloads import package_url
from stove_linux.manifest import NoRedirect, fetch_manifest, repair_plan


class DownloadCancelled(Exception):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()


def verified(path, size, md5, cancel=None):
    if path.is_symlink():
        raise ValueError("Unsafe cache file")
    if not path.is_file() or path.stat().st_size != size:
        return False
    with path.open("rb") as stream:
        digest = hashlib.md5(usedforsecurity=False)
        while data := stream.read(1024 * 1024):
            check_cancel(cancel)
            digest.update(data)
        return digest.hexdigest() == md5


def atomic_json(path, value):
    temporary = path.with_suffix(".new")
    if temporary.is_symlink() or path.is_symlink():
        raise ValueError("Unsafe cache file")
    with temporary.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def stage_resumable(item, directory, cancel=None, progress=None, opener=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise ValueError("Unsafe cache directory")
    packed, output = directory / "download.gz", directory / "verified.bin"
    for path in (packed, output, directory / "expanded.tmp"):
        if path.is_symlink():
            raise ValueError("Unsafe cache file")
    check_cancel(cancel)
    if verified(output, item["size"], item["md5"], cancel):
        if progress:
            progress(item["packed_size"], item["packed_size"])
        return output
    output.unlink(missing_ok=True)
    opener = opener or build_opener(NoRedirect())
    for attempt in range(3):
        check_cancel(cancel)
        offset = packed.stat().st_size if packed.exists() else 0
        if offset > item["packed_size"]:
            packed.unlink()
            offset = 0
        if offset == item["packed_size"]:
            break
        if progress:
            progress(offset, item["packed_size"])
        request = Request(package_url(item), headers={"Accept-Encoding": "identity"})
        if offset:
            request.add_header("Range", f"bytes={offset}-")
        try:
            with opener.open(request, timeout=10) as response:
                if response.status == 206:
                    expected = f"bytes {offset}-{item['packed_size'] - 1}/{item['packed_size']}"
                    if response.headers.get("Content-Range") != expected:
                        raise ValueError("Unexpected partial response")
                    mode = "ab"
                elif response.status == 200:
                    # A CDN may ignore Range. Restart this package, never append.
                    offset, mode = 0, "wb"
                else:
                    raise ValueError("Unexpected package response")
                with packed.open(mode) as stream:
                    try:
                        while True:
                            check_cancel(cancel)
                            data = response.read(
                                min(256 * 1024, item["packed_size"] - offset + 1)
                            )
                            if not data:
                                break
                            offset += len(data)
                            if offset > item["packed_size"]:
                                raise ValueError("Package larger than manifest")
                            stream.write(data)
                            if progress:
                                progress(offset, item["packed_size"])
                    finally:
                        stream.flush()
                        os.fsync(stream.fileno())
            if offset != item["packed_size"]:
                raise URLError("Interrupted package")
            break
        except (URLError, socket.timeout, ConnectionError):
            if attempt == 2:
                raise
            if cancel is not None:
                if cancel.wait(0.5 * (attempt + 1)):
                    raise DownloadCancelled()
            else:
                time.sleep(0.5 * (attempt + 1))
    check_cancel(cancel)
    if not verified(packed, item["packed_size"], item["packed_md5"], cancel):
        packed.unlink(missing_ok=True)
        raise ValueError("Package integrity mismatch")
    temporary = directory / "expanded.tmp"
    digest = hashlib.md5(usedforsecurity=False)
    size = 0
    try:
        with gzip.open(packed, "rb") as source, temporary.open("wb") as stream:
            while True:
                check_cancel(cancel)
                data = source.read(min(256 * 1024, item["size"] - size + 1))
                if not data:
                    break
                size += len(data)
                if size > item["size"]:
                    raise ValueError("Expanded package larger than manifest")
                digest.update(data)
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if size != item["size"] or digest.hexdigest() != item["md5"]:
            raise ValueError("Expanded package integrity mismatch")
        os.replace(temporary, output)
        return output
    finally:
        temporary.unlink(missing_ok=True)


def pending_download(installation, staging):
    if installation is None:
        return None
    for receipt in sorted(Path(staging).glob("update-*/downloading.json")):
        try:
            if receipt.is_symlink() or receipt.parent.is_symlink():
                continue
            value = json.loads(receipt.read_text())
            if (
                not isinstance(value, dict)
                or type(value.get("published_version")) is not int
            ):
                continue
            if value.get("root") == str(installation.game_dir.resolve()):
                return {"published_version": value["published_version"]}
        except (OSError, ValueError, KeyError):
            continue
    return None


def prepare_resumable(installation, published, destination, cancel=None, progress=None):
    if published.latest < published.installed:
        raise AuthError(
            "La versión publicada es anterior: no se preparará una degradación."
        )
    destination = Path(destination).absolute()
    game = installation.game_dir.resolve()
    if destination.resolve() == game or game in destination.resolve().parents:
        raise AuthError("La carpeta temporal debe estar fuera del juego.")
    descriptor = None
    try:
        check_cancel(cancel)
        if progress:
            progress({"phase": "manifest", "downloaded_bytes": 0, "total_bytes": 0})
        raw = fetch_manifest(published.latest, published.project_url)
        check_cancel(cancel)
        fresh = not game.exists() and not game.is_symlink()
        if not fresh:
            plan = repair_plan(game, raw, published.latest)
        else:
            # A fresh destination remains absent until the complete verified tree
            # is published. Derive the initial plan against a disposable empty root.
            destination.mkdir(parents=True, exist_ok=True)
            if destination.is_symlink():
                raise ValueError("Unsafe cache directory")
            with tempfile.TemporaryDirectory(prefix="empty-", dir=destination) as empty:
                plan = repair_plan(empty, raw, published.latest)
        if plan["blocked"]:
            raise ValueError("Blocked plan")
        destination.mkdir(parents=True, exist_ok=True)
        if destination.is_symlink():
            raise ValueError("Unsafe cache directory")
        cache_key = hashlib.sha256(os.fsencode(game) + b"\0" + raw).hexdigest()
        work = destination / ("update-" + cache_key)
        work.mkdir(mode=0o700, exist_ok=True)
        if work.is_symlink() or (work / ".lock").is_symlink():
            raise ValueError("Unsafe cache directory")
        descriptor = os.open(work / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic_json(
            work / "downloading.json",
            {
                "root": str(game),
                "published_version": published.latest,
            },
        )
        plan.update(
            distribution_version=published.installed,
            published_version=published.latest,
            remote_latest_checked=True,
            installation_root=str(game),
            fresh_install=fresh,
        )
        plan["staged"] = []
        items = [
            x
            for x in plan["entries"]
            if x["kind"] == "F" and x["status"] in ("missing", "modified")
        ]
        completed = 0
        for index, item in enumerate(items):
            check_cancel(cancel)

            def report(received, total):
                if progress:
                    progress(
                        {
                            "phase": "download",
                            "downloaded_bytes": completed + received,
                            "total_bytes": plan["download_bytes"],
                            "package_index": index + 1,
                            "package_count": len(items),
                            "path": item["path"],
                        }
                    )

            key = hashlib.sha256(
                json.dumps(
                    {
                        k: item[k]
                        for k in (
                            "build",
                            "chunk",
                            "startpos",
                            "size",
                            "packed_size",
                            "md5",
                            "packed_md5",
                        )
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            path = stage_resumable(item, work / ("package-" + key), cancel, report)
            plan["staged"].append(
                {"path": item["path"], "staged_file": str(path.relative_to(work))}
            )
            completed += item["packed_size"]
        check_cancel(cancel)
        manifest = work / "manifest.json"
        if manifest.is_symlink():
            raise ValueError("Unsafe manifest")
        with manifest.open("wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        atomic_json(work / "prepared.json", plan)
        (work / "downloading.json").unlink(missing_ok=True)
        if progress:
            progress(
                {
                    "phase": "ready",
                    "downloaded_bytes": completed,
                    "total_bytes": plan["download_bytes"],
                    "package_count": len(items),
                }
            )
        return {
            **published.summary(),
            "prepared": True,
            "prepared_directory": str(work),
            "download_bytes": plan["download_bytes"],
            "staged_packages": len(items),
            "installed_files_changed": False,
        }
    except (OSError, ValueError, EOFError):
        raise AuthError(
            "La descarga no se completó. Puedes reanudarla; no se modificaron archivos del juego."
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
