"""Existing PCSDK cryptography, executed in Rust for packaged engines.

AES-CBC retains the SDK's ASCII key and deterministic first-16-byte IV.
Python handles JSON/hex/base64 envelopes; its independent cipher is a source oracle.
"""
import base64
import re
from . import native_crypto, python_crypto
from .protocol import Packet

checked_key = python_crypto.checked_key
compact = python_crypto.compact


def encrypt_value(key: bytes, text: str) -> str:
    checked_key(key)
    if native_crypto.library is None:
        return python_crypto.encrypt_value(key, text)
    return native_crypto.aes_encrypt(key, text.encode('utf-8')).hex().upper()


def decrypt_value(key: bytes, encrypted: str) -> str:
    checked_key(key)
    if native_crypto.library is None:
        return python_crypto.decrypt_value(key, encrypted)
    if (not isinstance(encrypted, str) or len(encrypted) > 2 * (native_crypto.MAX_INPUT + 16)
            or not re.fullmatch(r'(?:[a-fA-F0-9]{32})+', encrypted)):
        raise ValueError('Invalid PCSDK hexadecimal ciphertext')
    return native_crypto.aes_decrypt(key, bytes.fromhex(encrypted)).decode('utf-8')


class _SDKPublicKey:
    def __init__(self, der):
        self.der = der
        # Bits are checked on the Rust modulus; ciphertext size can round up.
        self.size = native_crypto.rsa_public_size(der)

    def encrypt(self, plain):
        return native_crypto.rsa_encrypt(self.der, plain)


def load_sdk_public_key(encoded: str):
    if native_crypto.library is None:
        return python_crypto.load_sdk_public_key(encoded)
    if not isinstance(encoded, str) or len(encoded) > 16384:
        raise ValueError('Invalid SDK public key')
    try:
        der = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError('Invalid SDK public key encoding') from None
    return _SDKPublicKey(der)


def aes_key_packet(encoded_public_key: str, key: bytes) -> Packet:
    checked_key(key)
    if native_crypto.library is None:
        return python_crypto.aes_key_packet(encoded_public_key, key)
    public = load_sdk_public_key(encoded_public_key)
    plain = compact({'aesKey': key.decode('ascii')}).encode('utf-8')
    encrypted = public.encrypt(plain)
    envelope = {'code': 0, 'message': 'Success', 'value': base64.b64encode(encrypted).decode('ascii')}
    return Packet(2000, compact(envelope).encode('utf-8'))


def encrypted_packet(packet_id: int, key: bytes, value: dict) -> Packet:
    if not isinstance(value, dict):
        raise ValueError('PCSDK information must be a JSON object')
    envelope = {'code': 0, 'message': 'Success', 'value': encrypt_value(key, compact(value))}
    return Packet(packet_id, compact(envelope).encode('utf-8'))
