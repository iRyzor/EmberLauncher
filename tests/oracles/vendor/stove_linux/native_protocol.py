"""Incremental Rust migration; source development retains the Python oracle."""
import ctypes
from pathlib import Path
import sys
import threading
from . import python_codec

name = {"win32": "ember_protocol.dll", "darwin": "libember_protocol.dylib"}.get(sys.platform, "libember_protocol.so")
base = Path(__file__).resolve().parents[2]
path = base / "runtime" / name
if path.is_file():
    library = ctypes.CDLL(str(path))
    library.ember_frame_header.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    library.ember_frame_header.restype = ctypes.c_int32
else:
    library = None

def frame_length(header):
    if library is None:
        if len(header) < 4:
            return 0
        length = int.from_bytes(header[:2], "little")
        return length if length >= 4 else -1
    buffer = ctypes.create_string_buffer(bytes(header[:4]))
    return library.ember_frame_header(buffer, min(len(header), 4))

# Stateful codec ABI is checked explicitly; a stale library must be rebuilt.

Sink = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint16, ctypes.c_void_p, ctypes.c_size_t)
if library is not None:
    try:
        library.ember_codec_abi.argtypes = []
        library.ember_codec_abi.restype = ctypes.c_uint32
        if library.ember_codec_abi() != 1:
            raise RuntimeError('Unsupported Rust codec ABI')
        library.ember_decoder_new.argtypes = []
        library.ember_decoder_new.restype = ctypes.c_void_p
        library.ember_decoder_free.argtypes = [ctypes.c_void_p]
        library.ember_decoder_free.restype = None
        library.ember_decoder_feed.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, Sink, ctypes.c_void_p]
        library.ember_decoder_feed.restype = ctypes.c_int32
        library.ember_decoder_finish.argtypes = [ctypes.c_void_p]
        library.ember_decoder_finish.restype = ctypes.c_int32
        library.ember_packet_encode.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        library.ember_packet_encode.restype = ctypes.c_int32
    except AttributeError:
        raise RuntimeError('Rust codec component is outdated; rebuild the engine') from None


def encode_packet(packet_id, payload):
    if library is None:
        return python_codec.encode_packet(packet_id, payload)
    length = 4 + len(payload)
    if not 4 <= length <= 65535:
        raise ValueError('Packet exceeds the 16-bit length limit')
    if not isinstance(packet_id, int) or not 0 <= packet_id <= 65535:
        raise ValueError('Invalid packet ID')
    source = ctypes.create_string_buffer(payload)
    output = ctypes.create_string_buffer(length)
    result = library.ember_packet_encode(packet_id, source, len(payload), output, length)
    if result != length:
        raise ValueError('Native IPC encoding failed')
    return output.raw[:length]


class NativeDecoder:
    def __init__(self):
        self._lock = threading.RLock()
        self._library = library
        self._handle = self._library.ember_decoder_new()
        if not self._handle:
            raise MemoryError('Cannot allocate IPC decoder')

    def feed(self, data):
        with self._lock:
            if not self._handle:
                raise ValueError('IPC decoder is closed')
            packets, failures = [], []
            @Sink
            def receive(_, packet_id, payload, size):
                try:
                    packets.append((packet_id, ctypes.string_at(payload, size)))
                    return 0
                except BaseException as error:
                    failures.append(error)
                    return 1
            source = ctypes.create_string_buffer(bytes(data))
            result = self._library.ember_decoder_feed(self._handle, source, len(data), receive, None)
            if failures:
                raise failures[0]
            if result != 0:
                raise ValueError('Malformed IPC frame length' if result == -1 else 'Native IPC decoding failed')
            return packets

    def finish(self):
        with self._lock:
            if not self._handle:
                raise ValueError('IPC decoder is closed')
            if self._library.ember_decoder_finish(self._handle) != 0:
                raise ValueError('Truncated IPC frame')

    def close(self):
        with self._lock:
            if self._handle:
                self._library.ember_decoder_free(self._handle)
                self._handle = None

    def __del__(self):
        if getattr(self, '_handle', None):
            self.close()


def new_decoder():
    return NativeDecoder() if library is not None else python_codec.Decoder()
