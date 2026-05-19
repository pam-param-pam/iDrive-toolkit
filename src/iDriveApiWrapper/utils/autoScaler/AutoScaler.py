import threading
import time
import logging

from ...downloader.models import ThrottleState
from ...utils.autoScaler.AutoScalePolicy import AutoScalePolicy

logger = logging.getLogger("iDrive")

# todo needs refactoring
# very much actually...


class AutoScaler:
    def __init__(self, throttle_state: ThrottleState, policy: AutoScalePolicy):
        self.throttle_state = throttle_state
        self.policy = policy

        self.current = policy.min_workers
        self.lock = threading.Lock()
        self.stop_flag = False

        # throughput tracking
        self._last_rate = 0.0
        self._no_improve_steps = 0

        # cooldown tracking
        self._last_scale_up_time = 0.0
        self._last_scale_down_time = 0.0

        self._scale_up_cooldown = policy.scale_up_cooldown
        self._scale_down_cooldown = policy.scale_down_cooldown

        # -------------------------------
        # NEW: hard error backoff
        # -------------------------------
        self._backoff_until = 0.0
        self._backoff_duration = policy.hard_error_backoff  # e.g. 5–10s

        # -------------------------------
        # NEW: pause control
        # -------------------------------
        self._pause_event = threading.Event()
        self._pause_event.set()  # running by default

    # -------------------------------
    # Worker count management
    # -------------------------------

    def _increase_workers(self, spawn_func):
        step = self.policy.scale_up_step
        for _ in range(step):
            if self.current >= self.policy.max_workers:
                logger.info(f"[AutoScaler] Wanted scale UP but already at max={self.policy.max_workers}")
                break
            spawn_func()
            self.current += 1

        self._last_scale_up_time = time.time()
        logger.info(f"[AutoScaler] Scaled UP → workers={self.current}")

    def _decrease_workers(self, kill_func):
        step = self.policy.scale_down_step
        for _ in range(step):
            if self.current <= self.policy.min_workers:
                logger.info(f"[AutoScaler] Wanted scale DOWN but already at min={self.policy.min_workers}")
                break
            kill_func()
            self.current -= 1

        self._last_scale_down_time = time.time()
        logger.info(f"[AutoScaler] Scaled DOWN → workers={self.current}")

    # -------------------------------
    # Autoscaling loop
    # -------------------------------

    def start(self, spawn_func, kill_func):
        t = threading.Thread(target=self._loop, args=(spawn_func, kill_func), daemon=True)
        t.start()
        return t

    def _loop(self, spawn_func, kill_func):
        logger.info("[AutoScaler] Started autoscaling loop")

        while not self.stop_flag:
            # -----------------------
            # PAUSE GATE
            # -----------------------
            self._pause_event.wait()

            time.sleep(1.5)
            now = time.time()

            # -----------------------
            # BACKOFF GATE
            # -----------------------
            if now < self._backoff_until:
                continue

            hard_errors = self.throttle_state.error_rate()
            rate = self.throttle_state.bytes_rate()

            can_scale_up = (now - self._last_scale_up_time) >= self._scale_up_cooldown
            can_scale_down = (now - self._last_scale_down_time) >= self._scale_down_cooldown

            with self.lock:
                # -----------------------
                # 1. HARD THROTTLING
                # -----------------------
                if self.policy.should_react_to_hard_errors(hard_errors):
                    logger.warning(f"[AutoScaler] Hard throttling ({hard_errors}) → entering backoff")

                    if can_scale_down:
                        self._decrease_workers(kill_func)

                    # activate backoff window
                    self._backoff_until = now + self._backoff_duration

                    # reset trend tracking
                    self._no_improve_steps = 0
                    self._last_rate = rate
                    continue

                # -----------------------
                # 2. THROUGHPUT TREND
                # -----------------------
                if rate <= self._last_rate * self.policy.plateau_factor:
                    self._no_improve_steps += 1
                else:
                    self._no_improve_steps = 0

                if (
                    self._no_improve_steps >= self.policy.scale_down_window
                    and self.current > self.policy.min_workers
                    and can_scale_down
                ):
                    logger.info(
                        f"[AutoScaler] Plateau detected → scale DOWN "
                        f"(rate={rate:.1f}, prev={self._last_rate:.1f})"
                    )
                    self._decrease_workers(kill_func)
                    self._last_rate = rate
                    continue

                # -----------------------
                # 3. SCALE UP
                # -----------------------
                if rate > self._last_rate * self.policy.up_improvement_factor and can_scale_up:
                    logger.info("[AutoScaler] Throughput improving → scale UP")
                    self._increase_workers(spawn_func)

                self._last_rate = rate

        logger.info("[AutoScaler] Exiting autoscaling loop")

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
        self._pause_event.set()  # ensure loop can exit
