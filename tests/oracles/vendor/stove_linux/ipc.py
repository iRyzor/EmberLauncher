"""Launcher half of the PCSDK handshake; tokens stay on the local pipe."""
import secrets

from .crypto import checked_key
from .protocol import Packet
from . import native_sdk

# BaseSDK 3.4.0 RequiredInfoData constructor, RVA 0x3ecf0.
REQUIRED_FIELDS = (
    "game_id", "access_token", "refresh_token", "expire_in", "expire_time", "guid",
    "cafe_key", "account_type", "market_game_id", "heartbeat_interval",
    "playtime_interval", "playtime_retry_interval", "ref_session_id", "uuid",
    "service_protocol", "ref_source_type", "studio_game_yn", "dlc_list", "game_type",
    "env", "gds_isdefault_yn", "gds_nation", "gds_regulation", "gds_timezone",
    "gds_utcoffset", "language", "member_no", "member_nickname", "person_verify_yn",
    "email_verify_yn", "country_cd", "provider_cd", "streaming_yn", "exit_when_launcher_yn",
)


class IPCError(RuntimeError):
    """Fixed diagnostics, never include packet or token contents."""


class LauncherHandshake:
    def __init__(self, required_info: dict, *, key: bytes | None = None):
        try:
            native_sdk.validate_info(required_info)
        except ValueError as error:
            raise IPCError(str(error)) from None
        self._info = dict(required_info)
        self._key = checked_key(key if key is not None else secrets.token_hex(16).encode("ascii"))
        self._state = native_sdk.SDKState()
        self._closed = False

    @property
    def phase(self):
        return "closed" if self._closed else ("waiting_public_key", "information_sent")[self._state.step(2)]

    @property
    def heartbeats(self):
        return self._state.step(3) if not self._closed else self._last_heartbeats

    def receive(self, packet: Packet) -> list[Packet]:
        with self._state.lock:
            try:
                action = self._state.step(0, packet)
            except ValueError as error:
                raise IPCError(str(error)) from None
            if action == 2:
                return []
            try:
                result = native_sdk.responses(packet, self._key, self._info)
            except (ValueError, TypeError, OverflowError):
                raise IPCError("Cannot construct SDK handshake") from None
            self._state.step(1)
            return result

    def close(self):
        with self._state.lock:
            if not self._closed:
                self._last_heartbeats = self._state.step(3)
                self._info.clear()
                self._key = b""
                self._state.close()
                self._closed = True


if native_sdk.library is None:
    from .python_ipc import LauncherHandshake, IPCError
