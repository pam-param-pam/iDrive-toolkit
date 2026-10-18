"""Smooth displayed throughput independently of polling cadence."""
import math
import time


class TransferSpeed:
    TIME_CONSTANT_SECONDS = 8.0

    def __init__(self, transferred=0, *, now=None):
        self.reset(transferred, now=now)

    def reset(self, transferred=0, *, now=None):
        self._bytes = transferred
        self._time = time.monotonic() if now is None else now
        self.speed = 0.0

    def update(self, transferred, *, now=None):
        now = time.monotonic() if now is None else now
        if transferred < self._bytes:
            self.reset(transferred, now=now)
            return self.speed
        elapsed = now - self._time
        if elapsed <= 0:
            return self.speed
        measured = (transferred - self._bytes) / elapsed
        weight = -math.expm1(-elapsed / self.TIME_CONSTANT_SECONDS)
        self.speed += weight * (measured - self.speed)
        self._bytes, self._time = transferred, now
        return self.speed
