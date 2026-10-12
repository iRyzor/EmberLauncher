"""Historical controller oracle for regression tests; no executable entry point."""

import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).parent / "vendor"))
from stove_linux.platform_fs import os
from stove_linux.auth import AuthError, QRClient
from stove_linux.installation import Installation
from stove_linux.http_context import NativeHTTPContext
from stove_linux.version_context import NativeVersionContext
from stove_linux.updates import installed_update_state, request_published_version
from stove_linux.manifest import fetch_manifest, repair_plan
from resumable_updates import prepare_resumable, pending_download, DownloadCancelled
from stove_linux.game_auth import request_game_session
from stove_linux.launch import GameLauncher
from stove_linux.update_guard import require_idle
from protocol_profile import load_profile, transfer_profile, bootstrap_profile
from bootstrap import create_prefix
from dependencies import prepare_runtime, require_runtime
from app_paths import config_dir, cache_dir, default_root
from fresh_install import validate_destination, install_prepared
from uninstall import uninstall
from game_process import GameProcesses
from session_access import (
    authenticated_request,
    refresh_launcher_session,
    SessionExpired,
)
from update_actions import (
    prepared_summary,
    recovery_needed,
    apply_update,
    recover_update,
)

DEFAULT_ROOT = str(default_root())


class Engine:
    def __init__(self, root=None, helper=None):
        self.root = root or os.environ.get("EMBER_GAME_ROOT", DEFAULT_ROOT)
        self.helper = helper or str(Path(__file__).parent / "native/pipe-bridge.exe")
        try:
            self.installation = Installation.discover(Path(self.root))
        except ValueError:
            self.installation = None
        self.client = None
        self.challenge = None
        self.session = None
        self.reauth_required = False
        self.launcher = None
        self.game_processes = None
        self.phase = "signed_out"
        self.image = None
        self.detail = "Conecta tu cuenta para jugar."
        self.updates = None
        self.job = None
        self.download_cancel = threading.Event()
        self.prepared = None
        self.last_poll = 0
        self.lock = threading.RLock()
        self.settings_path = config_dir() / "settings.json"
        self.staging = cache_dir() / "updates"
        self.local = (
            installed_update_state(self.installation)
            if self.installation
            else {"distribution_version": None}
        )

        self.restore_prepared()

    def restore_prepared(self):
        pointer = self.settings_path.parent / "pending-update.json"
        try:
            value = json.loads(pointer.read_text())
            folder = Path(value["directory"])
            if value["root"] != str(self.installation.root) or folder.is_symlink():
                return
            if self.staging.resolve() not in folder.resolve().parents:
                return
            review = prepared_summary(folder)
            if review["installed_version"] == (self.local["distribution_version"] or 0):
                self.prepared = str(folder)
        except (OSError, ValueError, KeyError, AttributeError):
            pass

    def save_prepared(self):
        pointer = self.settings_path.parent / "pending-update.json"
        if self.prepared is None:
            pointer.unlink(missing_ok=True)
            return
        pointer.parent.mkdir(parents=True, exist_ok=True)
        temporary = pointer.with_suffix(".tmp")
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w") as stream:
            json.dump(
                {"root": str(self.installation.root), "directory": self.prepared},
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, pointer)

    def snapshot(self):
        try:
            game_installed = (
                self.installation is not None and self.installation.executable.is_file()
            )
        except ValueError:
            game_installed = False
        running = self.game_processes.refresh() if self.game_processes else False
        finished = self.game_processes.finished() if self.game_processes else False
        if self.game_processes and self.game_processes.seen and not finished:
            running = True
        if self.game_processes and finished and self.launcher:
            self.launcher.close()
            self.launcher = None
            self.game_processes = None
            self.phase = "ready" if self.session else "signed_out"
            self.detail = "El juego se cerró. Puedes iniciar otra partida."
        launch = self.launcher.state() if self.launcher else None
        starting = bool(
            launch
            and launch["phase"] in ("launch_preparing", "launch_waiting_sdk")
            and not running
        )

        review = None
        if self.prepared:
            try:
                review = prepared_summary(self.prepared)
            except (OSError, ValueError, KeyError):
                self.prepared = None
        return {
            "phase": launch["phase"]
            if launch and launch["phase"] != "idle"
            else self.phase,
            "detail": launch["detail"]
            if launch and launch["phase"] != "idle"
            else self.detail,
            "image": self.image,
            "connected": self.session is not None,
            "reauth_required": self.reauth_required,
            "version": self.local["distribution_version"],
            "updates": self.updates,
            "job": self.job,
            "prepared": self.prepared is not None,
            "resumable_download": pending_download(self.installation, self.staging),
            "prepared_summary": review,
            "recovery_needed": recovery_needed(self.installation),
            "root": self.root,
            "suggested_prefix": str(
                Path(DEFAULT_ROOT).parent
                if sys.platform == "win32"
                else Path(DEFAULT_ROOT).parents[1]
            ),
            "platform": sys.platform,
            "native_game": sys.platform == "win32",
            "runtime_prefix": str(self.installation.prefix) if self.installation else None,
            "game_directory": str(self.installation.game_dir)
            if self.installation
            else None,
            "runner": os.environ.get(
                "EMBER_WINE_RUNNER", "native" if sys.platform == "win32" else "wine"
            ),
            "game_installed": game_installed,
            "fresh_destination": bool(
                self.installation and not self.installation.game_dir.exists()
            ),
            "game_running": running,
            "game_starting": starting,
        }

    def client_ready(self):
        if self.installation is None:
            raise AuthError(
                "Selecciona la instalación de Chaos Zero Nightmare en Ajustes."
            )
        if not self.client:
            http, self.version_context = load_profile(
                self.installation, self.settings_path.parent
            )
            self.client = QRClient(http_context=http)

    def background(self, kind, action):
        if self.job and self.job["running"]:
            raise AuthError("Hay otra operación en curso.")
        self.job = {"kind": kind, "running": True, "error": None, "result": None}

        def work():
            try:
                result = action()
                with self.lock:
                    self.job.update(running=False, result=result)
            except DownloadCancelled:
                with self.lock:
                    self.job.update(running=False, result={"cancelled": True})
            except SessionExpired as error:
                with self.lock:
                    self.expire_session()
                    self.job.update(running=False, error=str(error))
            except AuthError as error:
                with self.lock:
                    self.job.update(running=False, error=str(error))
            except Exception:
                with self.lock:
                    self.job.update(
                        running=False,
                        error=(
                            "No se pudo completar la operación. Revisa el estado de recuperación antes de iniciar el juego."
                            if kind in ("apply_update", "recover_update")
                            else "No se pudo completar la operación."
                        ),
                    )

        threading.Thread(target=work, daemon=True).start()

    def expire_session(self):
        self.session = None
        self.reauth_required = True
        self.phase = "session_expired"
        self.detail = "Conecta de nuevo mediante QR para continuar."
        # The running game's IPC bridge and owned processes remain active.

    def rotate_session(self, session):
        with self.lock:
            self.session = session
            self.reauth_required = False

    def authenticated(self, operation):
        if self.session is None:
            raise SessionExpired()
        return authenticated_request(
            self.client, self.session, operation, self.rotate_session
        )

    def dispatch(self, action, payload=None):
        payload = payload or {}
        with self.lock:
            try:
                if action == "shutdown":
                    self.close()
                    return {"closed": True}
                if action == "state":
                    return self.snapshot()
                if action == "login":
                    if self.game_processes and self.game_processes.refresh():
                        raise AuthError("Cierra el juego antes de cambiar de sesión.")
                    if self.job and self.job["running"]:
                        raise AuthError("Espera a que termine la operación actual.")
                    self.client_ready()
                    self.session = None
                    self.challenge = self.client.begin()
                    self.phase = "qr_pending"
                    self.image = self.challenge.image
                    self.detail = (
                        "Escanea el QR con la app de STOVE y confirma el acceso."
                    )
                elif action == "poll":
                    if self.challenge and time.monotonic() - self.last_poll >= 3:
                        self.last_poll = time.monotonic()
                        status = self.client.poll(self.challenge)
                        if status == "success":
                            self.session = self.client.exchange(self.challenge)
                            self.challenge = None
                            self.image = None
                            self.reauth_required = False
                            self.phase = "ready"
                            self.detail = "Cuenta conectada. Todo listo para jugar."
                        elif status in ("expired", "close"):
                            self.phase = "qr_expired"
                            self.image = None
                            self.challenge = None
                            self.detail = (
                                "El código ha vencido. Genera otro para continuar."
                            )
                elif action == "logout":
                    if self.game_processes and self.game_processes.refresh():
                        raise AuthError(
                            "Cierra el juego antes de desconectar la cuenta."
                        )
                    if self.launcher and self.launcher.state()["phase"] not in (
                        "idle",
                        "launch_closed",
                        "launch_error",
                    ):
                        raise AuthError(
                            "Cierra el juego antes de desconectar la cuenta."
                        )
                    if self.job and self.job["running"]:
                        raise AuthError("Espera a que termine la operación actual.")
                    self.session = None
                    self.challenge = None
                    self.image = None
                    self.reauth_required = False
                    self.phase = "signed_out"
                    self.detail = "Sesión desconectada."
                elif action == "launch":
                    if not self.session:
                        raise AuthError("Conecta tu cuenta para jugar.")
                    if self.job and self.job["running"]:
                        raise AuthError("Espera a que termine la operación actual.")
                    require_idle(self.installation)
                    if self.launcher is not None:
                        if self.launcher.state()["phase"] not in (
                            "launch_closed",
                            "launch_error",
                        ):
                            raise AuthError("El juego ya está en ejecución.")
                        self.launcher.close()
                        self.launcher = None
                    require_runtime(self.installation, os.environ.get("EMBER_WINE_RUNNER", "wine"), self.helper)
                    self.client_ready()
                    session = self.authenticated(
                        lambda current: request_game_session(self.client, current)
                    )
                    self.launcher = GameLauncher(
                        self.installation,
                        os.environ.get("EMBER_WINE_RUNNER", "wine"),
                        helper=self.helper,
                        region=self.client.region,
                    )
                    self.game_processes = GameProcesses(self.installation)
                    self.launcher.start(session)
                elif action in ("stop_game", "force_stop_game"):
                    if not self.launcher or not self.game_processes:
                        raise AuthError("No hay una partida iniciada por Ember.")
                    launcher, processes = self.launcher, self.game_processes

                    def stop_game():
                        if not processes.refresh():
                            launcher.close()  # Cancel startup before the loader spawns.
                        result = processes.stop(force=action == "force_stop_game")
                        if result["closed"]:
                            launcher.close()
                            with self.lock:
                                self.launcher = None
                                self.game_processes = None
                                self.phase = "ready" if self.session else "signed_out"
                                self.detail = (
                                    "El juego se cerró. Puedes iniciar otra partida."
                                )
                        return result

                    self.background(action, stop_game)
                elif action == "renew_session":
                    if self.session is None:
                        raise SessionExpired()
                    previous = self.session

                    def renew():
                        try:
                            renewed = refresh_launcher_session(self.client, previous)
                        except AuthError as error:
                            if isinstance(error, SessionExpired) or getattr(
                                error, "http_status", None
                            ) in (401, 403):
                                raise SessionExpired() from None
                            raise
                        self.rotate_session(renewed)
                        return {"session_renewed": True}

                    self.background(action, renew)
                elif action == "cancel_download":
                    if (
                        not self.job
                        or not self.job["running"]
                        or self.job["kind"] != "prepare"
                    ):
                        raise AuthError("No hay ninguna descarga en curso.")
                    self.download_cancel.set()
                    self.job["cancelling"] = True
                elif action in ("check_updates", "prepare"):
                    if not self.session:
                        raise AuthError(
                            "Conecta tu cuenta para consultar actualizaciones."
                        )
                    self.download_cancel.clear()
                    if action == "prepare":
                        self.prepared = None
                        self.save_prepared()
                    samples = {
                        "key": None,
                        "bytes": 0,
                        "time": time.monotonic(),
                        "speed": 0,
                    }

                    def download_progress(value):
                        now = time.monotonic()
                        key = (value["phase"], value.get("package_index"))
                        received = value["downloaded_bytes"]
                        if key != samples["key"] or received < samples["bytes"]:
                            samples.update(key=key, bytes=received, time=now)
                        elif now - samples["time"] >= 0.25:
                            samples["speed"] = (received - samples["bytes"]) / (
                                now - samples["time"]
                            )
                            samples.update(bytes=received, time=now)
                        speed = samples["speed"] if value["phase"] == "download" else 0
                        remaining = max(0, value["total_bytes"] - received)
                        with self.lock:
                            self.job["progress"] = {
                                **value,
                                "bytes_per_second": speed,
                                "eta_seconds": remaining / speed if speed > 0 else None,
                            }

                    def update():
                        context = self.version_context
                        published = self.authenticated(
                            lambda current: request_published_version(
                                self.client,
                                current,
                                self.local["distribution_version"] or 0,
                                context,
                            )
                        )
                        self.updates = published.summary()
                        if action == "prepare":
                            result = prepare_resumable(
                                self.installation,
                                published,
                                self.staging,
                                self.download_cancel,
                                download_progress,
                            )
                            self.prepared = result["prepared_directory"]
                            self.save_prepared()
                            return result
                        return self.updates

                    self.background(action, update)
                elif action in ("apply_update", "recover_update"):
                    if self.game_processes and self.game_processes.refresh():
                        raise AuthError(
                            "Cierra el juego antes de actualizar o recuperar archivos."
                        )
                    if action == "apply_update" and not self.prepared:
                        raise AuthError(
                            "Primero prepara los archivos de la actualización."
                        )

                    def modify():
                        if action == "apply_update":
                            result = (
                                install_prepared(
                                    self.installation,
                                    self.prepared,
                                )
                                if json.loads(
                                    (Path(self.prepared) / "prepared.json").read_text()
                                ).get("fresh_install", False)
                                else apply_update(self.installation, self.prepared)
                            )
                        else:
                            result = recover_update(self.installation)
                        with self.lock:
                            self.local = installed_update_state(self.installation)
                            self.prepared = None
                            self.save_prepared()
                            if self.updates:
                                self.updates["distribution_version"] = self.local[
                                    "distribution_version"
                                ]
                                self.updates["update_available"] = (
                                    self.updates["published_version"]
                                    > self.local["distribution_version"]
                                )
                        return result

                    self.background(action, modify)
                elif action == "verify":

                    def verify():
                        version = self.local["distribution_version"]
                        plan = repair_plan(
                            self.installation.game_dir, fetch_manifest(version), version
                        )
                        return {
                            "matching": sum(
                                x["kind"] == "F" and x["status"] == "matching"
                                for x in plan["entries"]
                            ),
                            "files": sum(x["kind"] == "F" for x in plan["entries"]),
                            "download_bytes": plan["download_bytes"],
                            "blocked": plan["blocked"],
                        }

                    self.background(action, verify)
                elif action == "uninstall":
                    if payload.get("confirmed") is not True:
                        raise AuthError(
                            "Confirma la ruta del juego antes de desinstalar."
                        )
                    if self.session:
                        raise AuthError("Desconecta la cuenta antes de desinstalar.")
                    if self.job and self.job["running"]:
                        raise AuthError("Espera a que termine la operación actual.")
                    if self.game_processes and self.game_processes.refresh():
                        raise AuthError("Cierra el juego antes de desinstalar.")
                    if self.launcher:
                        if self.launcher.state()["phase"] not in (
                            "idle",
                            "launch_closed",
                            "launch_error",
                        ):
                            raise AuthError("Cierra el juego antes de desinstalar.")
                        self.launcher.close()
                        self.launcher = None
                    if self.prepared or recovery_needed(self.installation):
                        raise AuthError(
                            "Completa o recupera la actualización pendiente antes de desinstalar."
                        )
                    selected = self.installation
                    confirmed_path = payload.get("path")
                    self.challenge = None
                    self.image = None

                    def remove():
                        result = uninstall(selected, confirmed_path)
                        with self.lock:
                            self.local = installed_update_state(selected)
                            self.updates = None
                            self.prepared = None
                            self.save_prepared()
                            self.phase = "signed_out"
                            self.detail = "Juego enviado a la papelera. Puedes volver a instalarlo."
                        return result

                    self.background("uninstall", remove)
                elif action == "prepare_dependencies":
                    if self.installation is None:
                        raise AuthError("Primero prepara o selecciona una instalación en Ajustes.")
                    if self.game_processes and self.game_processes.refresh():
                        raise AuthError("Cierra el juego antes de preparar las dependencias.")
                    if self.launcher and self.launcher.state()["phase"] not in ("idle", "launch_closed", "launch_error"):
                        raise AuthError("Cierra el juego antes de preparar las dependencias.")
                    installation = self.installation
                    runner = os.environ.get("EMBER_WINE_RUNNER", "wine")
                    self.background("dependencies", lambda: prepare_runtime(installation, runner, self.helper, cache_dir(), self.dependency_progress))
                elif action == "bootstrap":
                    prefix = payload.get("prefix")
                    runner = payload.get("runner")
                    if (
                        not isinstance(prefix, str)
                        or not prefix
                        or not isinstance(runner, str)
                        or not runner
                    ):
                        raise AuthError(
                            "Indica la carpeta del prefijo nuevo y el ejecutable Wine."
                        )
                    if (
                        self.session
                        or (self.launcher and self.launcher.state()["phase"] not in ("idle", "launch_closed", "launch_error"))
                        or (self.job and self.job["running"])
                    ):
                        raise AuthError(
                            "Cierra el juego y desconecta la cuenta antes de crear otro prefijo."
                        )
                    self.challenge = None
                    self.image = None

                    def initialize():
                        installation = create_prefix(prefix, runner)
                        prepare_runtime(installation, runner, self.helper, cache_dir(), self.dependency_progress)
                        bootstrap_profile(installation, self.settings_path.parent)
                        with self.lock:
                            self.select_installation(installation, runner)
                            self.detail = "Biblioteca preparada. Conecta por QR y descarga el juego."
                        return {"prefix_created": True}

                    self.background("bootstrap", initialize)
                elif action in ("settings", "new_installation"):
                    root = payload.get("root")
                    runner = payload.get("runner")
                    if (
                        not isinstance(root, str)
                        or not isinstance(runner, str)
                        or not root
                        or not runner
                    ):
                        raise AuthError(
                            "Indica la carpeta Smilegate y el ejecutable Wine."
                        )
                    if (
                        self.session
                        or (self.job and self.job["running"])
                        or (self.launcher and self.launcher.state()["phase"] not in ("idle", "launch_closed", "launch_error"))
                    ):
                        raise AuthError(
                            "Desconecta la cuenta antes de cambiar la instalación."
                        )
                    if sys.platform != "win32" and not shutil.which(runner):
                        raise AuthError("No se encontró el ejecutable Wine indicado.")
                    if action == "new_installation":
                        installation = validate_destination(root)
                        if self.installation is not None:
                            try:
                                transfer_profile(
                                    self.installation,
                                    installation,
                                    self.settings_path.parent,
                                )
                            except FileNotFoundError:
                                bootstrap_profile(
                                    installation, self.settings_path.parent
                                )
                        else:
                            bootstrap_profile(installation, self.settings_path.parent)
                    else:
                        installation = Installation.discover(Path(root))
                        installation.executable
                    local = installed_update_state(installation)
                    self.settings_path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = self.settings_path.with_suffix(".tmp")
                    temporary.write_text(
                        json.dumps({"root": str(installation.root), "runner": runner})
                    )
                    temporary.chmod(0o600)
                    os.replace(temporary, self.settings_path)
                    self.root = str(installation.root)
                    self.installation = installation
                    self.local = local
                    self.prepared = None
                    self.save_prepared()
                    self.updates = None
                    self.client = None
                    self.reauth_required = False
                    self.job = None
                    self.phase = "signed_out"
                    self.detail = (
                        "Instalación seleccionada. Conecta tu cuenta para jugar."
                        if local["distribution_version"] is not None
                        else "Destino seleccionado. Conecta tu cuenta para instalar el juego."
                    )
                    os.environ["EMBER_WINE_RUNNER"] = runner
                else:
                    raise AuthError("Operación no disponible.")
                return self.snapshot()
            except SessionExpired as error:
                self.expire_session()
                return {**self.snapshot(), "error": str(error)}
            except AuthError as error:
                return {**self.snapshot(), "error": str(error)}
            except Exception:
                return {
                    **self.snapshot(),
                    "error": "No se pudo completar la operación. Comprueba la instalación y el entorno de ejecución.",
                }

    def close(self):
        self.download_cancel.set()
        if self.launcher:
            self.launcher.close()
        self.session = None
        self.challenge = None

    def dependency_progress(self, value):
        with self.lock:
            if value["phase"] == "runtime_download":
                downloaded = value["downloaded_bytes"] / 1024**2
                total = value["total_bytes"] / 1024**2
                self.job["detail"] = f"Descargando WebView2 de Microsoft: {downloaded:.0f} / {total:.0f} MiB." if total else f"Descargando WebView2 de Microsoft: {downloaded:.0f} MiB."
            else:
                self.job["detail"] = "Instalando WebView2 en el entorno seleccionado. Puede tardar unos minutos…"

    def select_installation(self, installation, runner):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"root": str(installation.root), "runner": runner})
        )
        temporary.chmod(0o600)
        os.replace(temporary, self.settings_path)
        self.root = str(installation.root)
        self.installation = installation
        self.local = installed_update_state(installation)
        self.prepared = None
        self.save_prepared()
        self.updates = None
        self.client = None
        self.reauth_required = False
        self.phase = "signed_out"
        os.environ["EMBER_WINE_RUNNER"] = runner
