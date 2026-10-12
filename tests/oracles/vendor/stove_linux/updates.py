"""Read the native distribution manifest without modifying installed files."""
import json
import sqlite3
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit, urlunsplit

from .auth import AuthError, LauncherSession
from .game_auth import GAME_ID

GAME_CDN = "chaoszero-dl.game.onstove.com"


def manifest_url(url, version):
    """Normalize the known native HTTP CDN URL to its verified HTTPS endpoint."""
    if not isinstance(url, str) or not 1 <= len(url) <= 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise AuthError("STOVE no devolvió un manifiesto de versión válido.")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname == GAME_CDN
        valid = valid and not parsed.username and not parsed.password and not parsed.fragment
        allowed_port = 80 if parsed.scheme == "http" else 443
        valid = valid and parsed.port in (None, allowed_port)
        valid = valid and parsed.path == f"/game/dpms_{GAME_ID}/{GAME_ID}_{version}.json"
    except ValueError:
        valid = False
    if not valid:
        raise AuthError("STOVE devolvió una dirección de manifiesto inesperada.")
    return urlunsplit(("https", GAME_CDN, parsed.path, parsed.query, ""))


@dataclass(frozen=True)
class PublishedVersion:
    installed: int
    latest: int
    project_url: str = field(repr=False)

    @classmethod
    def from_value(cls, value, installed):
        # Shared LiveVersionData keys also appear in the installed Python updater.
        if not isinstance(value, dict) or type(installed) is not int or installed < 0:
            raise AuthError("La respuesta de versiones no tiene el formato esperado.")
        latest = value.get("live_version")
        if type(latest) is not int or latest < 0:
            raise AuthError("STOVE no devolvió una versión publicada válida.")
        url = value.get("live_project_file_url") or value.get("live_project_url")
        return cls(installed, latest, manifest_url(url, latest))

    def summary(self):
        return {"game_id": GAME_ID, "distribution_version": self.installed,
                "published_version": self.latest, "remote_checked": True,
                "update_available": self.latest > self.installed,
                "remote_older": self.latest < self.installed}


def request_published_version(client, session, installed, version_context):
    if not isinstance(session, LauncherSession):
        raise AuthError("Primero debe obtenerse una sesión del launcher.")
    if type(installed) is not int or installed < 0:
        raise AuthError("No se conoce la versión instalada del juego.")
    from .version_context import NativeVersionContext
    if not isinstance(version_context, NativeVersionContext):
        raise AuthError("Falta el perfil nativo de consulta de versiones.")
    try:
        ticket = version_context.ticket(session.member_no)
    except ValueError:
        raise AuthError("La sesión no contiene el identificador necesario para consultar versiones.") from None
    # Native MakeService 0x45ffc0..0x4601bd: boolean string and ticket header.
    query = urlencode({"game_id": GAME_ID, "local_version": installed, "pc_room": "false"})
    try:
        value = client._request_url("GET", "https://api.onstove.com/dpms/game/v3.1/live_version?" + query,
            native_context=True, extra_headers={"Authorization": "bearer " + session.access_token,
                                                "ticket": ticket})
    except AuthError as error:
        if type(error.code) is int:
            raise AuthError(f"STOVE rechazó la consulta de versiones (código {error.code}).", code=error.code) from None
        raise
    return PublishedVersion.from_value(value, installed)


def installed_update_state(installation):
    directory = installation.game_dir / "combinedata_manifest"
    manifest = directory / ("GameManifest_" + GAME_ID + ".upf")
    result = {"game_id": GAME_ID, "distribution_version": None,
              "tracked_files": None, "tracked_bytes": None,
              "remote_checked": False, "update_available": None}
    if manifest.is_file():
        with manifest.open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("Distribution manifest exceeds the expected size")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("game_id") != GAME_ID:
            raise ValueError("Unexpected distribution manifest")
        version = value.get("local_version")
        if type(version) is not int or version < 0:
            raise ValueError("Unexpected distribution version")
        result["distribution_version"] = version
    database = directory / "cacheii.db"
    if database.is_file():
        # URI mode=ro also refuses to create an absent database.
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error:
            raise ValueError("Cannot read the installed file inventory") from None
        try:
            connection.execute("PRAGMA query_only=ON")
            count, total, invalid = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(file_size), 0), "
                "SUM(CASE WHEN typeof(file_size) != 'integer' OR file_size < 0 THEN 1 ELSE 0 END) "
                "FROM installed_info").fetchone()
            if invalid:
                raise ValueError("Invalid installed file inventory")
            result.update(tracked_files=count, tracked_bytes=total)
        except sqlite3.Error:
            raise ValueError("Cannot read the installed file inventory") from None
        finally:
            connection.close()
    return result
