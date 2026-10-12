"""Remove only the selected game directory through the desktop trash service."""

import os
import sys
from send2trash import send2trash
import shutil
import subprocess

from stove_linux.auth import AuthError
from stove_linux.installation_lock import acquire
from stove_linux.update_guard import require_idle
from stove_linux.updates import installed_update_state


def validate(installation, confirmed_path):
    if installation is None:
        raise AuthError("No hay una instalación seleccionada.")
    game = installation.game_dir
    if confirmed_path != str(game):
        raise AuthError("La instalación seleccionada cambió. Revisa de nuevo la ruta.")
    try:
        if sys.platform != "win32":
            game.relative_to(installation.prefix / "drive_c")
    except ValueError:
        raise AuthError("La carpeta del juego debe estar dentro de drive_c.") from None
    if game.is_symlink() or game.resolve() != game or not game.is_dir():
        raise AuthError(
            "La carpeta del juego no es un destino válido para desinstalar."
        )
    if os.path.ismount(game):
        raise AuthError(
            "No se puede desinstalar una carpeta que sea un punto de montaje."
        )
    if installed_update_state(installation)["distribution_version"] is None:
        raise AuthError("No se pudo verificar la instalación seleccionada.")
    installation.executable
    return game


def uninstall(installation, confirmed_path):
    game = validate(installation, confirmed_path)
    executable = shutil.which("gio")
    if sys.platform == "linux" and not executable:
        raise AuthError(
            "Se necesita gio para enviar el juego a la papelera del sistema."
        )
    try:
        require_idle(installation)
        descriptor = acquire(game)
    except (OSError, ValueError):
        raise AuthError(
            "Cierra el juego y completa las operaciones pendientes antes de desinstalar."
        ) from None
    try:
        validate(installation, confirmed_path)
        try:
            require_idle(installation)
        except ValueError:
            raise AuthError("Cierra el juego antes de desinstalar.") from None
        identity = os.fstat(descriptor)
        # The lock file must still belong to the selected directory.
        current = (game / ".stove-update.lock").stat(follow_symlinks=False)
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise AuthError("La instalación cambió durante la operación.")
        if sys.platform == "linux":
            result = subprocess.run(
                [executable, "trash", "--", str(game)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            failed = result.returncode != 0
        else:
            try:
                send2trash(str(game))
                failed = False
            except OSError:
                failed = True
        if failed or game.exists() or game.is_symlink():
            raise AuthError(
                "No se pudo enviar el juego a la papelera. Revisa la carpeta antes de reintentar."
            )
        return {"uninstalled": True, "trashed": True, "path": str(game)}
    finally:
        os.close(descriptor)
