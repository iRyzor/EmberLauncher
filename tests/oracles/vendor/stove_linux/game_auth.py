"""The installed client's normal game-access check; no game process is started."""
from dataclasses import dataclass, field
import hashlib
import time

from .auth import AuthError, LauncherSession
from . import native_auth

GAME_ID = "STOVE_CHAOSZERO"
GAME_INSTANCE_HASH = "831eefcff54f93a87e9d6748daaf640a9f60d514286b01d1f54551ef16ab46b4"


@dataclass(frozen=True)
class GameSession:
    game_id: str
    tokens: LauncherSession = field(repr=False)
    received_at_ms: int = field(default_factory=lambda: time.time_ns() // 1_000_000, repr=False)
    user_id: str = field(default="", repr=False)

    @classmethod
    def from_value(cls, value):
        if native_auth.library is not None:
            result = native_auth.validate_game(value)
            user_id = result.pop("user_id")
            return cls(GAME_ID, LauncherSession(**result), user_id=user_id)
        tokens = LauncherSession.from_value(value)
        user = value.get("user", {})
        if not isinstance(user, dict):
            raise AuthError("STOVE devolvió metadatos de juego inesperados.")
        user_id = user.get("user_id", "")
        if not isinstance(user_id, str) or len(user_id) > 32768 or "\0" in user_id:
            raise AuthError("STOVE devolvió un identificador de juego inesperado.")
        return cls(GAME_ID, tokens, user_id=user_id)


def verify_game_profile(installation):
    with (installation.root / "STOVE/GameInstanceLib.dll").open("rb") as file:
        if hashlib.file_digest(file, "sha256").hexdigest() != GAME_INSTANCE_HASH:
            raise ValueError("The installed game authentication profile needs new research")
    if not installation.executable.is_file():
        raise ValueError("The supported game loader was not found")


def request_game_session(client, launcher_session):
    if not isinstance(launcher_session, LauncherSession):
        raise AuthError("Primero debe obtenerse una sesión del launcher.")
    # STOVE.exe's non-development call uses an empty device_key (0x705be1).
    # Both access-check skip flags remain false.
    value = client._request_url("POST", "https://s-api.onstove.com/gc/v1.4/check/" + GAME_ID, {
        "device_info": {"device_key": ""}, "gds_info": client.region.payload(),
        "skip_provider_check": False, "skip_session_check": False,
    }, native_context=True, extra_headers={
        "Authorization": "bearer " + launcher_session.access_token,
        "Captcha-Token": "", "market-name": "PC_MARKET",
    })
    return GameSession.from_value(value)
