"""Native process predicates; enumeration and identity-pinned signals stay on host."""
import ctypes
import json
from .native_protocol import library
from . import python_process

if library is not None:
    try:
        library.ember_process_abi.argtypes = []
        library.ember_process_abi.restype = ctypes.c_uint32
        if library.ember_process_abi() != 2:
            raise RuntimeError('Unsupported Rust process ABI')
        library.ember_process_check.argtypes = [ctypes.c_uint32,ctypes.c_void_p,ctypes.c_size_t,
            ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t]
        library.ember_process_check.restype = ctypes.c_int32
        library.ember_process_lifecycle.argtypes = [ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t]
        library.ember_process_lifecycle.restype = ctypes.c_int32
    except AttributeError:
        raise RuntimeError('Rust process component is outdated; rebuild the engine') from None


def _check(kind, game, windows, value):
    buffers = [ctypes.create_string_buffer(part) for part in (game,windows,value)]
    result = library.ember_process_check(kind,buffers[0],len(game),buffers[1],len(windows),buffers[2],len(value))
    if result < 0:
        raise ValueError('Invalid process metadata')
    return bool(result)


def path_match(game, windows, candidate):
    return _check(0,game,windows,candidate) if library is not None else python_process.path_match(game,windows,candidate)


def mapping_match(game, maps):
    return _check(1,game,b'',maps) if library is not None else python_process.mapping_match(game,maps)


def excluded(name):
    return _check(2,b'',b'',name) if library is not None else python_process.excluded(name)


def lifecycle(baseline, owned, current, alive, seen, empty_since, now):
    if library is None:
        return python_process.lifecycle(baseline, owned, current, alive, seen, empty_since, now)
    raw = json.dumps(dict(baseline=baseline, owned=owned, current=current, alive=alive,
                         seen=seen, empty_since=empty_since, now=now), allow_nan=False).encode()
    source = ctypes.create_string_buffer(raw)
    target = ctypes.create_string_buffer(1024*1024+4096)
    count = library.ember_process_lifecycle(source,len(raw),target,len(target))
    if count < 0:
        raise ValueError('Invalid process lifecycle metadata')
    result = json.loads(target.raw[:count])
    result['owned'] = {int(pid):start for pid,start in result['owned'].items()}
    return result
