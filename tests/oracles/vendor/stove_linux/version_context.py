"""Native distribution ticket for the installed, pinned launcher profile."""
from dataclasses import dataclass, field
import hashlib
import hmac
import re
import struct

from .game_auth import GAME_ID
from .http_context import LAUNCHER_HASH


@dataclass(frozen=True)
class NativeVersionContext:
    suffix: bytes = field(repr=False)

    def __post_init__(self):
        if not isinstance(self.suffix, bytes) or not re.fullmatch(rb"[0-9a-fA-F]{64}", self.suffix):
            raise ValueError("Unsupported native version ticket profile")

    @classmethod
    def from_installation(cls, installation):
        data = (installation.root / "STOVE/STOVE.exe").read_bytes()
        if hashlib.sha256(data).hexdigest() != LAUNCHER_HASH:
            raise ValueError("The native version profile needs new research")
        # Four MOVDQA loads at 0x41367d..0x4136ac build the 64 ASCII
        # bytes copied to the protocol global +0x37220d0. No decoding.
        pe = struct.unpack_from("<I", data, 0x3c)[0]
        count = struct.unpack_from("<H", data, pe + 6)[0]
        optional_size = struct.unpack_from("<H", data, pe + 20)[0]
        sections = pe + 24 + optional_size
        chunks = []
        for rva in (0x34e7a80, 0x34e7c10, 0x34e78b0, 0x34e78a0):
            for index in range(count):
                entry = sections + 40 * index
                _, address, raw_size, offset = struct.unpack_from("<IIII", data, entry + 8)
                if address <= rva and rva + 16 <= address + raw_size:
                    chunks.append(data[offset + rva - address:offset + rva - address + 16])
                    break
            else:
                raise ValueError("Native version profile constant was not found")
        return cls(b"".join(chunks))

    def ticket(self, member_no):
        if not isinstance(member_no, str) or not re.fullmatch(r"[0-9]{1,32}", member_no):
            raise ValueError("A valid session member identifier is required")
        # 0x6da120 uppercases the game ID and joins member_no + ':' + suffix.
        # 0x6da45a invokes HMAC(EVP_sha256, key=joined, data=game_id).
        key = member_no.encode("ascii") + b":" + self.suffix
        return hmac.new(key, GAME_ID.upper().encode("ascii"), hashlib.sha256).hexdigest()
