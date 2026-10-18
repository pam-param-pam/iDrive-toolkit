"""Worker ownership and cooperative retirement, independent of work queues."""
import threading


class WorkerPool:
    def __init__(self, factory, name):
        self.factory = factory
        self.name = name
        self._workers = []
        self._lock = threading.Lock()

    def spawn(self):
        retire = threading.Event()
        worker = self.factory()
        thread = threading.Thread(target=worker.run, args=(retire,), name=self.name, daemon=True)
        with self._lock:
            self._workers.append((thread, retire))
        thread.start()
        return True

    def retire_one(self):
        with self._lock:
            for thread, retire in reversed(self._workers):
                if thread.is_alive() and not retire.is_set():
                    retire.set()
                    return True
        return False

    def available_workers(self):
        with self._lock:
            return sum(thread.is_alive() and not retire.is_set() for thread, retire in self._workers)

    def close(self):
        with self._lock:
            workers = list(self._workers)
            for _, retire in workers:
                retire.set()
        for thread, _ in workers:
            thread.join()
