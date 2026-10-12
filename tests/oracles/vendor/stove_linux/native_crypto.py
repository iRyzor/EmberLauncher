"""Private crypto ABI. No keys or plaintext are retained by the Rust component."""
import ctypes
from .native_protocol import library

MAX_INPUT = 1_048_576
if library is not None:
    try:
        library.ember_crypto_abi.argtypes = []
        library.ember_crypto_abi.restype = ctypes.c_uint32
        if library.ember_crypto_abi() != 1:
            raise RuntimeError('Unsupported Rust crypto ABI')
        library.ember_rsa_public_size.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        library.ember_rsa_public_size.restype = ctypes.c_int32
        for name in ('ember_aes_encrypt', 'ember_aes_decrypt', 'ember_rsa_encrypt'):
            function = getattr(library, name)
            function.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
            function.restype = ctypes.c_int32
    except AttributeError:
        raise RuntimeError('Rust crypto component is outdated; rebuild the engine') from None


def _call(name, key, data, capacity):
    key_buffer = ctypes.create_string_buffer(key)
    source = ctypes.create_string_buffer(data)
    target = ctypes.create_string_buffer(capacity)
    try:
        result = getattr(library, name)(key_buffer, len(key), source, len(data), target, capacity)
        if result < 0:
            raise ValueError('Invalid SDK cryptographic input or operation failed')
        return target.raw[:result]
    finally:
        # Python immutable inputs/results still belong to the private engine.
        # Explicitly clear the additional temporary copies made for ctypes.
        for buffer in (key_buffer, source, target):
            ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))


def aes_encrypt(key, plain):
    if len(plain) > MAX_INPUT:
        raise ValueError('SDK cryptographic input exceeds the size limit')
    return _call('ember_aes_encrypt', key, plain, (len(plain) // 16 + 1) * 16)


def aes_decrypt(key, encrypted):
    if len(encrypted) > MAX_INPUT + 16:
        raise ValueError('SDK cryptographic input exceeds the size limit')
    return _call('ember_aes_decrypt', key, encrypted, max(1, len(encrypted)))


def rsa_public_size(der):
    source = ctypes.create_string_buffer(der)
    size = library.ember_rsa_public_size(source, len(der))
    if not 128 <= size <= 512:
        raise ValueError('Invalid SDK RSA public key')
    return size


def rsa_encrypt(der, plain):
    size = rsa_public_size(der)
    if len(plain) > size - 11:
        raise ValueError('Invalid SDK RSA plaintext size')
    return _call('ember_rsa_encrypt', der, plain, size)
