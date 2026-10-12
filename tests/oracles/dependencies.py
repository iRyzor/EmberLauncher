"""Prepare the game's official WebView2 runtime without using STOVE or account data."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from stove_linux.auth import AuthError
from stove_linux.platform_fs import os as fs_os, fcntl
from stove_linux.update_guard import require_idle

INSTALLER_URL = "https://go.microsoft.com/fwlink/?linkid=2124701"
ALLOWED_HOSTS = {"go.microsoft.com", "msedge.sf.dl.delivery.mp.microsoft.com"}
MAX_INSTALLER_BYTES = 300 * 1024 * 1024


def checked_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.username or parsed.password or parsed.port not in (None,443):
        raise AuthError("Microsoft devolvió una dirección de descarga inesperada.")
    return url


class MicrosoftRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def download_installer(cache, progress=lambda _: None):
    cache = Path(cache) / "runtimes"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    receipt = target.with_suffix(".json")
    if target.is_file() and not target.is_symlink() and receipt.is_file() and not receipt.is_symlink():
        try:
            metadata = json.loads(receipt.read_text())
            with target.open("rb") as stream:
                if isinstance(metadata, dict) and metadata.get("source") == INSTALLER_URL and 0 <= time.time() - metadata.get("downloaded_at", 0) < 30 * 86400 and metadata.get("sha256") == hashlib.file_digest(stream, "sha256").hexdigest() and 1024 * 1024 < target.stat().st_size <= MAX_INSTALLER_BYTES:
                    return target
        except (OSError,ValueError,TypeError):
            pass
    if target.is_symlink() or receipt.is_symlink():
        raise AuthError("La caché de dependencias contiene un enlace inesperado.")
    temporary = None
    try:
        with build_opener(MicrosoftRedirect()).open(checked_url(INSTALLER_URL), timeout=30) as response:
            checked_url(response.geturl())
            total = int(response.headers.get("Content-Length", 0))
            if total < 0 or total > MAX_INSTALLER_BYTES:
                raise AuthError("El instalador de Microsoft tiene un tamaño inesperado.")
            digest = hashlib.sha256()
            count = 0
            with tempfile.NamedTemporaryFile(dir=cache, prefix=".webview2-", delete=False) as stream:
                temporary = Path(stream.name)
                while chunk := response.read(1024 * 1024):
                    if count == 0 and not chunk.startswith(b"MZ"):
                        raise AuthError("Microsoft no devolvió un instalador válido.")
                    count += len(chunk)
                    if count > MAX_INSTALLER_BYTES:
                        raise AuthError("El instalador supera el tamaño permitido.")
                    stream.write(chunk); digest.update(chunk)
                    progress({"phase":"runtime_download", "downloaded_bytes":count, "total_bytes":total})
                stream.flush();os.fsync(stream.fileno())
            if count <= 1024 * 1024 or (total and count != total):
                raise AuthError("La descarga del runtime quedó incompleta. Reintenta la preparación.")
        os.replace(temporary, target)
        temporary = None
        # A receipt protects reuse against partial/corrupt downloads, not a publisher signature.
        metadata = {"source":INSTALLER_URL,"sha256":digest.hexdigest(),"bytes":count,"downloaded_at":time.time()}
        with tempfile.NamedTemporaryFile(dir=cache, mode="w", prefix=".receipt-",delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(metadata,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary, receipt);temporary=None
        return target
    except AuthError:
        raise
    except (OSError,ValueError):
        raise AuthError("No se pudo descargar WebView2 desde Microsoft. Comprueba la conexión y reintenta.") from None
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def environment(installation):
    return dict(os.environ) if sys.platform == "win32" else dict(os.environ,WINEPREFIX=str(installation.prefix),WINEDEBUG="-all")


def command(runner, *args):
    if sys.platform == "win32":return list(map(str,args))
    executable=shutil.which(runner)
    if not executable:raise AuthError("No se encontró el ejecutable Wine indicado.")
    return [executable,*map(str,args)]


def probe_runtime(installation, runner, helper):
    sdk = installation.game_dir / "bin/WebView2Loader.dll"
    probe = Path(helper).with_name("webview-probe.exe")
    if not sdk.is_file() or not probe.is_file():return None
    try:
        result = subprocess.run(command(runner,probe,installation.windows_path(sdk)), env=environment(installation), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, creationflags=0x08000000 if sys.platform == "win32" else 0)
    except (OSError,subprocess.TimeoutExpired):return None
    match = re.fullmatch(rb"WEBVIEW2_VERSION=([0-9]+(?:\.[0-9]+){3})\r?\n", result.stdout)
    return match.group(1).decode("ascii") if result.returncode==0 and match else None


def runtime_files(installation):
    if sys.platform == "win32":
        folders = [Path(os.environ.get(key,""))/"Microsoft/EdgeWebView/Application" for key in ("PROGRAMFILES(X86)","PROGRAMFILES","LOCALAPPDATA") if os.environ.get(key)]
    else:
        folders = [installation.prefix/"drive_c/Program Files (x86)/Microsoft/EdgeWebView/Application", installation.prefix/"drive_c/Program Files/Microsoft/EdgeWebView/Application"]
        folders += list((installation.prefix/"drive_c/users").glob("*/AppData/Local/Microsoft/EdgeWebView/Application"))
    for folder in folders:
        for binary in folder.glob("*/msedgewebview2.exe"):
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}",binary.parent.name) and binary.is_file() and (binary.parent/"msedge.dll").is_file() and (binary.parent/"icudtl.dat").is_file():
                return True
    return False


def wine_compatibility(installation, runner):
    if sys.platform == "win32":return
    # Match the scoped Wine workarounds upstream Winetricks applies to WebView2.
    for key,name,kind,value in [
        (r"HKCU\Software\Wine\AppDefaults\msedgewebview2.exe","Version","REG_SZ","win7"),
        (r"HKLM\System\CurrentControlSet\Services\edgeupdate","Start","REG_DWORD","3"),
    ]:
        result=subprocess.run(command(runner,"reg","add",key,"/v",name,"/t",kind,"/d",value,"/f"),env=environment(installation),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
        if result.returncode:raise AuthError("No se pudo preparar la compatibilidad de WebView2 en este prefijo.")


def require_runtime(installation, runner, helper):
    if not probe_runtime(installation,runner,helper):
        raise AuthError("No se detecta WebView2 para el juego. Abre Ajustes → Preparar dependencias y vuelve a intentarlo.")


def prepare_runtime(installation, runner, helper, cache, progress=lambda _: None):
    require_idle(installation)
    lock=fs_os.open(installation.prefix/".ember-runtime.lock",fs_os.O_CREAT|fs_os.O_RDWR|fs_os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        require_idle(installation)
        has_sdk=(installation.game_dir/"bin/WebView2Loader.dll").is_file()
        ready=probe_runtime(installation,runner,helper) if has_sdk else runtime_files(installation)
        installed=False
        timed_out=False
        if not ready:
            installer=download_installer(cache,progress)
            require_idle(installation)
            progress({"phase":"runtime_install"})
            try:
                subprocess.run(command(runner,installer,"/silent","/install"),env=environment(installation),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180,creationflags=0x08000000 if sys.platform == "win32" else 0)
            except subprocess.TimeoutExpired:
                timed_out=True
            installed=True
        wine_compatibility(installation,runner)
        version=probe_runtime(installation,runner,helper) if has_sdk else None
        if not (version if has_sdk else runtime_files(installation)):
            raise AuthError("WebView2 aún no se detecta en este prefijo. La instalación no terminó correctamente; reintenta Preparar dependencias.")
        return {"runtime_ready":True,"runtime_installed":installed,"runtime_version":version,"installer_timed_out":timed_out}
    finally:
        fs_os.close(lock)
