"""Initialize only a new, explicitly selected Wine prefix owned by Ember."""

from stove_linux.platform_fs import fcntl, os, path_without_links
import json
from pathlib import Path
import shutil
import subprocess
import time
import sys

from fresh_install import validate_destination
from stove_linux.auth import AuthError


def create_prefix(path, runner):
    try:
        prefix = path_without_links(Path(path).expanduser())
    except ValueError:
        raise AuthError("Selecciona una carpeta nueva y sin enlaces para el prefijo Wine.") from None
    if prefix == Path.home().resolve() or prefix == Path(prefix.anchor):
        raise AuthError(
            "Selecciona una carpeta nueva y sin enlaces para el prefijo Wine."
        )
    executable = shutil.which(runner) if sys.platform != "win32" else None
    if sys.platform != "win32" and not executable:
        raise AuthError("Wine no está instalado o el ejecutable indicado no existe.")
    marker = prefix / ".ember-prefix.json"
    try:
        prefix.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        if marker.is_symlink() or not marker.is_file():
            raise AuthError(
                "El destino ya existe. Usa una carpeta nueva para no modificar otro prefijo."
            ) from None
        if json.loads(marker.read_text()).get("owner") != "ember-launcher":
            raise AuthError("El destino no pertenece a un prefijo creado por Ember.")
    else:
        descriptor = os.open(
            marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"owner": "ember-launcher", "schema": 1}, stream)
            stream.flush()
            os.fsync(stream.fileno())
    lock = os.open(
        prefix / ".ember-bootstrap.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if sys.platform == "win32":
            return validate_destination(prefix / "Smilegate")
        destination = prefix / "drive_c/Smilegate"
        if (prefix / "system.reg").is_file() and (prefix / "drive_c/windows").is_dir():
            return validate_destination(destination)
        env = dict(
            os.environ,
            WINEPREFIX=str(prefix),
            WINEARCH="win64",
            WINEDEBUG="-all",
            WINEDLLOVERRIDES="mscoree,mshtml=",
        )
        try:
            result = subprocess.run(
                [executable, "wineboot.exe", "--init"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise AuthError(
                "Wine tardó demasiado al crear el prefijo. Puedes reintentar en el mismo destino de Ember."
            ) from None
        # Wine may return before its background registry writer publishes system.reg.
        deadline = time.monotonic() + 30
        while (
            result.returncode == 0
            and not (prefix / "system.reg").is_file()
            and time.monotonic() < deadline
        ):
            time.sleep(0.2)
        if (
            result.returncode != 0
            or not (prefix / "system.reg").is_file()
            or not (prefix / "drive_c/windows").is_dir()
        ):
            raise AuthError(
                "Wine no pudo completar el prefijo. Comprueba su instalación y reintenta."
            )
        return validate_destination(prefix / "drive_c/Smilegate")
    finally:
        os.close(lock)
