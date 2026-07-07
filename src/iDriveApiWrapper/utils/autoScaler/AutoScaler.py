import threading
import time
import logging
from typing import Any, Callable, Optional

from ...utils.autoScaler.AutoScalePolicy import AutoScalePolicy

logger = logging.getLogger("iDrive")


class AutoScaler:
    _SAMPLE_INTERVAL = 1.5

    def __init__(self, throttle_state: Any, policy: AutoScalePolicy):
        self.throttle_state = throttle_state
        self.policy = policy

        self.current = policy.min_workers
        self.lock = threading.Lock()
        self.stop_flag = False
        self._stop_event = threading.Event()

        # throughput tracking
        self._last_rate = 0.0
        self._best_rate = 0.0
        self._best_rate_since_scale = 0.0
        self._healthy_steps = 0
        self._low_rate_steps = 0
        self._samples_since_scale = 0
        self._probe_reference_rate: Optional[float] = None

        # cooldown tracking
        self._last_scale_up_time = 0.0
        self._last_scale_down_time = 0.0

        self._scale_up_cooldown = policy.scale_up_cooldown
        self._scale_down_cooldown = policy.scale_down_cooldown

        self._backoff_until = 0.0
        self._backoff_duration = policy.hard_error_backoff

        self._pause_event = threading.Event()
        self._pause_event.set()  # running by default

    # -------------------------------
    # Worker count management
    # -------------------------------

    def _increase_workers(self, spawn_func: Callable[[], object]) -> int:
        step = self.policy.scale_up_step
        added = 0
        for _ in range(step):
            if self.current >= self.policy.max_workers:
                logger.info(f"[AutoScaler] Wanted scale UP but already at max={self.policy.max_workers}")
                break

            try:
                result = spawn_func()
            except Exception:
                logger.exception("[AutoScaler] Failed to spawn worker")
                break

            if result is False:
                logger.info("[AutoScaler] Spawn callback declined scale UP")
                break

            self.current += 1
            added += 1

        if added:
            self._last_scale_up_time = time.time()
            logger.info(f"[AutoScaler] Scaled UP -> workers={self.current}")

        return added

    def _decrease_workers(self, kill_func: Callable[[], object]) -> int:
        step = self.policy.scale_down_step
        removed = 0
        for _ in range(step):
            if self.current <= self.policy.min_workers:
                logger.info(f"[AutoScaler] Wanted scale DOWN but already at min={self.policy.min_workers}")
                break

            try:
                result = kill_func()
            except Exception:
                logger.exception("[AutoScaler] Failed to stop worker")
                break

            if result is False:
                logger.info("[AutoScaler] Stop callback declined scale DOWN")
                break

            self.current -= 1
            removed += 1

        if removed:
            self._last_scale_down_time = time.time()
            logger.info(f"[AutoScaler] Scaled DOWN -> workers={self.current}")

        return removed

    # -------------------------------
    # Autoscaling loop
    # -------------------------------

    def start(self, spawn_func, kill_func):
        t = threading.Thread(target=self._loop, args=(spawn_func, kill_func), daemon=True)
        t.start()
        return t

    def _loop(self, spawn_func, kill_func):
        logger.info("[AutoScaler] Started autoscaling loop")

        while not self._stop_event.is_set():
            if not self._pause_event.wait(timeout=0.5):
                continue

            if self._stop_event.wait(self._SAMPLE_INTERVAL):
                break
            if not self._pause_event.is_set():
                continue

            now = time.time()

            hard_errors = self.throttle_state.error_rate()
            rate = self.throttle_state.bytes_rate()

            can_scale_up = (now - self._last_scale_up_time) >= self._scale_up_cooldown
            can_scale_down = (now - self._last_scale_down_time) >= self._scale_down_cooldown

            with self.lock:
                if now < self._backoff_until:
                    self._last_rate = rate
                    continue

                if self.policy.should_react_to_hard_errors(hard_errors):
                    logger.warning(f"[AutoScaler] Hard throttling ({hard_errors}) -> entering backoff")

                    if can_scale_down:
                        self._decrease_workers(kill_func)

                    self._backoff_until = now + self._backoff_duration
                    self._reset_trend(rate)
                    continue

                self._observe_rate(rate)

                if self._should_roll_back_probe() and can_scale_down:
                    logger.info(
                        "[AutoScaler] Scale-up probe did not improve throughput enough "
                        f"(reference={self._probe_reference_rate:.1f}, best={self._best_rate_since_scale:.1f})"
                    )
                    if self._decrease_workers(kill_func):
                        self._reset_trend(rate)
                    continue

                if (
                    self._probe_reference_rate is None
                    and self._low_rate_steps >= self.policy.scale_down_window
                    and self.current > self.policy.min_workers
                    and can_scale_down
                ):
                    logger.info(
                        f"[AutoScaler] Throughput fell below best rate -> scale DOWN "
                        f"(rate={rate:.1f}, best={self._best_rate:.1f})"
                    )
                    if self._decrease_workers(kill_func):
                        self._reset_trend(rate)
                    continue

                if self._should_accept_probe():
                    logger.info(
                        "[AutoScaler] Scale-up probe accepted "
                        f"(reference={self._probe_reference_rate:.1f}, best={self._best_rate_since_scale:.1f})"
                    )
                    self._best_rate = max(self._best_rate, self._best_rate_since_scale)
                    self._probe_reference_rate = None
                    self._best_rate_since_scale = 0.0
                    self._samples_since_scale = 0
                    self._healthy_steps = 0

                if (
                    self._probe_reference_rate is None
                    and self._healthy_steps >= self.policy.scale_up_window
                    and rate > 0
                    and self.current < self.policy.max_workers
                    and can_scale_up
                ):
                    reference_rate = max(rate, self._best_rate)
                    logger.info(
                        f"[AutoScaler] Throughput is healthy -> probing scale UP "
                        f"(rate={rate:.1f}, best={self._best_rate:.1f})"
                    )
                    if self._increase_workers(spawn_func):
                        self._start_probe(reference_rate)
                    continue

                self._last_rate = rate

        logger.info("[AutoScaler] Exiting autoscaling loop")

    def _observe_rate(self, rate: float) -> None:
        if self._probe_reference_rate is not None:
            self._samples_since_scale += 1
            self._best_rate_since_scale = max(self._best_rate_since_scale, rate)

        if rate <= 0:
            self._healthy_steps = 0
            self._low_rate_steps += 1
            self._last_rate = rate
            return

        if self._best_rate <= 0:
            self._best_rate = rate
            self._healthy_steps = 1
            self._low_rate_steps = 0
            self._last_rate = rate
            return

        low_threshold = self._best_rate * (1.0 - self.policy.plateau_factor)
        if rate >= low_threshold:
            self._healthy_steps += 1
            self._low_rate_steps = 0
        else:
            self._healthy_steps = 0
            self._low_rate_steps += 1

        if self._probe_reference_rate is None:
            self._best_rate = max(self._best_rate, rate)

        self._last_rate = rate

    def _start_probe(self, reference_rate: float) -> None:
        self._probe_reference_rate = reference_rate
        self._best_rate_since_scale = 0.0
        self._samples_since_scale = 0
        self._healthy_steps = 0
        self._low_rate_steps = 0

    def _should_accept_probe(self) -> bool:
        if self._probe_reference_rate is None:
            return False
        return self._best_rate_since_scale >= self._probe_reference_rate * (
            1.0 + self.policy.up_improvement_factor
        )

    def _should_roll_back_probe(self) -> bool:
        if self._probe_reference_rate is None:
            return False
        if self._samples_since_scale < self.policy.scale_down_window:
            return False
        return not self._should_accept_probe()

    def _reset_trend(self, rate: float) -> None:
        self._last_rate = rate
        self._best_rate = max(rate, 0.0)
        self._best_rate_since_scale = 0.0
        self._healthy_steps = 0
        self._low_rate_steps = 0
        self._samples_since_scale = 0
        self._probe_reference_rate = None

    # -------------------------------
    # Control API
    # -------------------------------

    def pause(self):
        logger.info("[AutoScaler] Paused")
        self._pause_event.clear()

    def resume(self):
        logger.info("[AutoScaler] Resumed")
        self._pause_event.set()

    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def in_backoff(self) -> bool:
        return time.time() < self._backoff_until

    def stop(self):
        logger.info("[AutoScaler] Stop requested")
        self.stop_flag = True
        self._stop_event.set()
        self._pause_event.set()  # ensure loop can exit
