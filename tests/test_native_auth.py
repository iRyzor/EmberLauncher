import base64
import io
import json
import time
import unittest
from urllib.error import HTTPError, URLError

from stove_linux.auth import AuthError, LAUNCHER_CLIENT_ID, LauncherSession, NoRedirect, QRChallenge, QRClient
from stove_linux.http_context import NativeHTTPContext


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return Response(json.dumps(value).encode())


def qr_value():
    return {"session": "synthetic-session", "image": base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic").decode(),
            "expires_in": int((time.time() + 30) * 1000)}


class AuthTests(unittest.TestCase):
    def approved_client(self, final):
        value = qr_value()
        opener = Opener([{"code": 0, "value": value},
                         {"code": 0, "value": {"status": "success", "session": value["session"]}}, final])
        client = QRClient(opener=opener, http_context=NativeHTTPContext("a" * 40))
        challenge = client.begin()
        client.poll(challenge)
        return client, challenge, opener

    def test_approved_qr_signin_returns_direct_tokens_and_never_replays(self):
        client, challenge, opener = self.approved_client({"code": 0, "value": {
            "access_token": "PRIVATE-ACCESS", "refresh_token": "PRIVATE-REFRESH",
            "expires_in": 3600, "member": {"member_no": 123}}})
        session = client.exchange(challenge)
        self.assertEqual(session.access_token, "PRIVATE-ACCESS")
        self.assertNotIn("PRIVATE", repr(session))
        request = opener.calls[-1]
        self.assertEqual(request.full_url, "https://s-api.onstove.com/sign/v2.1/pc/signin")
        payload = json.loads(request.data)
        self.assertEqual(payload["client_id"], LAUNCHER_CLIENT_ID)
        self.assertEqual(payload["service_id"], "Launcher")
        self.assertEqual(payload["provider_cd"], "QR")
        self.assertEqual(payload["provider_data"], {"qr_login_session": challenge.session})
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(headers["caller-id"], "STOVE_LAUNCHER_VER.3.2.29.736")
        self.assertEqual(headers["caller-detail"], "a" * 40)
        self.assertEqual(headers["user-agent"], "STOVEAPIClient/3.0")
        self.assertEqual(headers["accept-language"], "en-DO")
        self.assertEqual(headers["x-utc-offset"], "-240")
        self.assertEqual(headers["x-timezone"], payload["gds_info"]["timezone"])
        self.assertEqual(headers["x-nation"], payload["gds_info"]["nation"])
        self.assertEqual(headers["x-lang"], payload["gds_info"]["lang"])
        self.assertEqual(headers["transaction-id"], client.http_context.transaction_id)
        self.assertNotIn("Captcha-token", dict(request.header_items()))
        # Native SigninRequest's GDS builder has six fields. Portal web's empty
        # ip property must not be carried into this Launcher client contract.
        self.assertEqual(set(payload["gds_info"]),
                         {"is_default", "nation", "regulation", "timezone", "utc_offset", "lang"})
        with self.assertRaises(AuthError):
            client.exchange(challenge)
        self.assertEqual(len(opener.calls), 3)

    def test_unapproved_qr_cannot_request_tokens(self):
        opener = Opener([])
        with self.assertRaises(AuthError):
            QRClient(opener=opener).exchange(QRChallenge.from_value(qr_value()))
        self.assertEqual(opener.calls, [])

    def test_missing_http_context_never_posts_an_incomplete_signin(self):
        client, challenge, opener = self.approved_client({"code": 0, "value": {}})
        client.http_context = None
        with self.assertRaises(AuthError):
            client.exchange(challenge)
        self.assertEqual(len(opener.calls), 2)
        with self.assertRaises(AuthError):
            client.exchange(challenge)
        self.assertEqual(len(opener.calls), 2)

    def test_failed_exchange_consumes_approval_and_omits_remote_details(self):
        client, challenge, opener = self.approved_client({"code": 49500, "message": "PRIVATE"})
        with self.assertRaises(AuthError) as caught:
            client.exchange(challenge)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(caught.exception.code, 49500)
        with self.assertRaises(AuthError):
            client.exchange(challenge)
        self.assertEqual(len(opener.calls), 3)

    def test_incomplete_tokens_do_not_create_session(self):
        for data in [{"access_token": "PRIVATE"},
                     {"access_token": "PRIVATE", "refresh_token": "PRIVATE", "expires_in": True}]:
            with self.assertRaises(AuthError):
                LauncherSession.from_value(data)

    def test_verified_create_and_poll_contract(self):
        value = qr_value()
        opener = Opener([{"code": 0, "value": value},
                         {"code": 0, "value": {"status": "success", "session": value["session"]}}])
        client = QRClient(opener=opener)
        challenge = client.begin()
        self.assertEqual(client.poll(challenge), "success")
        create, poll = opener.calls
        self.assertEqual(create.get_method(), "POST")
        self.assertEqual(json.loads(create.data)["login_type"], "QR_LOGIN_LAUNCHER")
        self.assertEqual(poll.get_method(), "GET")
        self.assertTrue(poll.full_url.endswith("qr/status?session=synthetic-session"))
        self.assertAlmostEqual(challenge.expires_at, value["expires_in"] / 1000)
        self.assertNotIn("synthetic-session", repr(challenge))

    def test_expired_challenge_does_not_call_server(self):
        opener = Opener([])
        client = QRClient(opener=opener)
        challenge = QRChallenge("SECRET", "", time.time() - 1)
        self.assertEqual(client.poll(challenge), "expired")
        self.assertEqual(opener.calls, [])

    def test_response_does_not_substitute_session(self):
        opener = Opener([{"code": 0, "value": {"status": "success", "session": "another"}}])
        with self.assertRaises(AuthError):
            QRClient(opener=opener).poll(QRChallenge.from_value(qr_value()))

    def test_invalid_image_and_relative_expiry_are_rejected(self):
        for key, value in [("image", "<script>SECRET</script>"), ("expires_in", 20)]:
            data = qr_value()
            data[key] = value
            with self.assertRaises(AuthError):
                QRChallenge.from_value(data)

    def test_http_error_does_not_echo_secret_url_or_body(self):
        error = HTTPError("https://example.invalid/?token=SECRET", 403, "SECRET", {}, io.BytesIO(b"SECRET"))
        with self.assertRaises(AuthError) as caught:
            QRClient(opener=Opener([error])).begin()
        self.assertNotIn("SECRET", str(caught.exception))

    def test_remote_message_is_never_exposed(self):
        with self.assertRaises(AuthError) as caught:
            QRClient(opener=Opener([{"code": 49500, "message": "SECRET"}])).begin()
        self.assertNotIn("SECRET", str(caught.exception))

    def test_redirect_handler_never_follows_redirects(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid"))


class NativeAuthTests(unittest.TestCase):
    def test_production_client_requires_native_transport(self):
        from stove_linux import native_auth
        self.assertIsNotNone(native_auth.library)
        self.assertEqual(native_auth.library.ember_auth_abi(), 1)
        self.assertTrue(QRClient()._native_transport)
        self.assertFalse(QRClient(opener=Opener([]))._native_transport)

    def test_session_metadata_matches_independent_python_oracle(self):
        from stove_linux import python_auth
        for member in ['', '123', 123, -12]:
            value = {'access_token': 'SYNTHETIC-ACCESS', 'refresh_token': 'SYNTHETIC-REFRESH',
                     'expires_in': 3600, 'expire_in': 7200, 'transaction_id': 'transaction',
                     'member': {'member_no': member}}
            actual, oracle = LauncherSession.from_value(value), python_auth.LauncherSession.from_value(value)
            for name in ['access_token', 'refresh_token', 'expires_in', 'expire_in', 'transaction_id', 'member_no']:
                self.assertEqual(getattr(actual, name), getattr(oracle, name))
            self.assertNotIn('SYNTHETIC', repr(actual))

    def test_qr_matches_oracle_and_rejects_invalid_contracts(self):
        from stove_linux import python_auth
        value = qr_value()
        actual, oracle = QRChallenge.from_value(value), python_auth.QRChallenge.from_value(value)
        self.assertEqual((actual.session, actual.image, actual.expires_at), (oracle.session, oracle.image, oracle.expires_at))
        for field, bad in [('session', ''), ('session', 'x'*4097), ('image', 'a'*262145),
                           ('image', 'not-base64'), ('expires_in', True), ('expires_in', 20),
                           ('expires_in', float('nan')), ('expires_in', float('inf'))]:
            broken = dict(value, **{field: bad})
            with self.assertRaises(AuthError):
                QRChallenge.from_value(broken)
            with self.assertRaises(python_auth.AuthError):
                python_auth.QRChallenge.from_value(broken)

    def test_native_envelope_errors_omit_private_remote_fields(self):
        from stove_linux import native_auth
        for value in [{'code': 49500, 'message': 'SECRET', 'value': 'SECRET'},
                      {'code': 70713, 'message': 'SECRET'}, {'code': True, 'message': 'SECRET'},
                      {'code': 0, 'value': 'SECRET'}, ['SECRET']]:
            with self.assertRaises(AuthError) as caught:
                native_auth._call(4, value)
            self.assertNotIn('SECRET', str(caught.exception))
        with self.assertRaises(AuthError) as caught:
            native_auth._call(4, {'code': 49500, 'value': {'token': 'SECRET'}})
        self.assertEqual(caught.exception.code, 49500)
        self.assertIsNone(caught.exception.http_status)
        self.assertEqual(native_auth._call(4, {'code': 0, 'value': {'safe': 1}}), {'safe': 1})

    def test_untrusted_destinations_rejected_before_network(self):
        from stove_linux import native_auth
        for url in ['http://api.onstove.com/auth-secure/v1.0/qr',
                    'https://api.onstove.com.evil.invalid/auth-secure/v1.0/qr',
                    'https://api.onstove.com:444/auth-secure/v1.0/qr',
                    'https://user@api.onstove.com/auth-secure/v1.0/qr',
                    'https://api.onstove.com/auth-secure/v1.0/qr#SECRET',
                    'https://api.onstove.com/other']:
            with self.assertRaises(AuthError) as caught:
                native_auth._call(5, {'method': 'POST', 'url': url, 'headers': {'Authorization': 'SECRET'}})
            self.assertNotIn('SECRET', str(caught.exception))

    def test_all_production_endpoints_and_headers_are_accepted(self):
        from stove_linux import native_auth
        endpoints = [('POST','https://api.onstove.com/auth-secure/v1.0/qr'),
                     ('GET','https://api.onstove.com/auth-secure/v1.0/qr/status?session=synthetic'),
                     ('GET','https://api.onstove.com/dpms/game/v3.1/live_version?game_id=test'),
                     ('POST','https://s-api.onstove.com/sign/v2.1/pc/signin'),
                     ('POST','https://s-api.onstove.com/sign/v2.0/pc/refresh'),
                     ('POST','https://s-api.onstove.com/gc/v1.4/check/STOVE_CHAOSZERO')]
        client = QRClient(http_context=NativeHTTPContext('a'*40))
        headers = client.http_context.headers(client.region)
        headers.update({'Content-Type': 'application/json', 'Authorization': 'bearer synthetic'})
        for method,url in endpoints:
            self.assertTrue(native_auth._call(5, dict(method=method,url=url,headers=headers)))
        for name, value in [('Authorization','token\r\nInjected: SECRET'),('Host','evil.invalid'),
                            ('Content-Length','1'),('Transfer-Encoding','chunked')]:
            with self.assertRaises(AuthError):
                native_auth._call(5,dict(method='POST',url=endpoints[0][1],headers={name:value}))

    def test_game_contract_rejects_invalid_user_without_leaking_tokens(self):
        from stove_linux.game_auth import GameSession
        tokens = {'access_token': 'SYNTHETIC-ACCESS', 'refresh_token': 'SYNTHETIC-REFRESH'}
        for user in [None, [], {'user_id': 'SECRET\0'}, {'user_id': 'x'*32769}]:
            with self.assertRaises(AuthError) as caught:
                GameSession.from_value(dict(tokens,user=user))
            self.assertNotIn('SECRET', str(caught.exception))
            self.assertNotIn('SYNTHETIC', str(caught.exception))
        session = GameSession.from_value(dict(tokens,user={'user_id':'123'}))
        self.assertEqual(session.user_id,'123')
        self.assertNotIn('SYNTHETIC',repr(session))

    def test_native_transport_receives_exact_verified_signin_contract(self):
        from unittest.mock import patch
        from stove_linux import native_auth
        client = QRClient(http_context=NativeHTTPContext('a'*40))
        challenge = QRChallenge.from_value(qr_value())
        client._gate.reset(challenge)
        client._gate.step(1, challenge, time.time(), {"session":challenge.session,"status":"success"})
        with patch.object(native_auth, 'request', return_value={'access_token':'synthetic','refresh_token':'synthetic'}) as request:
            client.exchange(challenge)
        method,url,headers,payload = request.call_args.args
        self.assertEqual((method,url),('POST','https://s-api.onstove.com/sign/v2.1/pc/signin'))
        self.assertEqual(payload['provider_data'],{'qr_login_session':challenge.session})
        self.assertEqual(headers['Caller-Detail'],'a'*40)
        self.assertEqual(headers['User-Agent'],'STOVEAPIClient/3.0')
        self.assertEqual(headers['Accept-Language'],'en-DO')
        self.assertIsNone(client._approved)

    def test_ffi_capacity_failure_cannot_send_or_write(self):
        import ctypes
        from stove_linux import native_auth
        source=ctypes.create_string_buffer(b'{}')
        target=ctypes.create_string_buffer(b'Z'*4,4)
        self.assertLess(native_auth.library.ember_auth_call(0,source,2,target,4),0)
        self.assertEqual(target.raw,b'Z'*4)


class NativeQRGateTests(unittest.TestCase):
    def test_consumed_approval_cannot_be_rearmed_by_poll(self):
        client, challenge, opener = AuthTests().approved_client({'code':0,'value':{'access_token':'synthetic','refresh_token':'synthetic'}})
        client.exchange(challenge)
        with self.assertRaises(AuthError):
            client._gate.step(1,challenge,time.time(),{'session':challenge.session,'status':'success'})
        with self.assertRaises(AuthError):
            client.exchange(challenge)
        self.assertEqual(len(opener.calls),3)

    def test_failed_new_qr_discards_previous_native_approval(self):
        client, challenge, opener = AuthTests().approved_client({'code':49500,'message':'PRIVATE'})
        with self.assertRaises(AuthError):
            client.begin()
        with self.assertRaises(AuthError):
            client.exchange(challenge)
        self.assertEqual(len(opener.calls),3)

    def test_expired_approved_qr_cannot_exchange(self):
        from unittest.mock import patch
        client,challenge,opener=AuthTests().approved_client({'code':0,'value':{}})
        with patch('stove_linux.auth.time.time',return_value=challenge.expires_at):
            with self.assertRaises(AuthError):
                client.exchange(challenge)
        self.assertEqual(len(opener.calls),2)

    def test_old_challenge_cannot_approve_new_qr_and_free_is_idempotent(self):
        from stove_linux import native_auth
        old=QRChallenge('old','',time.time()+30)
        new=QRChallenge('new','',time.time()+30)
        gate=native_auth.QRGate()
        try:
            gate.reset(old)
            gate.step(1,old,time.time(),{'session':'old','status':'success'})
            gate.reset(new)
            with self.assertRaises(AuthError):
                gate.step(1,old,time.time(),{'session':'old','status':'success'})
            with self.assertRaises(AuthError):
                gate.step(2,new,time.time())
            gate.step(1,new,time.time(),{'session':'new','status':'success'})
            self.assertEqual(gate.step(2,new,time.time()),0)
        finally:
            gate.close()
            gate.close()
        with self.assertRaises(ValueError):
            gate.step(0,new,time.time())
