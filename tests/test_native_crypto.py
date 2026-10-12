import base64
import json
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from stove_linux.crypto import aes_key_packet, decrypt_value, encrypt_value, encrypted_packet

KEY = b"0123456789ABCDEF0123456789ABCDEF"


class CryptoTests(unittest.TestCase):
    def test_sdk_compatible_known_vector(self):
        # This ciphertext was separately accepted by the original SDK AES decoder
        # via tools/test_sdk_crypto.py, not just a Python encode/decode roundtrip.
        expected = "0063A0602309A13A457AFE0CAC460548E7DE4449A7805BCF92243D07F33AC5B0"
        self.assertEqual(encrypt_value(KEY, "STOVE-LINUX-CRYPTO-TEST"), expected)
        self.assertEqual(decrypt_value(KEY, expected), "STOVE-LINUX-CRYPTO-TEST")

    def test_non_ascii_json_roundtrip(self):
        packet = encrypted_packet(2001, KEY, {"test": "sesión 日本語"})
        outer = json.loads(packet.payload)
        self.assertEqual(outer["message"], "Success")
        self.assertEqual(json.loads(decrypt_value(KEY, outer["value"])), {"test": "sesión 日本語"})

    def test_rsa_encrypts_entire_value_with_pkcs1v15(self):
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        encoded = base64.b64encode(private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        packet = aes_key_packet(encoded, KEY)
        envelope = json.loads(packet.payload)
        plain = private.decrypt(base64.b64decode(envelope["value"]), padding.PKCS1v15())
        self.assertEqual(json.loads(plain), {"aesKey": KEY.decode()})

    def test_invalid_ciphertexts_and_keys(self):
        for cipher in ["", "not-hex", "00", "0" * 32]:
            with self.assertRaises(ValueError):
                decrypt_value(KEY, cipher)
        with self.assertRaises(ValueError):
            encrypt_value(b"short", "test")


class NativeCryptoParityTests(unittest.TestCase):
    def test_requires_rust_crypto_abi(self):
        from stove_linux import native_crypto
        self.assertIsNotNone(native_crypto.library)
        self.assertEqual(native_crypto.library.ember_crypto_abi(), 1)

    def test_aes_matches_independent_oracle_at_boundaries(self):
        from stove_linux import python_crypto
        import random
        rng = random.Random(20261011)
        for length in [0, 1, 15, 16, 17, 31, 32, 255, 1024, 32768]:
            key = bytes(rng.randrange(32, 127) for _ in range(32))
            text = 'sesión 日本語 🎮\0' + 'x' * length
            expected = python_crypto.encrypt_value(key, text)
            self.assertEqual(encrypt_value(key, text), expected)
            self.assertEqual(decrypt_value(key, expected.lower()), text)
            self.assertEqual(python_crypto.decrypt_value(key, encrypt_value(key, text)), text)
        for text in ['', 'a' * 16, 'a' * 32]:
            self.assertEqual(encrypt_value(KEY, text), python_crypto.encrypt_value(KEY, text))
            self.assertEqual(decrypt_value(KEY, encrypt_value(KEY, text)), text)

    def test_rejects_invalid_padding_utf8_keys_and_size(self):
        from stove_linux import native_crypto
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        for key in [b'', b'x' * 31, b'x' * 33, b'\x00' * 32, b'\x7f' * 32, b'\xff' * 32]:
            with self.assertRaises(ValueError):
                encrypt_value(key, 'example')
        for padded in [b'x' * 15 + b'\x00', b'x' * 14 + b'\x01\x02', b'\xff' + b'\x0f' * 15]:
            cipher = Cipher(algorithms.AES(KEY), modes.CBC(KEY[:16])).encryptor()
            encrypted = (cipher.update(padded) + cipher.finalize()).hex()
            with self.assertRaises(ValueError):
                decrypt_value(KEY, encrypted)
        with self.assertRaises(ValueError):
            native_crypto.aes_encrypt(KEY, b'x' * (native_crypto.MAX_INPUT + 1))
        with self.assertRaises(ValueError):
            decrypt_value(KEY, '00' * (native_crypto.MAX_INPUT + 32))

    def test_rsa_der_formats_sizes_and_randomized_padding(self):
        from stove_linux import python_crypto
        for bits in [1024, 2048, 4096]:
            private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
            for fmt in [serialization.PublicFormat.SubjectPublicKeyInfo, serialization.PublicFormat.PKCS1]:
                encoded = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.DER, fmt)).decode()
                packets = [aes_key_packet(encoded, KEY), aes_key_packet(encoded, KEY), python_crypto.aes_key_packet(encoded, KEY)]
                ciphertexts = []
                for packet in packets:
                    self.assertEqual(packet.packet_id, 2000)
                    envelope = json.loads(packet.payload)
                    self.assertEqual((envelope['code'], envelope['message']), (0, 'Success'))
                    ciphertext = base64.b64decode(envelope['value'], validate=True)
                    self.assertEqual(len(ciphertext), bits // 8)
                    self.assertEqual(private.decrypt(ciphertext, padding.PKCS1v15()), b'{"aesKey":"' + KEY + b'"}')
                    ciphertexts.append(ciphertext)
                self.assertNotEqual(ciphertexts[0], ciphertexts[1])

    def test_rejects_wrong_key_type_sizes_and_der(self):
        from cryptography.hazmat.primitives.asymmetric import ec
        from stove_linux.crypto import load_sdk_public_key
        public_keys = [ec.generate_private_key(ec.SECP256R1()).public_key()]
        for bits in [512, 8192]:
            public_keys.append(rsa.RSAPublicNumbers(65537, (1 << (bits - 1)) + 1).public_key())
        for public in public_keys:
            encoded = base64.b64encode(public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
            with self.assertRaises(ValueError):
                load_sdk_public_key(encoded)
        for encoded in [None, '', '!!!', 'a' * 16385, base64.b64encode(b'invalid DER').decode()]:
            with self.assertRaises(ValueError):
                load_sdk_public_key(encoded)

    def test_failed_native_operation_leaves_output_untouched(self):
        from stove_linux import native_crypto
        import ctypes
        library = native_crypto.library
        key = ctypes.create_string_buffer(KEY)
        source = ctypes.create_string_buffer(b'x')
        output = ctypes.create_string_buffer(b'Z' * 32, 32)
        self.assertLess(library.ember_aes_encrypt(key, 32, source, 1, output, 15), 0)
        self.assertEqual(output.raw, b'Z' * 32)
        self.assertLess(library.ember_aes_encrypt(None, 32, source, 1, output, 32), 0)
        self.assertEqual(output.raw, b'Z' * 32)

    def test_real_handshake_is_decryptable_with_python(self):
        from stove_linux.ipc import LauncherHandshake
        from stove_linux.protocol import Packet, Decoder
        from stove_linux import python_crypto
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        encoded = base64.b64encode(private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
        info = {'game_id': 'test-game', 'access_token': 'synthetic-test-token', 'language': '日本語'}
        handshake = LauncherHandshake(info, key=KEY)
        try:
            responses = handshake.receive(Packet(1000, json.dumps({'code': 0, 'value': {'publicKey': encoded, 'processId': '123'}}).encode()))
            with Decoder() as decoder:
                responses = decoder.feed(b''.join(p.encode() for p in responses))
                decoder.finish()
            key_envelope, info_envelope = [json.loads(p.payload) for p in responses]
            self.assertEqual(json.loads(private.decrypt(base64.b64decode(key_envelope['value']), padding.PKCS1v15())), {'aesKey': KEY.decode()})
            self.assertEqual(json.loads(python_crypto.decrypt_value(KEY, info_envelope['value'])), info)
            self.assertEqual(handshake.receive(Packet(1001, b'{"code":0,"value":""}')), [])
        finally:
            handshake.close()
