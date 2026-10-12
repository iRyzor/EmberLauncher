"""Launcher token rotation stays in memory; only HTTP 401 triggers renewal."""

from stove_linux import native_session
from dataclasses import replace
from stove_linux.auth import AuthError, LAUNCHER_CLIENT_ID, LauncherSession


class SessionExpired(AuthError):
    def __init__(self):
        super().__init__(
            "La sesión necesita un nuevo acceso por QR. Si hay una partida abierta, ciérrala antes de conectar de nuevo."
        )


def refresh_launcher_session(client, session):
    if not isinstance(session, LauncherSession):
        raise SessionExpired()
    value = client._request_url(
        "POST",
        "https://s-api.onstove.com/sign/v2.0/pc/refresh",
        {
            "client_id": LAUNCHER_CLIENT_ID,
            "service_id": "Launcher",
            "refresh_token": session.refresh_token,
            "gds_info": client.region.payload(),
        },
        native_context=True,
    )
    renewed = LauncherSession.from_value(value)
    try:
        member = native_session.renewed_identity(session.member_no, renewed.member_no)
    except ValueError:
        raise SessionExpired() from None
    return replace(renewed, member_no=member)



def authenticated_request(client, session, operation, rotated):
    try:
        return operation(session)
    except AuthError as error:
        if native_session.decision(0, getattr(error, "http_status", None)) != 1:
            raise
    # Retry once, only after a successful rotation. No retry on service policy codes.
    try:
        renewed = refresh_launcher_session(client, session)
    except AuthError as error:
        if native_session.decision(1, getattr(error, "http_status", None), isinstance(error, SessionExpired)) == 2:
            raise SessionExpired() from None
        raise
    rotated(renewed)
    try:
        return operation(renewed)
    except AuthError as error:
        if native_session.decision(2, getattr(error, "http_status", None)) == 2:
            raise SessionExpired() from None
        raise
