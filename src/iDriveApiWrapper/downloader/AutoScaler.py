import threading
import time
import logging

from ..downloader.state import ThrottleState

logger = logging.getLogger("iDrive")

# todo needs refactoring

class AutoScaler:
    def __init__(self, max_workers: int, throttle_state: ThrottleState):
        self.min = 10
        self.max = max_workers
        self.current = self.min
        self.ts = throttle_state
        self.lock = threading.Lock()
        self.stop_flag = False

        self._last_rate = 0.0
        self._no_improve_steps = 0

        # cooldown tracking
        self._last_scale_up_time = 0.0
        self._last_scale_down_time = 0.0

        self._scale_up_cooldown = 3     # seconds
        self._scale_down_cooldown = 6  # seconds

    # -------------------------------
    # Worker count management
    # -------------------------------

    def _inc_workers(self, spawn_fn):
        if self.current < self.max:
            spawn_fn()
            self.current += 1
            self._last_scale_up_time = time.time()
            logger.info(f"[AutoScaler] Scaled UP → workers={self.current}")
        else:
            logger.info(f"[AutoScaler] Wanted scale UP but already at max={self.max}")

    def _dec_workers(self, kill_fn):
        if self.current > self.min:
            kill_fn()
            self.current -= 1
            self._last_scale_down_time = time.time()
            logger.info(f"[AutoScaler] Scaled DOWN → workers={self.current}")
        else:
            logger.info( f"[AutoScaler] Wanted scale DOWN but already at min={self.min}")

    # -------------------------------
    # Autoscaling loop
    # -------------------------------

    def start(self, spawn_fn, kill_fn):
        t = threading.Thread(target=self._loop, args=(spawn_fn, kill_fn), daemon=True)
        t.start()
        return t

    def _loop(self, spawn_fn, kill_fn):
        logger.info("[AutoScaler] Started autoscaling loop")

        while not self.stop_flag:
            time.sleep(1.5)

            now = time.time()
            hard_errors = self.ts.error_rate()
            rate = self.ts.download_rate()

            # cooldown checks
            can_scale_up = (now - self._last_scale_up_time) >= self._scale_up_cooldown
            can_scale_down = (now - self._last_scale_down_time) >= self._scale_down_cooldown

            with self.lock:
                # 1. hard throttling → immediate scale down (if not in cooldown)
                if hard_errors > 0:
                    logger.warning(f"[AutoScaler] Hard throttling ({hard_errors}) → request scale DOWN")
                    if can_scale_down:
                        self._dec_workers(kill_fn)
                    else:
                        logger.debug("[AutoScaler] DOWN blocked by cooldown")
                    self._last_rate = rate
                    continue

                # 2. throughput trend → check if plateau
                if rate <= self._last_rate * 1.02:  # <2% improvement
                    self._no_improve_steps += 1
                else:
                    self._no_improve_steps = 0

                if self._no_improve_steps >= 4 and self.current > self.min:
                    logger.info(f"[AutoScaler] Throughput plateau: rate={rate:.1f}, prev={self._last_rate:.1f}")
                    if can_scale_down:
                        self._dec_workers(kill_fn)
                    else:
                        logger.debug("[AutoScaler] DOWN blocked by cooldown")
                    self._last_rate = rate
                    continue

                # 3. scaling UP logic
                if hard_errors == 0 and can_scale_up:
                    if rate > self._last_rate * 1.10:  # >10% improvement
                        logger.info("[AutoScaler] Improving throughput → scale UP")
                        self._inc_workers(spawn_fn)
                else:
                    if not can_scale_up:
                        logger.debug("[AutoScaler] UP blocked by cooldown")

                self._last_rate = rate

        logger.info("[AutoScaler] Exiting autoscaling loop")

    def stop(self):
        logger.info("[AutoScaler] Stop requested")
        self.stop_flag = True

