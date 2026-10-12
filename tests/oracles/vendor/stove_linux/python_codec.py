"""Independent Python framing oracle, retained for migration comparison and source use."""
import struct


def encode_packet(packet_id, payload):
    length = 4 + len(payload)
    if not 4 <= length <= 65535:
        raise ValueError('Packet exceeds the 16-bit length limit')
    if not isinstance(packet_id, int) or not 0 <= packet_id <= 65535:
        raise ValueError('Invalid packet ID')
    return struct.pack('<HH', length, packet_id) + payload


class Decoder:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        packets = []
        while len(self.buffer) >= 4:
            length, packet_id = struct.unpack('<HH', self.buffer[:4])
            if length < 4:
                self.buffer.clear()
                raise ValueError('Malformed IPC frame length')
            if len(self.buffer) < length:
                break
            packets.append((packet_id, bytes(self.buffer[4:length])))
            del self.buffer[:length]
        return packets

    def finish(self):
        if self.buffer:
            raise ValueError('Truncated IPC frame')

    def close(self):
        self.buffer[:] = b'\0' * len(self.buffer)
        self.buffer.clear()
