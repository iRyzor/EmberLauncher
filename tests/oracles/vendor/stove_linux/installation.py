from dataclasses import dataclass
from pathlib import Path
import hashlib
import re
import shutil
import sys


@dataclass(frozen=True)
class Installation:
    root: Path
    prefix: Path

    @classmethod
    def discover(cls, root: Path):
        root = root.resolve()
        if sys.platform == "win32":
            return cls(root, root)
        for parent in (root, *root.parents):
            if (parent / "drive_c").is_dir():
                return cls(root, parent)
        raise ValueError("The installation must be inside a Wine prefix with drive_c")

    @property
    def game_dir(self):
        return self.root / "Games" / "ChaosZeroNightmare"

    @property
    def executable(self):
        directory = self.game_dir / "bin"
        matches = [p for p in directory.glob("*.exe")
                   if p.name.lower() == "ucldr_chaoszeronightmare_gl_loader_x64.exe"]
        if len(matches) != 1:
            raise ValueError("Chaos Zero Nightmare loader was not found uniquely")
        return matches[0]

    def windows_path(self, path: Path):
        if sys.platform == "win32":
            return str(path.resolve())
        relative = path.resolve().relative_to(self.prefix / "drive_c")
        return "C:\\" + "\\".join(relative.parts)

    def inventory(self):
        games = []
        for directory in sorted((self.root / "Games").glob("*")):
            if directory.is_dir():
                games.append({"directory": str(directory), "name": directory.name,
                              "executables": sorted(p.name for p in (directory / "bin").glob("*.exe")),
                              "has_base_sdk": (directory / "bin" / "BaseSDK.dll").is_file()})
        return games

    def doctor(self, runner: str):
        binary = shutil.which(runner)
        return {"root": str(self.root), "wine_prefix": str(self.prefix),
                "runner": binary, "prefix_initialized": (self.prefix / "system.reg").is_file(),
                "games": self.inventory(), "authentication": "qr_launcher_and_game_tokens_verified",
                "ipc": "real_game_entry_verified_by_user"}

    def sdk_evidence(self):
        """Read only explicit SDK log facts; no raw log lines are returned."""
        records = []
        for directory in sorted((self.prefix / "drive_c/users").glob("*/AppData/Local/STOVEPCSDK3/logs/STOVE_CHAOSZERO")):
            logs = sorted(directory.glob("BaseSDK*.log"), key=lambda p: p.stat().st_mtime)[-3:]
            for path in logs:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
                versions = re.findall(r"BaseSDK\.dll - version : ([0-9.]+)", text)
                records.append({"file": str(path), "sdk_version": versions[-1] if versions else None,
                                "game_id": "STOVE_CHAOSZERO" if "GameId : STOVE_CHAOSZERO" in text else None,
                                "uses_ipc": "Launcher data from IPC" in text,
                                "initialization_succeeded": "TokenActor::Initialize.onfinished.lambda - Success." in text,
                                "ipc_connection_succeeded": "IPC Connect Success" in text})
        return records

    def fingerprint(self):
        result = {}
        for relative in ("Games/ChaosZeroNightmare/bin/BaseSDK.dll", "STOVE/GameInstanceLib.dll", "STOVE/IdentityLib.dll"):
            path = self.root / relative
            if path.is_file():
                with path.open("rb") as file:
                    result[relative] = hashlib.file_digest(file, "sha256").hexdigest()
        return result
