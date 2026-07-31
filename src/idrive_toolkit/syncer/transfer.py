from __future__ import annotations

import threading
import time
from typing import Callable

from .progress import (
    TransferProgress,
    TransferProgressCallback,
    TransferProgressDirection,
    TransferProgressPhase,
)
from ..transfer_errors import raise_transfer_errors


class SyncTransferCancelled(RuntimeError):
    pass


class TransferMonitor:
    TRANSFER_SPEED_SMOOTHING = 0.12

    def __init__(self):
        self._transfer_progress_callback: TransferProgressCallback | None = None
        self._active_transfer = None
        self._active_transfer_direction: TransferProgressDirection | None = None
        self._last_transfer = None
        self._last_transfer_direction: TransferProgressDirection | None = None
        self._active_transfer_cancel_thread: threading.Thread | None = None
        self._active_transfer_lock = threading.Lock()
        self._transfer_cancel_requested = threading.Event()

    def set_progress_callback(self, callback: TransferProgressCallback | None) -> None:
        self._transfer_progress_callback = callback

    def abort_current_transfer(self) -> None:
        self._transfer_cancel_requested.set()
        with self._active_transfer_lock:
            active_transfer = self._active_transfer
        if active_transfer is not None:
            cancel_thread = threading.Thread(
                target=lambda: active_transfer.shutdown(cancel_pending=True),
                daemon=True,
            )
            with self._active_transfer_lock:
                if self._active_transfer is active_transfer and self._active_transfer_cancel_thread is None:
                    self._active_transfer_cancel_thread = cancel_thread
                    cancel_thread.start()

    def pause_current_transfer(self) -> bool:
        with self._active_transfer_lock:
            active_transfer = self._active_transfer
        if active_transfer is None:
            return False
        pause = getattr(active_transfer, "pause_all", None)
        if not callable(pause):
            return False
        pause()
        return True

    def resume_current_transfer(self) -> bool:
        with self._active_transfer_lock:
            active_transfer = self._active_transfer
        if active_transfer is None:
            return False
        resume = getattr(active_transfer, "resume_all", None)
        if not callable(resume):
            return False
        resume()
        return True

    def get_current_transfer(self) -> tuple[str | None, object | None]:
        with self._active_transfer_lock:
            direction = self._active_transfer_direction or self._last_transfer_direction
            transfer = self._active_transfer if self._active_transfer is not None else self._last_transfer
            return direction.value if direction is not None else None, transfer

    def get_active_transfer(self) -> tuple[str | None, object | None]:
        with self._active_transfer_lock:
            direction = self._active_transfer_direction
            return direction.value if direction is not None else None, self._active_transfer

    def join(self, transfer, direction: TransferProgressDirection) -> None:
        error: list[BaseException] = []
        cancelled = False
        last_bytes = 0
        last_time = time.monotonic()
        smoothed_speed = 0.0

        with self._active_transfer_lock:
            self._active_transfer = transfer
            self._active_transfer_direction = direction
            self._last_transfer = transfer
            self._last_transfer_direction = direction
            self._active_transfer_cancel_thread = None
        self._transfer_cancel_requested.clear()

        def wait_for_transfer() -> None:
            try:
                transfer.join()
            except BaseException as exc:
                error.append(exc)

        waiter = threading.Thread(target=wait_for_transfer, daemon=True)
        waiter.start()
        self._emit_transfer_progress(transfer, direction, TransferProgressPhase.RUNNING)

        while waiter.is_alive():
            waiter.join(timeout=0.25)
            current_bytes, last_bytes, last_time, smoothed_speed = self._sample_transfer_speed(
                transfer,
                direction,
                last_bytes,
                last_time,
                smoothed_speed,
            )
            if self._transfer_cancel_requested.is_set() and not cancelled:
                cancelled = True
                self._emit_transfer_progress(
                    transfer,
                    direction,
                    TransferProgressPhase.CANCELLING,
                    current_bytes=current_bytes,
                    bytes_per_second=smoothed_speed,
                )
                continue

            phase = TransferProgressPhase.CANCELLING if cancelled else TransferProgressPhase.RUNNING
            self._emit_transfer_progress(
                transfer,
                direction,
                phase,
                current_bytes=current_bytes,
                bytes_per_second=smoothed_speed,
            )

        try:
            if error:
                raise error[0]

            if cancelled:
                self._wait_for_cancel_shutdown_thread()
                self._emit_transfer_progress(transfer, direction, TransferProgressPhase.CANCELLED)
                raise SyncTransferCancelled(f"{direction.value.capitalize()} aborted")

            raise_transfer_errors(transfer, direction.value.capitalize())
            self._emit_transfer_progress(transfer, direction, TransferProgressPhase.COMPLETE)
        finally:
            with self._active_transfer_lock:
                if self._active_transfer is transfer:
                    self._active_transfer = None
                    self._active_transfer_direction = None
                    self._active_transfer_cancel_thread = None
            self._transfer_cancel_requested.clear()

    def _wait_for_cancel_shutdown_thread(self) -> None:
        with self._active_transfer_lock:
            cancel_thread = self._active_transfer_cancel_thread
        if cancel_thread is not None and cancel_thread is not threading.current_thread():
            cancel_thread.join()

    def _emit_transfer_progress(
        self,
        transfer,
        direction: TransferProgressDirection,
        phase: TransferProgressPhase,
        current_bytes: int | None = None,
        bytes_per_second: float = 0.0,
    ) -> None:
        callback: Callable[[TransferProgress], None] | None = self._transfer_progress_callback
        if callback is None:
            return

        measured_current_bytes, total_bytes = self._transfer_byte_progress(transfer, direction)
        if current_bytes is None:
            current_bytes = measured_current_bytes
        eta_seconds = self._transfer_eta_seconds(current_bytes, total_bytes, bytes_per_second)
        completed_items, total_items, failed_items = self._transfer_item_progress(transfer)
        verb = "Uploading" if direction == TransferProgressDirection.UPLOAD else "Downloading"
        message = f"{verb} files"
        if phase == TransferProgressPhase.CANCELLING:
            message = f"Aborting {direction.value}"
        if phase == TransferProgressPhase.CANCELLED:
            message = f"{direction.value.capitalize()} aborted"
        if phase == TransferProgressPhase.COMPLETE:
            message = f"{verb} complete"

        callback(
            TransferProgress(
                direction=direction,
                phase=phase,
                message=message,
                current_bytes=current_bytes,
                total_bytes=total_bytes,
                completed_items=completed_items,
                total_items=total_items,
                failed_items=failed_items,
                bytes_per_second=bytes_per_second,
                eta_seconds=eta_seconds,
            )
        )

    def _sample_transfer_speed(
        self,
        transfer,
        direction: TransferProgressDirection,
        last_bytes: int,
        last_time: float,
        smoothed_speed: float,
    ) -> tuple[int, int, float, float]:
        current_bytes, _ = self._transfer_byte_progress(transfer, direction)
        current_time = time.monotonic()
        elapsed = current_time - last_time
        if elapsed <= 0:
            return current_bytes, last_bytes, last_time, smoothed_speed

        instant_speed = max(0.0, (current_bytes - last_bytes) / elapsed)
        if instant_speed > 0:
            if smoothed_speed <= 0:
                smoothed_speed = instant_speed
            else:
                alpha = self.TRANSFER_SPEED_SMOOTHING
                smoothed_speed = alpha * instant_speed + (1.0 - alpha) * smoothed_speed
        return current_bytes, current_bytes, current_time, smoothed_speed

    def _transfer_eta_seconds(self, current_bytes: int, total_bytes: int, bytes_per_second: float) -> float | None:
        if total_bytes <= 0 or bytes_per_second <= 0:
            return None
        remaining = max(0, total_bytes - current_bytes)
        return remaining / bytes_per_second

    def _transfer_byte_progress(self, transfer, direction: TransferProgressDirection) -> tuple[int, int]:
        if direction == TransferProgressDirection.UPLOAD:
            total_bytes, current_bytes = transfer.ctx.get_sizes()
            return current_bytes, total_bytes
        return transfer.get_progress()

    def _transfer_item_progress(self, transfer) -> tuple[int, int, int]:
        states = transfer.ctx.get_all_states()
        total_items = len(states)
        completed_items = 0
        failed_items = 0

        for state in states.values():
            status = getattr(state, "status", None)
            status_value = getattr(status, "value", status)
            if status_value in ("completed", "failed", "save_failed", "aborted"):
                completed_items += 1
            if status_value in ("failed", "save_failed"):
                failed_items += 1

        return completed_items, total_items, failed_items
