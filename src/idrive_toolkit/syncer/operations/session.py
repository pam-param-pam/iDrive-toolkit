"""Resources shared by every direction and selected folder in one sync action."""
from contextlib import ExitStack

from .capacity import StagingBudget
from .transfers import TransferManager
from .workspace import TransferWorkspace


class TransferSession:
    def __init__(self, staging_dir, max_staging_gb, monitor):
        self.workspace = TransferWorkspace(staging_dir, max_staging_gb)
        self.budget = None
        self.monitor = monitor
        self.managers = {}
        self._resources = ExitStack()

    def __enter__(self):
        self._resources.enter_context(self.workspace.lock())
        self.budget = StagingBudget(self.workspace)
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._resources.__exit__(exc_type, exc, traceback)

    def manager(self, destination):
        if destination not in self.managers:
            self.managers[destination] = self._resources.enter_context(TransferManager(
                self.workspace, self.monitor, destination, self.budget, self.poll, self.has_active))
        return self.managers[destination]

    def has_active(self):
        return any(manager.has_active() for manager in self.managers.values())

    def poll(self, check_cancel):
        for manager in self.managers.values():
            manager.poll(check_cancel)

    def finish(self, check_cancel):
        for manager in self.managers.values():
            manager.finish(check_cancel)

    @property
    def jobs(self):
        return [job for manager in self.managers.values() for job in manager.jobs]

    @property
    def reports(self):
        return [report for manager in self.managers.values() for report in manager.reports]
