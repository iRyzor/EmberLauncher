"""Launcher token rotation stays in memory; only HTTP 401 triggers renewal."""

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
    if (
        session.member_no
        and renewed.member_no
        and session.member_no != renewed.member_no
    ):
        raise SessionExpired()
    return replace(renewed, member_no=renewed.member_no or session.member_no)


def authenticated_request(client, session, operation, rotated):
    try:
        return operation(session)
    except AuthError as error:
        if getattr(error, "http_status", None) != 401:
            raise
    # Retry once, only after a successful rotation. No retry on service policy codes.
    try:
        renewed = refresh_launcher_session(client, session)
    except AuthError as error:
        if isinstance(error, SessionExpired) or getattr(error, "http_status", None) in (
            401,
            403,
        ):
            raise SessionExpired() from None
        raise
    rotated(renewed)
    try:
        return operation(renewed)
    except AuthError as error:
        if getattr(error, "http_status", None) == 401:
            raise SessionExpired() from None
        raise
