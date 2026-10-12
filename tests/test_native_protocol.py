"""Compare the Rust framing boundary with the already validated Python wire format."""
from pathlib import Path
import random
import struct
import sys
import unittest
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "tests/oracles/vendor")]
from stove_linux.native_protocol import frame_length
from stove_linux.protocol import Decoder, Packet

class NativeProtocolTests(unittest.TestCase):
    def test_all_header_lengths_match_python_oracle(self):
        for length in range(65536):
            wire = struct.pack("<HH", length, 1000)
            self.assertEqual(frame_length(wire), length if length >= 4 else -1)
    def test_fragmentation_coalescing_and_truncation(self):
        randomizer = random.Random(491)
        expected = [Packet(i, bytes(randomizer.randrange(256) for _ in range(size))) for i,size in [(1000,400),(2001,65531),(1001,0),(99,14)]]
        wire = b"".join(p.encode() for p in expected)
        decoder = Decoder(); result = []
        offset = 0
        while offset < len(wire):
            size = randomizer.randrange(1,1024)
            result += decoder.feed(wire[offset:offset+size]); offset += size
        decoder.finish()
        self.assertEqual(result, expected)
        decoder.feed(b"\x04")
        with self.assertRaises(ValueError): decoder.finish()

    def test_native_codec_is_selected(self):
        from stove_linux.native_protocol import NativeDecoder, new_decoder
        decoder = new_decoder()
        self.assertIsInstance(decoder, NativeDecoder)
        decoder.close()

    def test_every_packet_id_encodes_like_independent_python_oracle(self):
        from stove_linux.python_codec import encode_packet as oracle
        for packet_id in range(65536):
            self.assertEqual(Packet(packet_id,b'').encode(),oracle(packet_id,b''))
        for size in (0,1,255,4096,65531):
            payload = bytes([size % 256]) * size
            self.assertEqual(Packet(2001,payload).encode(),oracle(2001,payload))
        for packet_id, payload in ((-1,b''),(65536,b''),(1,b'x'*65532)):
            with self.assertRaises(ValueError):
                Packet(packet_id,payload).encode()
            with self.assertRaises(ValueError):
                oracle(packet_id,payload)

    def test_random_streams_match_oracle_for_fragmentation_and_coalescing(self):
        from stove_linux.python_codec import Decoder as Oracle, encode_packet
        rng = random.Random(7394)
        for _ in range(30):
            expected = [(rng.randrange(65536),rng.randbytes(rng.choice([0,1,3,40,1000,65531]))) for _ in range(12)]
            wire = b''.join(encode_packet(packet_id,payload) for packet_id,payload in expected)
            oracle = Oracle()
            with Decoder() as decoder:
                actual,reference=[],[]
                offset=0
                while offset<len(wire):
                    size=rng.choice([1,2,3,4,99,65535,len(wire)])
                    chunk=wire[offset:offset+size];offset+=size
                    actual.extend((p.packet_id,p.payload) for p in decoder.feed(chunk))
                    reference.extend(oracle.feed(chunk))
                decoder.finish();oracle.finish()
                self.assertEqual(actual,reference)
                self.assertEqual(actual,expected)

    def test_invalid_stream_and_truncated_frame_recover_like_oracle(self):
        from stove_linux.python_codec import Decoder as Oracle, encode_packet
        for bad_length in range(4):
            with Decoder() as decoder:
                oracle=Oracle()
                wire=encode_packet(1001,b'')+struct.pack('<HH',bad_length,1)
                with self.assertRaises(ValueError):decoder.feed(wire)
                with self.assertRaises(ValueError):oracle.feed(wire)
                self.assertEqual([(p.packet_id,p.payload) for p in decoder.feed(encode_packet(9,b'ok'))],oracle.feed(encode_packet(9,b'ok')))
                decoder.finish();oracle.finish()
        with Decoder() as decoder:
            wire=encode_packet(7,b'payload')
            decoder.feed(wire[:-1])
            with self.assertRaises(ValueError):decoder.finish()
            self.assertEqual(decoder.feed(wire[-1:]),[Packet(7,b'payload')])
            decoder.finish()

    def test_callback_failure_propagates_and_decoder_can_be_reused(self):
        from unittest.mock import patch
        from stove_linux.native_protocol import NativeDecoder
        decoder=NativeDecoder()
        try:
            with patch('stove_linux.native_protocol.ctypes.string_at',side_effect=RuntimeError('synthetic delivery error')):
                with self.assertRaisesRegex(RuntimeError,'synthetic delivery error'):
                    decoder.feed(Packet(1001,b'').encode())
            self.assertEqual(decoder.feed(Packet(8,b'next').encode()),[(8,b'next')])
        finally:decoder.close()
        decoder.close()
        with self.assertRaises(ValueError):decoder.feed(b'')
        with self.assertRaises(ValueError):decoder.finish()
