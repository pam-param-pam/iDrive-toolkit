"""Observe and control engines without owning their queues or completion policy."""
from __future__ import annotations

import threading

from .history import TransferHistory
from .progress import TransferProgress, TransferProgressDirection, TransferProgressPhase
from ..utils.transfer.speed import TransferSpeed


class SyncTransferCancelled(RuntimeError):
    pass


class TransferMonitor:
    def __init__(self):
        self.history = TransferHistory()
        self._callback = None
        self._lock = threading.RLock()
        self._observed = {}
        self._last = (None, None)
        self._cancel_requested = threading.Event()

    def set_progress_callback(self, callback):
        self._callback = callback

    def observe(self, transfer, direction):
        self.history.add(transfer)
        with self._lock:
            if id(transfer) in self._observed:
                return
            if not self._observed:
                self._cancel_requested.clear()
            stop = threading.Event()
            watcher = threading.Thread(target=self._watch, args=(transfer, direction, stop),
                                       name=f"sync-{direction.value}-progress", daemon=True)
            self._observed[id(transfer)] = (transfer, direction, stop, watcher)
            self._last = (direction, transfer)
            watcher.start()

    def _watch(self, transfer, direction, stop):
        meter = TransferSpeed(transfer.get_transferred_bytes())
        while not stop.wait(0.25):
            current = transfer.get_transferred_bytes()
            if not transfer.ctx.global_pause.is_set():
                meter.reset(current)
                speed = 0.0
            else:
                speed = meter.update(current)
            callback = self._callback
            if callback is None:
                continue
            if direction == TransferProgressDirection.UPLOAD:
                total_bytes, current_bytes = transfer.ctx.get_sizes()
            else:
                current_bytes, total_bytes = transfer.get_progress()
            states = list(transfer.ctx.get_all_states().values())
            failed = sum(state.status.value in ("failed", "save_failed") for state in states)
            completed = sum(state.status.value in ("completed", "failed", "save_failed", "aborted") for state in states)
            cancelling = self._cancel_requested.is_set()
            callback(TransferProgress(
                direction=direction,
                phase=TransferProgressPhase.CANCELLING if cancelling else TransferProgressPhase.RUNNING,
                message=f"{'Uploading' if direction == TransferProgressDirection.UPLOAD else 'Downloading'} files",
                current_bytes=current_bytes, total_bytes=total_bytes,
                completed_items=completed, total_items=len(states), failed_items=failed,
                bytes_per_second=speed,
                eta_seconds=max(0, total_bytes - current_bytes) / speed if speed > 0 else None,
            ))

    def stop_observing(self, transfer):
        with self._lock:
            entry = self._observed.pop(id(transfer), None)
        if entry is not None:
            _, _, stop, watcher = entry
            stop.set()
            if watcher is not threading.current_thread():
                watcher.join()

    def _active(self):
        with self._lock:
            return [entry[0] for entry in self._observed.values()]

    def abort_current_transfer(self):
        self._cancel_requested.set()
        for transfer in self._active():
            # Only the operation owner shuts engines down. Signalling cancellation
            # here avoids concurrent shutdown threads draining the same queues.
            transfer.ctx.stop_requested.set()
            transfer.ctx.global_pause.set()

    def pause_current_transfer(self):
        active = self._active()
        for transfer in active:
            transfer.pause_all()
        return bool(active)

    def resume_current_transfer(self):
        active = self._active()
        for transfer in active:
            transfer.resume_all()
        return bool(active)

    def get_active_transfer(self):
        with self._lock:
            if not self._observed:
                return None, None
            transfer, direction, _, _ = next(reversed(self._observed.values()))
            return direction.value, transfer

    def get_current_transfer(self):
        with self._lock:
            if self._observed:
                return self.get_active_transfer()
            direction, transfer = self._last
            return direction.value if direction is not None else None, transfer
