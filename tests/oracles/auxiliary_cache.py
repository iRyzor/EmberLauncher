"""Preserve the locally validated auxiliary component without account data."""

import hashlib
import os
from pathlib import Path
import shutil
import tempfile

from stove_linux.auth import AuthError
from stove_linux.updates import installed_update_state

EXPECTED_HASH = "c9bcc03c6379132428fb82386599bebde6b4ab5171402e16340f467813604492"
EXPECTED_SIZE = 12242944
RELATIVE = "bin/custodiap/jacksparrow_x64.smt"
SUPPORTED_VERSION = 55


def verified(path):
    path = Path(path)
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        return False
    with path.open("rb") as stream:
        return (
            os.fstat(stream.fileno()).st_size == EXPECTED_SIZE
            and hashlib.file_digest(stream, "sha256").hexdigest() == EXPECTED_HASH
        )


def cache_path(directory):
    return Path(directory).absolute() / "auxiliary" / (EXPECTED_HASH + ".smt")


def copy_verified(source, target):
    if not verified(source):
        raise AuthError(
            "El componente auxiliar no coincide con la referencia validada."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.resolve() != target.parent:
        raise AuthError("La caché auxiliar no puede atravesar enlaces simbólicos.")
    descriptor, name = tempfile.mkstemp(prefix=".ember-aux-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as stream:
            shutil.copyfileobj(stream, output)
            output.flush()
            os.fsync(output.fileno())
        if not verified(temporary):
            raise AuthError("No se pudo verificar la copia del componente auxiliar.")
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            if not verified(target):
                raise AuthError(
                    "Existe un componente auxiliar distinto; no se reemplazó."
                ) from None
        descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def remember(installation, directory):
    target = cache_path(directory)
    if verified(target):
        return True
    source = installation.game_dir / RELATIVE
    if installed_update_state(installation)[
        "distribution_version"
    ] != SUPPORTED_VERSION or not verified(source):
        return False
    copy_verified(source, target)
    return True


def install(build, version, directory):
    if version != SUPPORTED_VERSION:
        raise AuthError("El componente auxiliar de esta versión requiere validación.")
    source = cache_path(directory)
    if not verified(source):
        raise AuthError(
            "Falta el componente auxiliar validado. Selecciona primero una instalación funcional para importarlo a la caché de Ember."
        )
    copy_verified(source, Path(build) / RELATIVE)
