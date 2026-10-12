"""Public protocol metadata and the installed client's device identifier.

No session tokens or registry values other than SGUP/CallerDetail are consumed.
This temporary profile is pinned to the version researched in STOVE.exe.
"""
from dataclasses import dataclass, field
import hashlib
import re
import uuid


LAUNCHER_HASH = "e67af969b3a96e15f529bca26114978c4e089e1e99e4f2c2661b9129bcb48345"
LAUNCHER_VERSION = "3.2.29.736"


@dataclass(frozen=True)
class NativeHTTPContext:
    caller_detail: str = field(repr=False)
    transaction_id: str = field(default_factory=lambda: str(uuid.uuid4()), repr=False)

    def __post_init__(self):
        if not re.fullmatch(r"[a-f0-9]{40}", self.caller_detail):
            raise ValueError("Installed CallerDetail has an unsupported format")
        try:
            valid_uuid = str(uuid.UUID(self.transaction_id)) == self.transaction_id
        except (ValueError, AttributeError, TypeError):
            valid_uuid = False
        if not valid_uuid:
            raise ValueError("Transaction identifier must be a canonical UUID")

    @classmethod
    def from_installation(cls, installation):
        with (installation.root / "STOVE/STOVE.exe").open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != LAUNCHER_HASH:
                raise ValueError("The installed launcher HTTP profile needs to be researched again")
        # Wine escapes backslashes in registry section names. Stream the file;
        # never copy its account/session records into a diagnostic or output.
        section = ""
        matches = []
        with (installation.prefix / "user.reg").open(encoding="utf-8") as file:
            for line in file:
                if line.startswith("["):
                    section = line[1:].split("]", 1)[0].replace("\\\\", "\\").lower()
                if section == "software\\sgup" and line.startswith('"CallerDetail"='):
                    match = re.fullmatch(r'"CallerDetail"="([a-f0-9]{40})"\s*', line)
                    if not match:
                        raise ValueError("Installed CallerDetail has an unsupported format")
                    matches.append(match[1])
        if len(matches) != 1:
            raise ValueError("A unique installed SGUP CallerDetail was not found")
        return cls(matches[0])

    def headers(self, region):
        # Native Accept-Language is language-country, whereas X-Lang is language.
        values = {
            "Accept-Language": f"{region.lang}-{region.nation}",
            "User-Agent": "STOVEAPIClient/3.0",
            "Transaction-ID": self.transaction_id,
            "Caller-ID": f"STOVE_LAUNCHER_VER.{LAUNCHER_VERSION}",
            "Caller-Detail": self.caller_detail,
            "X-Lang": region.lang,
            "X-Nation": region.nation,
            "X-Timezone": region.timezone,
            "X-Utc-Offset": str(region.utc_offset),
        }
        if any(not isinstance(value, str) or not value or len(value) > 512
               or any(ord(char) < 32 or ord(char) > 126 for char in value)
               for value in values.values()):
            raise ValueError("Invalid HTTP context metadata")
        return values
