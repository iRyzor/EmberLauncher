"""Bounded anonymous-pipe reads on Windows, Linux and macOS."""

import os
import queue
import threading
import time


class PipeReader:
    def __init__(self, stream, chunk_size=65535):
        self.stream = stream
        self.chunk_size = chunk_size
        self.queue = queue.Queue(maxsize=32)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        while not self.stop.is_set():
            try:
                data = os.read(self.stream.fileno(), self.chunk_size)
            except (OSError, ValueError):
                data = b""
            while not self.stop.is_set():
                try:
                    self.queue.put(data, timeout=0.2)
                    break
                except queue.Full:
                    continue
            if not data:
                return

    def read(self, timeout=0.5):
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self.stop.set()
        self.thread.join(timeout=1)


def write_windows(stream, frame, stop, timeout=5):
    done = threading.Event()
    failed = []

    def write():
        try:
            offset = 0
            while offset < len(frame):
                offset += os.write(stream.fileno(), frame[offset:])
        except (OSError, ValueError):
            failed.append(True)
        finally:
            done.set()

    worker = threading.Thread(target=write, daemon=True)
    worker.start()
    deadline = time.monotonic() + timeout
    while not done.wait(0.1):
        if stop.is_set() or time.monotonic() >= deadline:
            raise RuntimeError("Bridge write timed out")
    if failed:
        raise RuntimeError("Bridge unavailable")
