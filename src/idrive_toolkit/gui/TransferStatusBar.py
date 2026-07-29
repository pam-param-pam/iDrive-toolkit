from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from ..syncer.progress import TransferProgress, TransferProgressPhase


class TransferStatusBar:
    SPEED_SMOOTHING = 0.12
    POLL_MS = 500

    def __init__(
        self,
        parent: tk.Misc,
        *,
        abort_icon: tk.PhotoImage | None,
        abort_command: Callable[[], None],
        pause_icon: tk.PhotoImage | None = None,
        resume_icon: tk.PhotoImage | None = None,
        pause_command: Callable[[], None] | None = None,
    ):
        self.parent = parent
        self.status_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0)
        self._transfer: Any = None
        self._direction: str | None = None
        self._last_bytes = 0
        self._last_time = 0.0
        self._speed = 0.0
        self._aborting = False
        self._paused = False
        self._paused_status = None
        self._pause_icon = pause_icon
        self._resume_icon = resume_icon
        self._pause_command = pause_command

        ttk.Label(parent, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(parent, variable=self.progress_var, maximum=100, mode="determinate")
        self.progress_bar.grid(row=0, column=1, sticky="ew", padx=(10, 6))
        parent.columnconfigure(1, weight=1)
        self.pause_button = None
        abort_column = 2
        if pause_command is not None:
            self.pause_button = ttk.Button(
                parent,
                text="Pause",
                image=pause_icon,
                compound="left",
                command=pause_command,
                state="disabled",
            )
            self.pause_button.grid(row=0, column=2, sticky="e", padx=(0, 6))
            abort_column = 3
        self.abort_button = ttk.Button(
            parent,
            text="Abort",
            image=abort_icon,
            compound="left",
            command=abort_command,
            state="disabled",
        )
        self.abort_button.grid(row=0, column=abort_column, sticky="e")

    def set_status(self, status: str) -> None:
        self.status_var.set(status)

    def set_progress(self, current: int | None, total: int | None) -> None:
        self.progress_var.set(self.progress_percent(current, total))

    def set_aborting(self) -> None:
        self._aborting = True
        self.status_var.set("Aborting transfer...")
        self.set_pause_enabled(False)
        self.set_abort_enabled(False)

    def reset(self, status: str | None = None) -> None:
        self._aborting = False
        if status is not None:
            self.status_var.set(status)
        self.progress_var.set(0)
        self.abort_button.configure(state="disabled")
        self.set_paused(False)
        self.set_pause_enabled(False)
        self._transfer = None
        self._direction = None
        self._last_bytes = 0
        self._last_time = 0.0
        self._speed = 0.0

    def set_abort_enabled(self, enabled: bool) -> None:
        self.abort_button.configure(state="normal" if enabled else "disabled")

    def set_pause_enabled(self, enabled: bool) -> None:
        if self.pause_button is not None:
            self.pause_button.configure(state="normal" if enabled else "disabled")

    def set_paused(self, paused: bool) -> None:
        if self._paused != paused:
            self._paused_status = None
        self._paused = paused
        if self.pause_button is None:
            return
        self.pause_button.configure(
            text="Resume" if paused else "Pause",
            image=self._resume_icon if paused else self._pause_icon,
        )

    def apply_sync_progress(self, progress: TransferProgress) -> None:
        if self._aborting:
            self.set_abort_enabled(False)
            self.set_pause_enabled(False)
            return
        if self._transfer is not None and self.is_transfer_paused(self._transfer):
            self.set_paused(True)
            self.set_progress(progress.current_bytes, progress.total_bytes)
            if self._paused_status is None:
                self._paused_status = self.format_sync_progress(
                    TransferProgress(
                        direction=progress.direction,
                        phase=progress.phase,
                        message="Paused files",
                        current_bytes=progress.current_bytes,
                        total_bytes=progress.total_bytes,
                        completed_items=progress.completed_items,
                        total_items=progress.total_items,
                        failed_items=progress.failed_items,
                        bytes_per_second=0.0,
                        eta_seconds=None,
                    )
                )
            self.status_var.set(self._paused_status)
            self.set_abort_enabled(True)
            self.set_pause_enabled(True)
            return
        self.status_var.set(self.format_sync_progress(progress))
        self.set_progress(progress.current_bytes, progress.total_bytes)
        self.set_abort_enabled(progress.phase == TransferProgressPhase.RUNNING)
        self.set_pause_enabled(progress.phase == TransferProgressPhase.RUNNING)

    def attach_transfer(self, direction: str, transfer: Any) -> None:
        self._aborting = False
        self._transfer = transfer
        self._direction = direction
        current_bytes, _total_bytes = self.transfer_bytes(transfer, direction)
        self._last_bytes = current_bytes
        self._last_time = time.monotonic()
        self._speed = 0.0
        self._paused_status = None
        self.set_paused(False)
        self.parent.after(0, self.poll_transfer)

    def clear_transfer(self) -> None:
        self._transfer = None
        self._direction = None
        self.set_abort_enabled(False)
        self.set_pause_enabled(False)

    def poll_transfer(self) -> None:
        transfer = self._transfer
        direction = self._direction
        if transfer is None or direction is None:
            self.set_abort_enabled(False)
            return

        current_bytes, total_bytes = self.transfer_bytes(transfer, direction)
        now = time.monotonic()
        paused = self.is_transfer_paused(transfer)

        if paused:
            self.set_paused(True)
            self.set_progress(current_bytes, total_bytes)
            if self._paused_status is None:
                self._paused_status = self.format_polled_transfer(
                    direction,
                    transfer,
                    current_bytes,
                    total_bytes,
                    self._speed,
                    message="Paused files",
                )
            self.status_var.set(self._paused_status)
            self.set_abort_enabled(not self._aborting)
            self.set_pause_enabled(not self._aborting)
            self._last_bytes = current_bytes
            self._last_time = now
            self.parent.after(self.POLL_MS, self.poll_transfer)
            return

        self.set_paused(False)
        elapsed = max(0.001, now - self._last_time)
        instant_speed = max(0.0, (current_bytes - self._last_bytes) / elapsed)
        self._speed = instant_speed if self._speed <= 0 else (self.SPEED_SMOOTHING * instant_speed + (1 - self.SPEED_SMOOTHING) * self._speed)
        self._last_bytes = current_bytes
        self._last_time = now

        self.set_progress(current_bytes, total_bytes)
        if not self._aborting:
            self.status_var.set(self.format_polled_transfer(direction, transfer, current_bytes, total_bytes, self._speed))
            self.set_abort_enabled(True)
            self.set_pause_enabled(True)
        else:
            self.set_abort_enabled(False)
            self.set_pause_enabled(False)
        self.parent.after(self.POLL_MS, self.poll_transfer)

    @staticmethod
    def transfer_bytes(transfer: Any, direction: str) -> tuple[int, int]:
        if direction == "upload":
            total_bytes, current_bytes = transfer.ctx.get_sizes()
            return current_bytes, total_bytes
        return transfer.get_progress()

    def format_sync_progress(self, progress: TransferProgress) -> str:
        return self._format_transfer(
            progress.message,
            progress.current_bytes,
            progress.total_bytes,
            progress.bytes_per_second,
            progress.completed_items,
            progress.total_items,
            progress.failed_items,
            progress.eta_seconds,
        )

    def format_polled_transfer(self, direction: str, transfer: Any, current_bytes: int, total_bytes: int, speed: float, message: str | None = None) -> str:
        verb = "Uploading" if direction == "upload" else "Downloading"
        completed_items, total_items, failed_items, aborted_items = self.transfer_item_counts(transfer)
        eta = (total_bytes - current_bytes) / speed if total_bytes > 0 and speed > 0 else None
        return self._format_transfer(message or f"{verb} files", current_bytes, total_bytes, speed, completed_items, total_items, failed_items, eta, aborted_items)

    @staticmethod
    def is_transfer_paused(transfer: Any) -> bool:
        ctx = getattr(transfer, "ctx", None)
        is_paused = getattr(ctx, "is_paused", None)
        return bool(is_paused()) if callable(is_paused) else False

    @staticmethod
    def transfer_item_counts(transfer: Any) -> tuple[int, int, int, int]:
        states = transfer.ctx.get_all_states()
        total_items = len(states)
        completed = 0
        failed = 0
        aborted = 0
        for state in states.values():
            status = getattr(state, "status", None)
            status_value = getattr(status, "value", status)
            if status_value in ("completed", "failed", "save_failed", "aborted"):
                completed += 1
            if status_value in ("failed", "save_failed"):
                failed += 1
            if status_value == "aborted":
                aborted += 1
        return completed, total_items, failed, aborted

    def _format_transfer(
        self,
        message: str,
        current_bytes: int,
        total_bytes: int,
        speed: float,
        completed_items: int,
        total_items: int,
        failed_items: int,
        eta_seconds: float | None,
        aborted_items: int = 0,
    ) -> str:
        byte_part = f"{self.format_bytes(current_bytes)}/{self.format_bytes(total_bytes)}"
        speed_part = f"{self.format_bytes(int(speed))}/s"
        eta_part = self.format_eta(eta_seconds)
        transfer_part = f"{byte_part}, {speed_part}"
        if eta_part:
            transfer_part = f"{transfer_part}, ETA {eta_part}"
        if total_items:
            file_part = f"{completed_items} files/{total_items}"
            if failed_items:
                file_part = f"{file_part}, failed={failed_items}"
            if aborted_items:
                file_part = f"{file_part}, aborted={aborted_items}"
            return f"{message} ({transfer_part}, {file_part})"
        return f"{message} ({transfer_part})"

    @staticmethod
    def progress_percent(current: int | None, total: int | None) -> float:
        if current is None or total is None or total <= 0:
            return 0.0
        return min(100.0, max(0.0, current * 100.0 / total))

    @staticmethod
    def format_bytes(value: int) -> str:
        size = float(value)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if size < 1024 or unit == "TiB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} PiB"

    @staticmethod
    def format_eta(seconds: float | None) -> str:
        if seconds is None:
            return ""
        seconds = max(0, int(seconds))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"
