"""Cache only the researched device/protocol profile, never account tokens."""

import hashlib
import json
from stove_linux.platform_fs import os
import secrets
from pathlib import Path
from stove_linux.http_context import NativeHTTPContext, LAUNCHER_HASH
from stove_linux.version_context import NativeVersionContext


def bootstrap_profile(installation, config):
    """Create an Ember device identity using bundled public protocol constants."""
    resource = Path(__file__).resolve().parents[2] / "engine/runtime/bootstrap-profile.json"
    value = json.loads(resource.read_text())
    if value.get("profile_hash") != LAUNCHER_HASH:
        raise ValueError("Unsupported bootstrap protocol profile")
    version = NativeVersionContext(value["version_suffix"].encode("ascii"))
    identifier = hashlib.sha256(os.fsencode(installation.root)).hexdigest()[:16]
    path = Path(config) / f"protocol-{identifier}.json"
    if path.exists():
        return load_profile(installation, config)
    value = {
        "profile_hash": LAUNCHER_HASH,
        "caller_detail": secrets.token_hex(20),
        "version_suffix": version.suffix.decode("ascii"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return NativeHTTPContext(value["caller_detail"]), version


def load_profile(installation, config):
    identifier = hashlib.sha256(os.fsencode(installation.root)).hexdigest()[:16]
    path = Path(config) / f"protocol-{identifier}.json"
    if path.is_file():
        value = json.loads(path.read_text())
        if value.get("profile_hash") != LAUNCHER_HASH:
            raise ValueError("Unsupported protocol profile")
        return NativeHTTPContext(value["caller_detail"]), NativeVersionContext(
            value["version_suffix"].encode("ascii")
        )
    if (
        not (installation.root / "STOVE/STOVE.exe").is_file()
        or not (installation.prefix / "user.reg").is_file()
    ):
        return bootstrap_profile(installation, config)
    http = NativeHTTPContext.from_installation(installation)
    version = NativeVersionContext.from_installation(installation)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(
            {
                "profile_hash": LAUNCHER_HASH,
                "caller_detail": http.caller_detail,
                "version_suffix": version.suffix.decode("ascii"),
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return http, version


def transfer_profile(source, destination, config):
    """Copy validated protocol/device metadata, never account sessions."""
    http, version = load_profile(source, config)
    identifier = hashlib.sha256(os.fsencode(destination.root)).hexdigest()[:16]
    path = Path(config) / f"protocol-{identifier}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(
        temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as stream:
        json.dump(
            {
                "profile_hash": LAUNCHER_HASH,
                "caller_detail": http.caller_detail,
                "version_suffix": version.suffix.decode("ascii"),
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
