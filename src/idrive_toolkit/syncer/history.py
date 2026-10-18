import threading


class TransferHistory:
    def __init__(self):
        self.ctx = self
        self._engines = {}
        self._lock = threading.RLock()
        self._superseded = set()

    def supersede(self, engine, key):
        with self._lock:
            self._superseded.add(f"{id(engine)}:{key}")

    def add(self, engine):
        with self._lock:
            self._engines[str(id(engine))] = engine

    def _entries(self):
        with self._lock:
            return list(self._engines.items())

    def get_all_states(self):
        return {f"{prefix}:{key}": state for prefix, engine in self._entries()
                for key, state in engine.ctx.get_all_states().items()
                if f"{prefix}:{key}" not in self._superseded}

    def get_failed_states(self):
        return {key: state for key, state in self.get_all_states().items()
                if state.status.value in ("failed", "save_failed")}

    @property
    def records(self):
        return {f"{prefix}:{key}": record for prefix, engine in self._entries()
                for key, record in getattr(engine.ctx, "records", {}).copy().items()}

    def remove_state(self, key):
        prefix, state_id = key.split(":", 1)
        engine = self._engines[prefix]
        return engine._discard_handler(engine, state_id)

    def retry_failed_files(self, file_ids=None):
        selected = set(file_ids) if file_ids is not None else None
        count = 0
        for prefix, engine in self._entries():
            keys = [key for key in engine.get_failed_states() if f"{prefix}:{key}" not in self._superseded
                    if selected is None or f"{prefix}:{key}" in selected]
            if keys:
                count += engine.retry_failed_files(keys)
        return count
