"""Build and verify a new distribution off-tree, then publish without replacement."""

from contextlib import closing
import hashlib
import json
from stove_linux.platform_fs import os, publish_no_replace, path_without_links
from pathlib import Path
import shutil
import sqlite3
import tempfile
import sys

from stove_linux.auth import AuthError
from stove_linux.game_auth import GAME_ID
from stove_linux.installation import Installation
from stove_linux.launch import SDK_HASH
from stove_linux.manifest import parse_manifest, repair_plan
from stove_linux.transaction import FileTransaction
from stove_linux.update_guard import require_idle
from update_actions import prepared_summary


def validate_destination(root):
    try:
        supplied = path_without_links(root)
    except ValueError:
        raise AuthError("La carpeta nueva no puede atravesar enlaces simbólicos.") from None
    installation = Installation.discover(supplied)
    if sys.platform != "win32":
        try:
            installation.root.relative_to(installation.prefix / "drive_c")
        except ValueError:
            raise AuthError(
                "Selecciona una carpeta dentro de drive_c de un prefijo Wine."
            ) from None
    if sys.platform != "win32" and not (installation.prefix / "system.reg").is_file():
        raise AuthError("Selecciona un prefijo Wine ya inicializado.")
    game = installation.game_dir
    if game != game.resolve():
        raise AuthError("El destino del juego no puede atravesar enlaces simbólicos.")
    if game.exists() or game.is_symlink():
        raise AuthError(
            "Ya existe una carpeta del juego en ese destino. Selecciona una nueva."
        )
    return installation


def write_metadata(installation, game, raw, version):
    records = parse_manifest(raw, version)
    folder = game / "combinedata_manifest"
    folder.mkdir()
    database = folder / "cacheii.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE installed_info(file_name TEXT,file_size BIGINT,file_hash VARCHAR(32))"
        )
        connection.executemany(
            "INSERT INTO installed_info VALUES(?,?,?)",
            [
                (x["path"], x["size"], x["md5"].upper())
                for x in records
                if x["kind"] == "F"
            ],
        )
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("Invalid fresh inventory")
    # The minimal native distribution metadata is generated from verified facts.
    metadata = {
        "local_version": version,
        "files": [],
        "locale": "kr",
        "env": "",
        "game_id": GAME_ID,
        "install_path": installation.windows_path(installation.game_dir),
        "game_title": "Chaos Zero Nightmare",
        "type_code": 1,
        "grades": [],
        "project_url": f"https://chaoszero-dl.game.onstove.com/game/dpms_{GAME_ID}/{GAME_ID}_{version}.json",
    }
    upf = folder / f"GameManifest_{GAME_ID}.upf"
    upf.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    for path in (database, upf):
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install_prepared(installation, directory):
    validate_destination(installation.root)
    require_idle(installation)
    folder = Path(directory)
    summary = prepared_summary(folder)
    receipt = json.loads((folder / "prepared.json").read_bytes())
    if summary["installed_version"] != 0 or receipt.get("installation_root") != str(
        installation.game_dir.resolve()
    ):
        raise ValueError("Preparation does not belong to this fresh destination")
    raw = (folder / "manifest.json").read_bytes()
    records = parse_manifest(raw, summary["published_version"])
    required = {
        "bin/basesdk.dll",
        "bin/ucldr_chaoszeronightmare_gl_loader_x64.exe",
        "bin/ssr-stove-shield.exe",
    }
    if not required.issubset(
        {x["path"].casefold() for x in records if x["kind"] == "F"}
    ):
        raise ValueError("Missing launch files")
    sdk = next(
        x for x in receipt["staged"] if x["path"].casefold() == "bin/basesdk.dll"
    )
    source = folder / sdk["staged_file"]
    if source.is_symlink() or folder.resolve() not in source.resolve().parents:
        raise ValueError("Unsafe SDK source")
    with source.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SDK_HASH:
            raise AuthError(
                "El SDK publicado requiere validar un nuevo perfil antes de instalarlo."
            )
    parent = installation.game_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    # Recheck after creating parents; publication must never replace another game.
    validate_destination(installation.root)
    build = Path(tempfile.mkdtemp(prefix=".ember-install-", dir=parent))
    published = False
    try:
        transaction = FileTransaction(build, folder)
        try:
            transaction.apply(summary["published_version"])
            transaction.finalize()
        finally:
            transaction.close()
        write_metadata(installation, build, raw, summary["published_version"])
        checked = repair_plan(build, raw, summary["published_version"])
        if checked["blocked"] or any(
            x["kind"] == "F" and x["status"] != "matching" for x in checked["entries"]
        ):
            raise ValueError("Fresh verification failed")
        require_idle(installation)
        validate_destination(installation.root)
        publish_no_replace(build, installation.game_dir)
        published = True
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return {
            "installed": True,
            "version": summary["published_version"],
            "verified_files": sum(x["kind"] == "F" for x in checked["entries"]),
        }
    finally:
        if not published:
            shutil.rmtree(build)
