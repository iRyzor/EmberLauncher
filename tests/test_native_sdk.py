"""SDK state and response interoperability against the previous Python handshake."""
import base64
from concurrent.futures import ThreadPoolExecutor
import ctypes
import json
import unittest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from stove_linux import ipc, native_sdk, python_ipc, python_crypto
from stove_linux.protocol import Packet

KEY = b'0123456789abcdef0123456789abcdef'
INFO = {'game_id':'test-game','access_token':'synthetic-test-token','language':'日本語'}


class NativeSDKTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private = rsa.generate_private_key(public_exponent=65537,key_size=2048)
        cls.public = base64.b64encode(cls.private.public_key().public_bytes(
            serialization.Encoding.DER,serialization.PublicFormat.SubjectPublicKeyInfo)).decode()

    def public_packet(self, pid='123', key=None):
        return Packet(1000,json.dumps({'code':0,'value':{'processId':pid,'publicKey':self.public if key is None else key}}).encode())

    def test_requires_native_abi(self):
        self.assertIsNotNone(native_sdk.library)
        self.assertEqual(native_sdk.library.ember_sdk_abi(),2)

    def test_information_validation_matches_oracle(self):
        for info in [INFO,None,[],{}, {'game_id':'game'}, {**INFO,'unknown':'x'},
                     {**INFO,'language':True},{**INFO,'language':'\0'},
                     {**INFO,'language':'x'*32769},{**INFO,'language':'語'*32768}]:
            outcomes=[]
            for cls in [ipc.LauncherHandshake,python_ipc.LauncherHandshake]:
                try:
                    state=cls(info,key=KEY)
                    outcomes.append('ok');state.close()
                except (ipc.IPCError,python_ipc.IPCError) as error:
                    outcomes.append(str(error))
            self.assertEqual(*outcomes)

    def test_response_order_and_plaintext_match_original(self):
        outcomes=[]
        for cls in [ipc.LauncherHandshake,python_ipc.LauncherHandshake]:
            state=cls(INFO,key=KEY)
            try:
                packets=state.receive(self.public_packet())
                self.assertEqual([p.packet_id for p in packets],[2000,2001])
                key=json.loads(self.private.decrypt(base64.b64decode(json.loads(packets[0].payload)['value']),padding.PKCS1v15()))
                info=json.loads(python_crypto.decrypt_value(KEY,json.loads(packets[1].payload)['value']))
                outcomes.append((key,info,packets[1].payload))
                for _ in range(100):
                    self.assertEqual(state.receive(Packet(1001,b'{"code":0,"value":""}')),[])
                self.assertEqual(state.heartbeats,100)
                self.assertEqual(state.phase,'information_sent')
            finally:
                state.close()
        self.assertEqual(*outcomes)

    def test_rejected_packets_preserve_state_and_match_original(self):
        invalid=[Packet(2**32+1000,b'{"code":0}'),Packet(1000,b'bad'),Packet(1000,b'[]'),Packet(1000,b'{"code":true}'),
                 Packet(1000,b'{"code":0.0}'),Packet(1000,b'{"code":1}'),
                 Packet(1000,b'{"code":0,"value":[]}'),Packet(1002,b'{"code":0}'),
                 Packet(1001,b'{"code":0,"value":""}'),
                 self.public_packet('0'),self.public_packet('4294967296'),self.public_packet('１２３'),
                 self.public_packet(123),self.public_packet('123',key='not-base64')]
        for packet in invalid:
            outcomes=[]
            for cls in [ipc.LauncherHandshake,python_ipc.LauncherHandshake]:
                state=cls(INFO,key=KEY)
                try:
                    with self.assertRaises((ipc.IPCError,python_ipc.IPCError)) as caught:
                        state.receive(packet)
                    outcomes.append(str(caught.exception))
                    self.assertEqual(state.phase,'waiting_public_key')
                    self.assertEqual(state.heartbeats,0)
                    self.assertEqual(len(state.receive(self.public_packet())),2)
                finally:
                    state.close()
            self.assertEqual(*outcomes)

    def test_oversize_responses_do_not_commit_public_key(self):
        state=ipc.LauncherHandshake({**INFO,'access_token':'x'*32768,'refresh_token':'x'*32768},key=KEY)
        try:
            with self.assertRaises(ipc.IPCError):
                state.receive(self.public_packet())
            self.assertEqual(state.phase,'waiting_public_key')
            self.assertEqual(state.heartbeats,0)
        finally:
            state.close()

    def test_concurrent_public_key_cannot_be_accepted_twice(self):
        state=ipc.LauncherHandshake(INFO,key=KEY)
        try:
            def receive(_):
                try:
                    return len(state.receive(self.public_packet()))
                except ipc.IPCError:
                    return 'rejected'
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertCountEqual(list(pool.map(receive,range(2))),[2,'rejected'])
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _:state.receive(Packet(1001,b'{"code":0,"value":""}')),range(100)))
            self.assertEqual(state.heartbeats,100)
        finally:
            state.close()
        state.close()
        self.assertEqual(state.phase,'closed')
        self.assertEqual(state.heartbeats,100)
        self.assertEqual(state._info,{})
        self.assertEqual(state._key,b'')
        with self.assertRaises(ipc.IPCError):
            state.receive(self.public_packet())

    def test_native_invalid_calls_and_closed_handle_wrapper(self):
        library=native_sdk.library
        self.assertLess(library.ember_sdk_info(None,1),0)
        self.assertLess(library.ember_sdk_step(None,0,1000,None,1),0)
        state=native_sdk.SDKState()
        with state.lock:
            self.assertLess(library.ember_sdk_step(state._handle,99,0,None,0),0)
            self.assertLess(library.ember_sdk_step(state._handle,0,1000,None,1),0)
        state.close();state.close()
        with self.assertRaises(ValueError):
            state.step(2)


    def test_native_responses_preserve_unicode_escapes_and_ascii_key(self):
        info = {**INFO,'language':'日本語😀\r\n"\\<>','member_nickname':'é'}
        key = b'"\\'*16
        packet = self.public_packet()
        result = native_sdk.responses(packet,key,info)
        encoded_key = json.loads(result[0].payload)['value']
        plain_key = json.loads(self.private.decrypt(base64.b64decode(encoded_key),padding.PKCS1v15()))
        self.assertEqual(plain_key,{'aesKey':key.decode()})
        expected = python_crypto.encrypted_packet(2001,key,info)
        self.assertEqual(result[1].encode(),expected.encode())

    def test_native_response_frame_limit_matches_oracle(self):
        for size in [32000,32600,32640,32650,32700,32768]:
            info = {**INFO,'access_token':'x'*size}
            expected = python_crypto.encrypted_packet(2001,KEY,info)
            try:
                encoded = expected.encode()
            except ValueError:
                with self.assertRaises(ValueError):
                    native_sdk.responses(self.public_packet(),KEY,info)
            else:
                result = native_sdk.responses(self.public_packet(),KEY,info)
                self.assertEqual(result[1].encode(),encoded)

    def test_response_public_key_formats_and_sizes(self):
        for bits in [1024,2048,4096]:
            private = self.private if bits==2048 else rsa.generate_private_key(public_exponent=65537,key_size=bits)
            for fmt in [serialization.PublicFormat.SubjectPublicKeyInfo,serialization.PublicFormat.PKCS1]:
                public = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.DER,fmt)).decode()
                result = native_sdk.responses(self.public_packet(key=public),KEY,INFO)
                encrypted = base64.b64decode(json.loads(result[0].payload)['value'])
                self.assertEqual(len(encrypted),bits//8)
                self.assertEqual(json.loads(private.decrypt(encrypted,padding.PKCS1v15())),{'aesKey':KEY.decode()})

    def test_response_errors_never_write_partial_frames(self):
        target = ctypes.create_string_buffer(b'x'*(2*65535))
        packet = self.public_packet().payload
        raw_info = json.dumps(INFO).encode()
        def call(packet,key,info,capacity=2*65535):
            buffers = [ctypes.create_string_buffer(raw) for raw in [packet,key,info]]
            return native_sdk.library.ember_sdk_responses(buffers[0],len(packet),buffers[1],len(key),buffers[2],len(info),target,capacity)
        for value,key,info,capacity in [
            (packet,KEY,raw_info,1),(packet,b'bad',raw_info,2*65535),
            (b'bad',KEY,raw_info,2*65535),
            (self.public_packet(key='bad').payload,KEY,raw_info,2*65535),
            (packet,KEY,b'{}',2*65535),
            (packet,KEY,json.dumps({**INFO,'access_token':'x'*32768,'refresh_token':'x'*32768}).encode(),2*65535),
            (b'{"code":1,"value":{}}',KEY,raw_info,2*65535),
        ]:
            self.assertLess(call(value,key,info,capacity),0)
            self.assertEqual(target.raw[:16],b'x'*16)
        self.assertLess(native_sdk.library.ember_sdk_responses(None,1,None,32,None,1,target,len(target)),0)
        self.assertEqual(target.raw[:16],b'x'*16)
