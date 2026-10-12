import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/oracles"))
from session_access import (
    authenticated_request,
    refresh_launcher_session,
    SessionExpired,
)
from stove_linux.auth import (
    AuthError,
    LauncherSession,
    QRClient,
    Region,
    LAUNCHER_CLIENT_ID,
)


class SessionTests(unittest.TestCase):
    def session(self):
        return LauncherSession("PRIVATE_ACCESS", "PRIVATE_REFRESH", member_no="123")

    def client(self):
        client = Mock()
        client.region = Region()
        client._request_url.return_value = {
            "access_token": "NEW_PRIVATE_ACCESS",
            "refresh_token": "NEW_PRIVATE_REFRESH",
        }
        return client

    def test_native_refresh_payload_and_rotated_tokens_remain_private(self):
        client = self.client()
        renewed = refresh_launcher_session(client, self.session())
        args, kwargs = client._request_url.call_args
        self.assertEqual(
            args[:2], ("POST", "https://s-api.onstove.com/sign/v2.0/pc/refresh")
        )
        self.assertEqual(
            args[2],
            {
                "client_id": LAUNCHER_CLIENT_ID,
                "service_id": "Launcher",
                "refresh_token": "PRIVATE_REFRESH",
                "gds_info": client.region.payload(),
            },
        )
        self.assertTrue(kwargs["native_context"])
        self.assertEqual(renewed.access_token, "NEW_PRIVATE_ACCESS")
        self.assertEqual(renewed.member_no, "123")
        self.assertNotIn("PRIVATE", repr(renewed))

    def test_success_never_refreshes(self):
        client = self.client()
        rotated = Mock()
        result = authenticated_request(client, self.session(), lambda s: "ok", rotated)
        self.assertEqual(result, "ok")
        client._request_url.assert_not_called()
        rotated.assert_not_called()

    def test_401_rotates_then_retries_once_with_new_token(self):
        client = self.client()
        tokens = []

        def operation(session):
            tokens.append(session.access_token)
            if len(tokens) == 1:
                raise AuthError("Unauthorized", http_status=401)
            return "ok"

        rotated = Mock()
        self.assertEqual(
            authenticated_request(client, self.session(), operation, rotated), "ok"
        )
        self.assertEqual(tokens, ["PRIVATE_ACCESS", "NEW_PRIVATE_ACCESS"])
        rotated.assert_called_once()
        client._request_url.assert_called_once()

    def test_second_401_requires_qr_without_refresh_loop(self):
        client = self.client()
        operation = Mock(side_effect=AuthError("Unauthorized", http_status=401))
        with self.assertRaises(SessionExpired):
            authenticated_request(client, self.session(), operation, Mock())
        self.assertEqual(operation.call_count, 2)
        client._request_url.assert_called_once()

    def test_rejected_refresh_requires_qr(self):
        client = self.client()
        client._request_url.side_effect = AuthError("Unauthorized", http_status=401)
        operation = Mock(side_effect=AuthError("Unauthorized", http_status=401))
        with self.assertRaises(SessionExpired):
            authenticated_request(client, self.session(), operation, Mock())
        operation.assert_called_once()

    def test_policy_and_network_errors_never_refresh_or_guess_expiration(self):
        for error in (
            AuthError("Policy", code=49500),
            AuthError("Rejected", code=70713),
            AuthError("Forbidden", http_status=403),
            AuthError("Network"),
        ):
            client = self.client()
            with self.assertRaises(AuthError) as result:
                authenticated_request(
                    client, self.session(), Mock(side_effect=error), Mock()
                )
            self.assertIs(result.exception, error)
            client._request_url.assert_not_called()

    def test_rotated_account_must_not_change_identity(self):
        client = self.client()
        client._request_url.return_value["member"] = {"member_no": "999"}
        with self.assertRaises(SessionExpired):
            refresh_launcher_session(client, self.session())

    def test_http_401_has_structured_status_without_response_body(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(
            "https://example.invalid/private",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"PRIVATE_BODY"),
        )
        client = QRClient(opener=opener)
        with self.assertRaises(AuthError) as result:
            client._request_url("GET", "https://example.invalid/private")
        self.assertEqual(result.exception.http_status, 401)
        self.assertNotIn("PRIVATE", str(result.exception))

    def test_many_rotations_use_latest_refresh_and_retry_with_new_access(self):
        current = [self.session()]
        client = self.client()
        for index in range(100):
            old = current[0]
            new_access = f'SYNTHETIC_ACCESS_{index}'
            new_refresh = f'SYNTHETIC_REFRESH_{index}'
            def refresh(method, url, payload, *, native_context):
                self.assertEqual(payload['refresh_token'], old.refresh_token)
                return {'access_token':new_access,'refresh_token':new_refresh}
            client._request_url.side_effect = refresh
            attempts = []
            def operation(session):
                attempts.append(session.access_token)
                if len(attempts) == 1:
                    raise AuthError('Unauthorized', http_status=401)
                self.assertEqual(session.access_token,new_access)
                return 'ok'
            def rotated(session):
                current[0] = session
            self.assertEqual(authenticated_request(client,old,operation,rotated),'ok')
            self.assertEqual(attempts,[old.access_token,new_access])
            self.assertEqual(current[0].refresh_token,new_refresh)
            self.assertEqual(current[0].member_no,old.member_no)
        self.assertEqual(client._request_url.call_count,100)

    def test_transient_refresh_failure_preserves_session_for_later_retry(self):
        for error in (AuthError('Network'), AuthError('Unavailable',http_status=503)):
            client = self.client()
            original = self.session()
            rotated = Mock()
            client._request_url.side_effect = error
            with self.assertRaises(AuthError) as raised:
                authenticated_request(client,original,Mock(side_effect=AuthError('Unauthorized',http_status=401)),rotated)
            self.assertIs(raised.exception,error)
            self.assertNotIsInstance(raised.exception,SessionExpired)
            rotated.assert_not_called()
            client._request_url.side_effect = None
            self.assertEqual(authenticated_request(client,original,lambda _: 'ok',rotated),'ok')


class NativeSessionPolicyTests(unittest.TestCase):
    def test_requires_rust_policy_abi(self):
        from stove_linux import native_session
        self.assertIsNotNone(native_session.library)
        self.assertEqual(native_session.library.ember_session_abi(), 1)

    def test_callbacks_and_errors_match_python_oracle(self):
        import python_session_access as oracle
        cases = [([], None), ([401], None), ([401,401], None), ([401,403], None),
                 ([403], None), ([503], None), ([401],401), ([401],403), ([401],503),
                 ([401],0)]
        for statuses, refresh_error in cases:
            outcomes = []
            for module in [sys.modules['session_access'], oracle]:
                client = SessionTests().client()
                if refresh_error is not None:
                    client._request_url.side_effect = AuthError('Synthetic failure', http_status=refresh_error or None)
                calls=[]
                def operation(session):
                    calls.append(session.access_token)
                    if len(calls) <= len(statuses):
                        raise AuthError('Synthetic operation error',http_status=statuses[len(calls)-1])
                    return 'ok'
                rotated=Mock()
                try:
                    outcome=('result',module.authenticated_request(client,SessionTests().session(),operation,rotated))
                except AuthError as error:
                    outcome=('error',type(error).__name__,error.http_status)
                outcomes.append((outcome,calls,client._request_url.call_count,rotated.call_count))
            self.assertEqual(outcomes[0],outcomes[1])

    def test_service_code_401_does_not_trigger_http_renewal(self):
        client=SessionTests().client()
        error=AuthError('Synthetic policy',code=401)
        with self.assertRaises(AuthError) as raised:
            authenticated_request(client,SessionTests().session(),Mock(side_effect=error),Mock())
        self.assertIs(raised.exception,error)
        client._request_url.assert_not_called()

    def test_identity_continuity_matches_previous_rules(self):
        from stove_linux import native_session
        for previous,new in [('', ''),('123',''),('','123'),('123','123'),('日本語','日本語')]:
            self.assertEqual(native_session.renewed_identity(previous,new),new or previous)
        with self.assertRaises(ValueError) as raised:
            native_session.renewed_identity('PRIVATE_ACCOUNT','OTHER_PRIVATE_ACCOUNT')
        self.assertNotIn('PRIVATE_ACCOUNT',str(raised.exception))
