"""Independent Python oracle for PCSDK 3.4.0 primitives. Implements the existing wire format, not a new cipher.

AES-CBC uses the 32 ASCII bytes of aesKey and its first 16 bytes as IV.
This deterministic IV is a platform compatibility constraint.
"""
import base64
import json
import re

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .protocol import Packet


def checked_key(key: bytes):
    if not isinstance(key, bytes) or len(key) != 32 or any(c < 32 or c > 126 for c in key):
        raise ValueError("PCSDK aesKey must contain exactly 32 printable ASCII bytes")
    return key


def encrypt_value(key: bytes, text: str) -> str:
    checked_key(key)
    padder = padding.PKCS7(128).padder()
    plain = text.encode("utf-8")
    data = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    return (encryptor.update(data) + encryptor.finalize()).hex().upper()


def decrypt_value(key: bytes, encrypted: str) -> str:
    checked_key(key)
    if not isinstance(encrypted, str) or not re.fullmatch(r"(?:[a-fA-F0-9]{32})+", encrypted):
        raise ValueError("Invalid PCSDK hexadecimal ciphertext")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
    plain = decryptor.update(bytes.fromhex(encrypted)) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(plain) + unpadder.finalize()).decode("utf-8")


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_sdk_public_key(encoded: str):
    if not isinstance(encoded, str) or len(encoded) > 16384:
        raise ValueError("Invalid SDK public key")
    try:
        public = serialization.load_der_public_key(base64.b64decode(encoded, validate=True))
    except (ValueError, TypeError):
        raise ValueError("Invalid SDK public key encoding") from None
    if not isinstance(public, rsa.RSAPublicKey) or not 1024 <= public.key_size <= 4096:
        raise ValueError("Unexpected SDK RSA key type or size")
    return public


def aes_key_packet(encoded_public_key: str, key: bytes) -> Packet:
    checked_key(key)
    public = load_sdk_public_key(encoded_public_key)
    # PacketParsing decrypts the WHOLE value before Recv_AES_KEY parses aesKey.
    plain = compact({"aesKey": key.decode("ascii")}).encode("utf-8")
    encrypted = public.encrypt(plain, rsa_padding.PKCS1v15())
    envelope = {"code": 0, "message": "Success", "value": base64.b64encode(encrypted).decode("ascii")}
    return Packet(2000, compact(envelope).encode("utf-8"))


def encrypted_packet(packet_id: int, key: bytes, value: dict) -> Packet:
    if not isinstance(value, dict):
        raise ValueError("PCSDK information must be a JSON object")
    envelope = {"code": 0, "message": "Success", "value": encrypt_value(key, compact(value))}
    return Packet(packet_id, compact(envelope).encode("utf-8"))
