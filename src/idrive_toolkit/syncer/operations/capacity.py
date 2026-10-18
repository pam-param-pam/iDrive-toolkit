"""Account for retained bytes plus the unwritten portion of active jobs."""
from pathlib import Path


def disk_usage(paths):
    total = 0
    for directory in paths:
        for path in Path(directory).rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                pass
    return total


class StagingBudget:
    def __init__(self, workspace):
        self.workspace = workspace
        self.reservations = {}
        self._charged = workspace.used()
        self._retained = {}
        self._reserved_growth = 0

    def _existing(self, job):
        if job.id not in self._retained:
            self._retained[job.id] = disk_usage(job.cleanup_paths)
        return self._retained[job.id]

    def required(self, job, additional):
        return additional + self._existing(job)

    def can_reserve(self, job, required):
        additional = max(0, required - self._existing(job))
        if additional == 0:
            # Verification and retained backend saves can free an exhausted
            # workspace without consuming another byte of staging capacity.
            return True
        return self._charged + additional <= self.workspace.limit

    def reserve(self, job, required):
        additional = max(0, required - self._existing(job))
        if additional:
            # Conservatively count every active reservation's growth, including
            # bytes already written, without walking its fragment directories.
            self.workspace.ensure_disk_space(self._reserved_growth + additional)
        self.reservations[job.id] = (required, additional)
        self._charged += additional
        self._reserved_growth += additional

    def release(self, job):
        reservation = self.reservations.pop(job.id, None)
        if reservation is not None:
            peak, growth = reservation
            retained = disk_usage(job.cleanup_paths)
            self._charged += retained - peak
            self._reserved_growth -= growth
            self._retained[job.id] = retained
