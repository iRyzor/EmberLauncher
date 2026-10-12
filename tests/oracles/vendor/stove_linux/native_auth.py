"""Private Rust authentication transport/validation; never exposes raw diagnostics."""
import ctypes
import json
import threading
from .native_protocol import library

LIMIT = 1_048_576
CAPACITY = LIMIT + 4096
if library is not None:
    try:
        library.ember_auth_abi.argtypes = []
        library.ember_auth_abi.restype = ctypes.c_uint32
        if library.ember_auth_abi() != 1:
            raise RuntimeError('Unsupported Rust authentication ABI')
        library.ember_auth_call.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t,
                                          ctypes.c_void_p, ctypes.c_size_t]
        library.ember_auth_call.restype = ctypes.c_int32
    except AttributeError:
        raise RuntimeError('Rust authentication component is outdated; rebuild the engine') from None

MESSAGES = {
    'request': 'La petición de autenticación no tiene un destino o formato permitido.',
    'headers': 'STOVE devolvió metadatos no aptos para una petición HTTP.',
    'network': 'No se pudo contactar al servicio de autenticación de STOVE.',
    'size': 'La respuesta de STOVE excedió el límite esperado.',
    'json': 'STOVE devolvió una respuesta de autenticación ilegible.',
    'envelope': 'STOVE devolvió un envelope de autenticación inválido.',
    'value': 'STOVE no devolvió datos de autenticación.',
    'qr_session': 'El servidor no devolvió una sesión QR válida.',
    'qr_image': 'El servidor no devolvió una imagen QR válida.',
    'qr_encoding': 'La imagen QR recibida no tiene el formato esperado.',
    'qr_png': 'La imagen QR recibida no es PNG.',
    'qr_expiry': 'El vencimiento del QR no tiene el formato esperado.',
    'tokens': 'STOVE no devolvió los tokens de launcher esperados.',
    'expiry': 'STOVE devolvió un vencimiento de sesión inesperado.',
    'session_metadata': 'STOVE devolvió metadatos de sesión inesperados.',
    'account_metadata': 'STOVE devolvió metadatos de cuenta inesperados.',
    'game_metadata': 'STOVE devolvió metadatos de juego inesperados.',
    'game_user': 'STOVE devolvió un identificador de juego inesperado.',
}


def _call(kind, value):
    from .auth import AuthError
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError):
        raise AuthError(MESSAGES['json']) from None
    if len(raw) > LIMIT:
        raise AuthError(MESSAGES['size'])
    source, target = ctypes.create_string_buffer(raw), ctypes.create_string_buffer(CAPACITY)
    try:
        size = library.ember_auth_call(kind, source, len(raw), target, CAPACITY)
        if size < 0:
            raise AuthError(MESSAGES['json'])
        result = json.loads(target.raw[:size])
    finally:
        for buffer in (source, target):
            ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))
    kind = result.get('error')
    if kind == 'http':
        status = result['http_status']
        raise AuthError(f'STOVE respondió HTTP {status}.', http_status=status)
    if kind == 'rejected':
        code = result.get('code')
        if code == 49500:
            raise AuthError('STOVE restringió esta petición de autenticación (49500). '
                            'El prototipo no volverá a intentarla automáticamente.', code=49500)
        suffix = f' (código {code})' if type(code) is int else ''
        raise AuthError('STOVE rechazó la autenticación' + suffix + '.', code=code)
    if kind:
        raise AuthError(MESSAGES.get(kind, MESSAGES['json']))
    return result['value']


def request(method, url, headers, payload):
    return _call(0, dict(method=method, url=url, headers=headers, payload=payload))


def validate_qr(value):
    return _call(1, value)


def validate_session(value):
    return _call(2, value)


def validate_game(value):
    return _call(3, value)


# Separate ABI: native ownership of the one-use QR approval.
if library is not None:
    try:
        library.ember_qr_abi.argtypes = []
        library.ember_qr_abi.restype = ctypes.c_uint32
        if library.ember_qr_abi() != 1:
            raise RuntimeError('Unsupported Rust QR ABI')
        library.ember_qr_new.argtypes = []
        library.ember_qr_new.restype = ctypes.c_void_p
        library.ember_qr_free.argtypes = [ctypes.c_void_p]
        library.ember_qr_free.restype = None
        library.ember_qr_reset.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_double]
        library.ember_qr_reset.restype = ctypes.c_int32
        library.ember_qr_step.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.c_double, ctypes.c_void_p, ctypes.c_size_t]
        library.ember_qr_step.restype = ctypes.c_int32
    except AttributeError:
        raise RuntimeError('Rust QR component is outdated; rebuild the engine') from None


class QRGate:
    def __init__(self):
        self._lock = threading.RLock()
        self._library = library
        self._handle = library.ember_qr_new()
        if not self._handle:
            raise MemoryError('Cannot allocate QR approval state')

    def _ensure_open(self):
        if not self._handle:
            raise ValueError('QR approval state is closed')

    def reset(self, challenge):
        from .auth import AuthError
        raw = challenge.session.encode('utf-8')
        source = ctypes.create_string_buffer(raw)
        try:
            with self._lock:
                self._ensure_open()
                if self._library.ember_qr_reset(self._handle, source, len(raw), challenge.expires_at) != 0:
                    raise AuthError(MESSAGES['qr_session'])
        finally:
            ctypes.memset(ctypes.addressof(source), 0, ctypes.sizeof(source))

    def step(self, action, challenge, now, value=None):
        from .auth import AuthError
        raw = challenge.session.encode('utf-8')
        # Only the status contract enters the native state machine.
        response = b'' if value is None else json.dumps({
            'status': value.get('status'), 'session': value.get('session')
        }, ensure_ascii=False).encode('utf-8')
        source, data = ctypes.create_string_buffer(raw), ctypes.create_string_buffer(response)
        try:
            with self._lock:
                self._ensure_open()
                result = self._library.ember_qr_step(self._handle, action, source, len(raw), now, data, len(response))
        finally:
            for buffer in (source, data):
                ctypes.memset(ctypes.addressof(buffer), 0, ctypes.sizeof(buffer))
        if result == -2:
            raise AuthError('La respuesta QR no coincide con la sesión iniciada.')
        if result == -3:
            raise AuthError('Primero debe aprobarse un QR nuevo en la app de STOVE.')
        if result < 0:
            raise AuthError('STOVE devolvió un estado QR desconocido.')
        return result

    def close(self):
        with self._lock:
            if self._handle:
                self._library.ember_qr_free(self._handle)
                self._handle = None

    def __del__(self):
        if getattr(self, '_handle', None):
            self.close()
