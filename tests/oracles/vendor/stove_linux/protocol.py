"""PCSDK 3.4.0 framing recovered from BaseSDK.dll.

Header: little-endian uint16 total byte length, uint16 packet ID.
Payload: UTF-8 JSON envelope. Encryption inside value is implemented in crypto.py.
"""
from dataclasses import dataclass, field
import json
import struct
from .native_protocol import encode_packet, new_decoder

HEADER = struct.Struct("<HH")
PACKET_NAMES = {1000: "PUBLIC_KEY", 1001: "HEARTBEAT", 2000: "AES_KEY", 2001: "REQUIRED_INFO", 2002: "SDK_TERMINATE"}


@dataclass(frozen=True)
class Packet:
    packet_id: int
    payload: bytes = field(repr=False)

    def encode(self) -> bytes:
        return encode_packet(self.packet_id, self.payload)

    def summary(self) -> dict:
        """Only report structure, never account values, keys or tokens."""
        result = {"packet_id": self.packet_id,
                  "name": PACKET_NAMES.get(self.packet_id, "UNKNOWN"),
                  "payload_bytes": len(self.payload)}
        try:
            envelope = json.loads(self.payload)
        except (ValueError, UnicodeDecodeError):
            result["format"] = "opaque"
            return result
        result["format"] = "json"
        if isinstance(envelope, dict):
            # Only known protocol keys; arbitrary keys could themselves be secrets.
            known = {"code", "message", "value", "publicKey", "processId", "aesKey"}
            result["envelope_fields"] = sorted(set(envelope) & known)
            value = envelope.get("value")
            if isinstance(value, dict):
                result["value_fields"] = sorted(set(value) & known)
        return result


class Decoder:
    """Rust stream decoder; Python oracle is available for source development only."""
    def __init__(self):
        self._decoder = new_decoder()

    def feed(self, data: bytes) -> list[Packet]:
        return [Packet(packet_id, payload) for packet_id, payload in self._decoder.feed(data)]

    def finish(self):
        self._decoder.finish()

    def close(self):
        self._decoder.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
