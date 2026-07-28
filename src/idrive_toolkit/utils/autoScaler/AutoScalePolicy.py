import time
from dataclasses import dataclass, replace
from typing import Optional


@dataclass
class AutoScalePolicy:
    # ---- scale step sizes ----
    scale_up_step: int
    scale_down_step: int

    # ---- observation windows ----
    scale_up_window: int      # consecutive positive samples
    scale_down_window: int      # consecutive plateau samples

    # ---- thresholds ----
    up_improvement_factor: float   # +10%
    plateau_factor: float           # <2%

    # ---- hard throttle behavior ----
    hard_error_grace: int         # tolerate N error ticks
    hard_error_cooldown: float  # seconds after reacting

    # ---- cooldowns ----
    scale_up_cooldown: float
    scale_down_cooldown: float

    hard_error_backoff: int = 15

    min_workers: int = 1
    max_workers: int = 1
    initial_workers: Optional[int] = None

    # ---- internals (stateful) ----
    _hard_error_ticks: int = 0
    _last_hard_react: float = 0.0

    def with_bounds(
        self,
        *,
        min_workers: int,
        max_workers: int,
        initial_workers: Optional[int] = None,
    ) -> "AutoScalePolicy":
        if max_workers < min_workers:
            max_workers = min_workers

        if initial_workers is None:
            initial_workers = self.initial_workers

        if initial_workers is not None:
            initial_workers = max(min_workers, min(initial_workers, max_workers))

        return replace(
            self,
            min_workers=min_workers,
            max_workers=max_workers,
            initial_workers=initial_workers,
        )

    def get_initial_workers(self) -> int:
        if self.initial_workers is None:
            return self.min_workers
        return max(self.min_workers, min(self.initial_workers, self.max_workers))

    def should_react_to_hard_errors(self, hard_errors: int) -> bool:
        if hard_errors <= 0:
            self._hard_error_ticks = 0
            return False

        self._hard_error_ticks += 1
        now = time.time()

        if self._hard_error_ticks < self.hard_error_grace:
            return False

        if now - self._last_hard_react < self.hard_error_cooldown:
            return False

        self._last_hard_react = now
        self._hard_error_ticks = 0
        return True
