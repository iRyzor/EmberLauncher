"""QR login using the installed launcher's public client identifier.

Source: official accounts frontend and IdentityLib QR request factories.
No Google credentials, browser cookies or existing account sessions are read.
"""
from . import native_auth
from dataclasses import dataclass, field
import base64
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

# PINE_LAUNCHER_APP_KEY -> StoveAPIClient +0xe0 -> SigninRequest.client_id.
# Reproduce from the pinned binaries with tools/inspect_identity.py.
LAUNCHER_CLIENT_ID = "5faa0926311687ccc34a598d9640a48909a86a0e93afdf586ab088a3d01a93d3"


class AuthError(Exception):
    """Fixed diagnostic text, safe to show without response bodies or URLs."""

    def __init__(self, message, code=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class Region:
    nation: str = "DO"
    regulation: str = "ETC"
    timezone: str = "America/Santo_Domingo"
    utc_offset: int = -240
    lang: str = "en"

    def payload(self):
        return dict(is_default=False, nation=self.nation, regulation=self.regulation,
                    timezone=self.timezone, utc_offset=self.utc_offset, lang=self.lang)


@dataclass(frozen=True)
class QRChallenge:
    session: str = field(repr=False)
    image: str = field(repr=False)
    expires_at: float

    @classmethod
    def from_value(cls, value):
        if native_auth.library is not None:
            return cls(**native_auth.validate_qr(value))
        session = value.get("session")
        image = value.get("image")
        expires = value.get("expires_in")
        if not isinstance(session, str) or not 1 <= len(session) <= 4096:
            raise AuthError("El servidor no devolvió una sesión QR válida.")
        if not isinstance(image, str) or len(image) > 262144:
            raise AuthError("El servidor no devolvió una imagen QR válida.")
        try:
            png = base64.b64decode(image, validate=True)
        except (ValueError, TypeError):
            raise AuthError("La imagen QR recibida no tiene el formato esperado.") from None
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise AuthError("La imagen QR recibida no es PNG.")
        # Observed contract: despite its name, expires_in is an absolute Unix
        # timestamp in milliseconds, not a TTL in seconds.
        if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not 1e12 < expires < 1e14:
            raise AuthError("El vencimiento del QR no tiene el formato esperado.")
        return cls(session, image, expires / 1000)


@dataclass(frozen=True)
class LauncherSession:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    # Keep the native expiration fields without guessing units before validation.
    expires_in: int | None = field(default=None, repr=False)
    expire_in: int | None = field(default=None, repr=False)
    transaction_id: str = field(default="", repr=False)
    member_no: str = field(default="", repr=False)

    @classmethod
    def from_value(cls, value):
        if native_auth.library is not None:
            return cls(**native_auth.validate_session(value))
        tokens = [value.get(name) for name in ("access_token", "refresh_token")]
        if any(not isinstance(token, str) or not 1 <= len(token) <= 32768 for token in tokens):
            raise AuthError("STOVE no devolvió los tokens de launcher esperados.")
        expirations = [value.get(name) for name in ("expires_in", "expire_in")]
        if any(v is not None and (type(v) is not int or v <= 0) for v in expirations):
            raise AuthError("STOVE devolvió un vencimiento de sesión inesperado.")
        transaction = value.get("transaction_id", "")
        member = value.get("member", {})
        if not isinstance(transaction, str) or len(transaction) > 4096 or not isinstance(member, dict):
            raise AuthError("STOVE devolvió metadatos de sesión inesperados.")
        number = member.get("member_no", "")
        if isinstance(number, bool) or not isinstance(number, (str, int)):
            raise AuthError("STOVE devolvió metadatos de cuenta inesperados.")
        return cls(*tokens, *expirations, transaction, str(number))


class QRClient:
    def __init__(self, region=None, opener=None, http_context=None):
        self.region = region or Region()
        self._native_transport = opener is None and native_auth.library is not None
        self.opener = opener or build_opener(NoRedirect())
        self._approved = None
        self._gate = native_auth.QRGate() if native_auth.library is not None else None
        self._gate_bound = False
        self.http_context = http_context

    def _request(self, method, path, payload=None):
        return self._request_url(method, "https://api.onstove.com/auth-secure/v1.0/" + path, payload)

    def _request_url(self, method, url, payload=None, native_context=False, extra_headers=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "StoveLauncher/3.0", "Accept-Language": self.region.lang}
        if native_context:
            if self.http_context is None:
                raise AuthError("Falta el contexto HTTP de la instalación de STOVE.")
            try:
                headers.update(self.http_context.headers(self.region))
            except ValueError:
                raise AuthError("El contexto HTTP de STOVE no tiene un formato válido.") from None
        if extra_headers:
            # Header values can contain tokens; diagnostics must omit the value.
            if any(not isinstance(value, str) or len(value) > 32775
                   or any(ord(char) < 32 or ord(char) > 126 for char in value)
                   for value in extra_headers.values()):
                raise AuthError("STOVE devolvió metadatos no aptos para una petición HTTP.")
            headers.update(extra_headers)
        if self._native_transport:
            return native_auth.request(method, url, headers, payload)
        request = Request(url,
                          data=None if payload is None else json.dumps(payload).encode(),
                          method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=10) as response:
                raw = response.read(1048577)
                if len(raw) > 1048576:
                    raise AuthError("La respuesta de STOVE excedió el límite esperado.")
                result = json.loads(raw)
        except HTTPError as error:
            status = error.code
            error.close()
            raise AuthError(f"STOVE respondió HTTP {status}.", http_status=status) from None
        except (URLError, TimeoutError, OSError):
            raise AuthError("No se pudo contactar al servicio de autenticación de STOVE.") from None
        except (ValueError, UnicodeDecodeError):
            raise AuthError("STOVE devolvió una respuesta de autenticación ilegible.") from None
        if not isinstance(result, dict):
            raise AuthError("STOVE devolvió un envelope de autenticación inválido.")
        if type(result.get("code")) is not int or result["code"] != 0:
            code = result.get("code")
            if code == 49500:
                raise AuthError("STOVE restringió esta petición de autenticación (49500). "
                                "El prototipo no volverá a intentarla automáticamente.", code=49500)
            suffix = f" (código {code})" if type(code) is int else ""
            raise AuthError("STOVE rechazó la autenticación" + suffix + ".", code=code if type(code) is int else None)
        value = result.get("value")
        if not isinstance(value, dict):
            raise AuthError("STOVE no devolvió datos de autenticación.")
        return value

    def begin(self):
        self._approved = None
        if self._gate is not None:
            self._gate.close()
            self._gate = native_auth.QRGate()
            self._gate_bound = False
        challenge = QRChallenge.from_value(self._request("POST", "qr", {
            "login_type": "QR_LOGIN_LAUNCHER", "width": 180, "height": 180,
            "gds_info": self.region.payload()}))
        if self._gate is not None:
            self._gate.reset(challenge)
            self._gate_bound = True
        return challenge

    def poll(self, challenge):
        if self._gate is not None:
            if not self._gate_bound:
                self._gate.reset(challenge)
                self._gate_bound = True
            if self._gate.step(0, challenge, time.time()) == 4:
                return "expired"
        elif time.time() >= challenge.expires_at:
            return "expired"
        value = self._request("GET", "qr/status?" + urlencode({"session": challenge.session}))
        if self._gate is not None:
            return {0: "init", 1: "ongoing", 2: "success", 3: "close", 4: "expired"}[
                self._gate.step(1, challenge, time.time(), value)]
        status = value.get("status")
        if status not in {"init", "ongoing", "success", "close"}:
            raise AuthError("STOVE devolvió un estado QR desconocido.")
        # Do not silently substitute another session into the approved challenge.
        if value.get("session") != challenge.session:
            raise AuthError("La respuesta QR no coincide con la sesión iniciada.")
        if status == "success":
            self._approved = challenge.session
        return status

    def exchange(self, challenge):
        if self._gate is not None:
            self._gate.step(2, challenge, time.time())
        elif self._approved != challenge.session:
            raise AuthError("Primero debe aprobarse este QR en la app de STOVE.")
        # Consume the observed approval once; do not replay failed sign-in calls.
        self._approved = None
        # SigninModel's successful branch parses tokens directly. Authorization
        # codes belong to other login/verification branches, not normal QR login.
        value = self._request_url("POST", "https://s-api.onstove.com/sign/v2.1/pc/signin", {
            "client_id": LAUNCHER_CLIENT_ID, "service_id": "Launcher", "provider_cd": "QR",
            "provider_data": {"qr_login_session": challenge.session},
            "gds_info": self.region.payload()}, native_context=True)
        return LauncherSession.from_value(value)
