"""Private Rust renewal decisions and identity continuity."""
import ctypes
from .native_protocol import library

if library is not None:
    try:
        library.ember_session_abi.argtypes = []
        library.ember_session_abi.restype = ctypes.c_uint32
        if library.ember_session_abi() != 1:
            raise RuntimeError('Unsupported Rust session ABI')
        library.ember_session_decision.argtypes = [ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint32]
        library.ember_session_decision.restype = ctypes.c_int32
        library.ember_session_identity.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        library.ember_session_identity.restype = ctypes.c_int32
    except AttributeError:
        raise RuntimeError('Rust session component is outdated; rebuild the engine') from None


def decision(stage, http_status=None, terminal=False):
    status = http_status if type(http_status) is int and 100 <= http_status <= 599 else 0
    if library is None:
        if stage == 0:
            return 1 if status == 401 else 0
        if stage == 1:
            return 2 if terminal or status in (401, 403) else 0
        if stage == 2:
            return 2 if status == 401 else 0
        raise ValueError('Invalid session policy stage')
    result = library.ember_session_decision(stage, status, bool(terminal))
    if result < 0:
        raise ValueError('Invalid session policy stage')
    return result


def renewed_identity(previous, new):
    if not isinstance(previous, str) or not isinstance(new, str):
        raise ValueError('Invalid account identity')
    if library is None:
        if previous and new and previous != new:
            raise ValueError('Account identity changed')
        return new or previous
    old_bytes, new_bytes = previous.encode('utf-8'), new.encode('utf-8')
    if max(len(old_bytes), len(new_bytes)) > 1048576:
        raise ValueError('Invalid account identity')
    old, current = ctypes.create_string_buffer(old_bytes), ctypes.create_string_buffer(new_bytes)
    target = ctypes.create_string_buffer(max(1, len(old_bytes), len(new_bytes)))
    try:
        result = library.ember_session_identity(old,len(old_bytes),current,len(new_bytes),target,ctypes.sizeof(target))
        if result < 0:
            raise ValueError('Invalid or changed account identity')
        return target.raw[:result].decode('utf-8')
    finally:
        for buffer in (old, current, target):
            ctypes.memset(ctypes.addressof(buffer),0,ctypes.sizeof(buffer))
