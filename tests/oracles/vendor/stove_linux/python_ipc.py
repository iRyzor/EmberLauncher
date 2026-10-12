"""Launcher half of the PCSDK handshake; tokens stay on the local pipe."""
import json
import secrets

from .python_crypto import aes_key_packet, checked_key, encrypted_packet
from .protocol import Packet

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
        if not isinstance(required_info, dict) or set(required_info) - set(REQUIRED_FIELDS):
            raise IPCError("Unknown SDK information field")
        if any(not isinstance(v, str) or len(v) > 32768 or "\x00" in v for v in required_info.values()):
            raise IPCError("SDK information fields must be bounded strings")
        if not required_info.get("access_token") or not required_info.get("game_id"):
            raise IPCError("SDK access token and game ID are required")
        self._info = dict(required_info)
        self._key = checked_key(key if key is not None else secrets.token_hex(16).encode("ascii"))
        self.phase = "waiting_public_key"
        self.heartbeats = 0

    def receive(self, packet: Packet) -> list[Packet]:
        try:
            envelope = json.loads(packet.payload)
        except (ValueError, UnicodeError):
            raise IPCError("Invalid SDK JSON packet") from None
        if not isinstance(envelope, dict) or type(envelope.get("code")) is not int or envelope["code"] != 0:
            raise IPCError("SDK packet reports an error or invalid envelope")
        if packet.packet_id == 1000:
            if self.phase != "waiting_public_key":
                raise IPCError("Unexpected repeated SDK public key")
            value = envelope.get("value")
            if not isinstance(value, dict):
                raise IPCError("Invalid SDK public key envelope")
            pid = value.get("processId")
            # The native SDK converts GetCurrentProcessId to a decimal string.
            if not isinstance(pid, str) or not 1 <= len(pid) <= 10 or not pid.isascii() or not pid.isdecimal() or not 0 < int(pid) <= 0xffffffff:
                raise IPCError("Invalid SDK process identifier")
            try:
                result = [aes_key_packet(value.get("publicKey"), self._key),
                          encrypted_packet(2001, self._key, self._info)]
                for response in result:
                    response.encode()  # reject oversize frames before writing either response
            except (ValueError, TypeError, OverflowError):
                raise IPCError("Cannot construct SDK handshake") from None
            self.phase = "information_sent"
            return result
        if packet.packet_id == 1001:
            if self.phase != "information_sent" or envelope.get("value") != "":
                raise IPCError("Unexpected SDK heartbeat")
            self.heartbeats += 1
            return []  # SDK dispatches no launcher heartbeat-response packet ID.
        raise IPCError("Unsupported SDK packet ID")

    def close(self):
        self._info.clear()
        self._key = b""
        self.phase = "closed"
