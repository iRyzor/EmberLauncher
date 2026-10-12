"""Private SDK state handle; callers serialize validation/construction/commit."""
import ctypes
import json
import threading
from .native_protocol import library

MESSAGES = {
    -1: 'Invalid SDK JSON packet', -2: 'SDK packet reports an error or invalid envelope',
    -4: 'Unexpected repeated SDK public key', -5: 'Invalid SDK public key envelope',
    -6: 'Invalid SDK process identifier', -7: 'Unexpected SDK heartbeat',
    -8: 'Unsupported SDK packet ID', -9: 'Invalid or closed SDK state',
}
if library is not None:
    try:
        library.ember_sdk_abi.argtypes = []
        library.ember_sdk_abi.restype = ctypes.c_uint32
        if library.ember_sdk_abi() != 2:
            raise RuntimeError('Unsupported Rust SDK ABI')
        library.ember_sdk_info.argtypes = [ctypes.c_void_p,ctypes.c_size_t]
        library.ember_sdk_info.restype = ctypes.c_int32
        library.ember_sdk_new.argtypes = []
        library.ember_sdk_new.restype = ctypes.c_void_p
        library.ember_sdk_free.argtypes = [ctypes.c_void_p]
        library.ember_sdk_free.restype = None
        library.ember_sdk_step.argtypes = [ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_size_t]
        library.ember_sdk_step.restype = ctypes.c_int64
        library.ember_sdk_responses.argtypes = [ctypes.c_void_p,ctypes.c_size_t]*4
        library.ember_sdk_responses.restype = ctypes.c_int32
    except AttributeError:
        raise RuntimeError('Rust SDK component is outdated; rebuild the engine') from None


def validate_info(value):
    try:
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, UnicodeError):
        raise ValueError('SDK information fields must be bounded strings') from None
    source = ctypes.create_string_buffer(raw)
    try:
        result = library.ember_sdk_info(source,len(raw))
    finally:
        ctypes.memset(ctypes.addressof(source),0,ctypes.sizeof(source))
    if result < 0:
        raise ValueError({-1:'Unknown SDK information field',-2:'SDK information fields must be bounded strings',
                          -3:'SDK access token and game ID are required'}.get(result,'Invalid SDK information'))


class SDKState:
    def __init__(self):
        self.lock = threading.RLock()
        self._library = library
        self._handle = library.ember_sdk_new()
        if not self._handle:
            raise MemoryError('Cannot allocate SDK state')

    def step(self, action, packet=None):
        with self.lock:
            if not self._handle:
                raise ValueError(MESSAGES[-9])
            if packet is not None and (type(packet.packet_id) is not int or not 0 <= packet.packet_id <= 65535):
                raise ValueError(MESSAGES[-8])
            payload = b'' if packet is None else packet.payload
            source = ctypes.create_string_buffer(payload)
            try:
                result = self._library.ember_sdk_step(self._handle,action,0 if packet is None else packet.packet_id,source,len(payload))
            finally:
                ctypes.memset(ctypes.addressof(source),0,ctypes.sizeof(source))
            if result < 0:
                raise ValueError(MESSAGES.get(result,MESSAGES[-9]))
            return result

    def close(self):
        with self.lock:
            if self._handle:
                self._library.ember_sdk_free(self._handle)
                self._handle = None

    def __del__(self):
        if getattr(self,'_handle',None):
            self.close()


def responses(packet, key, info):
    from .protocol import Decoder
    try:
        raw = json.dumps(info,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (ValueError,TypeError,UnicodeError):
        raise ValueError('Cannot construct SDK handshake') from None
    source = ctypes.create_string_buffer(packet.payload)
    secret = ctypes.create_string_buffer(key)
    information = ctypes.create_string_buffer(raw)
    target = ctypes.create_string_buffer(2*65535)
    try:
        count = library.ember_sdk_responses(source,len(packet.payload),secret,len(key),
                                          information,len(raw),target,len(target))
        if count < 0:
            raise ValueError('Cannot construct SDK handshake')
        with Decoder() as decoder:
            packets = decoder.feed(target.raw[:count])
            decoder.finish()
        return packets
    finally:
        for buffer in (source,secret,information,target):
            ctypes.memset(ctypes.addressof(buffer),0,ctypes.sizeof(buffer))
