"""Review, apply and recover only this installation's prepared update."""

import hashlib
import json
from pathlib import Path
from stove_linux.auth import AuthError
from stove_linux.manifest import parse_manifest
from stove_linux.launch import SDK_HASH
from stove_linux.transaction import FileTransaction
from stove_linux.update_guard import require_idle
from stove_linux.updates import installed_update_state


def prepared_summary(directory):
    folder = Path(directory)
    raw = (folder / "manifest.json").read_bytes()
    receipt = json.loads((folder / "prepared.json").read_bytes())
    if receipt["manifest_sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("Manifest changed")
    parse_manifest(raw, receipt["published_version"])
    entries = receipt["entries"]
    changes = [
        {
            "path": x["path"],
            "action": "eliminar"
            if x["kind"] == "R"
            else "crear"
            if x["status"] == "missing"
            else "reemplazar",
        }
        for x in entries
        if x["status"] in ("missing", "modified", "remove")
    ]
    return {
        "installed_version": receipt["distribution_version"],
        "published_version": receipt["published_version"],
        "download_bytes": receipt["download_bytes"],
        "changes": changes,
        "change_count": len(changes),
    }


def recovery_needed(installation):
    if installation is None:
        return False
    journal = installation.game_dir / ".stove-update-transaction"
    return journal.exists() or journal.is_symlink()


def apply_update(installation, directory, progress=None):
    require_idle(installation)
    folder = Path(directory)
    summary = prepared_summary(folder)
    receipt = json.loads((folder / "prepared.json").read_bytes())
    # An updated SDK cannot be silently accepted by a pinned interoperability profile.
    for item in receipt["staged"]:
        if item["path"].casefold() == "bin/basesdk.dll":
            source = folder / item["staged_file"]
            if folder.resolve() not in source.resolve().parents:
                raise ValueError("Unsafe SDK path")
            with source.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != SDK_HASH:
                    raise AuthError(
                        "Esta actualización cambia el SDK de STOVE. Hace falta validar su compatibilidad antes de aplicarla."
                    )
    transaction = FileTransaction(installation.game_dir, folder)
    try:
        transaction.apply(
            summary["published_version"],
            after_operation=progress,
            installation=installation,
        )
        archive = transaction.finalize()
        state = installed_update_state(installation)
        return {
            "applied": True,
            "version": state["distribution_version"],
            "backup_directory": str(archive),
        }
    finally:
        transaction.close()


def recover_update(installation):
    require_idle(installation)
    transaction = FileTransaction(installation.game_dir, installation.game_dir)
    try:
        state = transaction.load_journal()
        if state["phase"] == "applying":
            transaction.rollback()
        archive = transaction.finalize()
        return {
            "recovered": True,
            "version": installed_update_state(installation)["distribution_version"],
            "backup_directory": str(archive),
        }
    finally:
        transaction.close()
