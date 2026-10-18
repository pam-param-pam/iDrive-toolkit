"""Continuous, bounded file pipeline. Only this coordinator mutates backends."""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .jobs import FolderTarget, JobPhase, TransferJob, resolve_folder
from .capacity import StagingBudget
from ..progress import TransferProgressDirection
from ..transfer import SyncTransferCancelled

logger = logging.getLogger("iDrive")


class TransferManager:
    MAX_ACTIVE_JOBS = 128

    def __init__(self, workspace, monitor, destination=None, budget=None, progress=None, has_active=None):
        self.workspace = workspace
        self.monitor = monitor
        self.destination = destination
        self._downloaders = {}
        self._uploaders = {}
        self._executor = None
        self._active = []
        self.jobs = []
        self.reports = []
        self._budget = budget or StagingBudget(workspace)
        self._progress = progress or self.poll
        self._has_active = has_active or self.has_active
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._check_cancel = lambda: None
        self._deletions = []
        self._closed = False
        self._verification_children = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close(cancel_pending=exc_type is not None)

    def submit(self, job, source, destination, report, check_cancel=lambda: None):
        self._check_cancel = check_cancel
        job.source, job.destination, job.report = source, destination, report
        self.jobs.append(job)
        try:
            self._prepare_paths(job)
            extra = 0 if job.resume_verification or job.saved_upload is not None else self.staging_requirement(source, job)
            amount = self._budget.required(job, extra)
            while self._has_active() and (
                len(self._active) >= self.MAX_ACTIVE_JOBS
                or not self._budget.can_reserve(job, amount)
            ):
                self._progress(check_cancel)
                self._wake.wait(0.05)
            check_cancel()
            if not self._budget.can_reserve(job, amount):
                raise OSError("Staging capacity exhausted; retry or discard retained failed files")
            self._budget.reserve(job, amount)
            self._active.append(job)
            if job.resume_verification:
                job.phase = JobPhase.VERIFYING
            elif job.saved_upload is not None:
                job.phase = JobPhase.STAGED
            else:
                source.schedule_export(job, self)
        except SyncTransferCancelled:
            raise
        except Exception as error:
            self._fail(job, error)
        self.poll(check_cancel)

    def has_active(self):
        return bool(self._active)

    def add_report(self, report):
        if all(existing is not report for existing in self.reports):
            self.reports.append(report)

    def staging_requirement(self, backend, job):
        include_output = not (job.destination.kind == "local" and not (job.force_staging or job.replacements))
        return backend.staging_requirement(job.node, self.workspace, include_output=include_output)

    def _prepare_paths(self, job):
        if job.source.kind != "remote":
            return
        fragment_dir = self.workspace.get_temp_path("downloader") / str(job.node.uid)
        if self.destination.kind == "local" and not (job.force_staging or job.replacements):
            job.cleanup_paths = [fragment_dir]
        else:
            folder = self.workspace.file_dir((str(job.node.uid), job.node.size, job.node.hash))
            job.cleanup_paths = [folder, fragment_dir]

    def schedule_local_export(self, job, path):
        job.path = Path(path)
        if not job.path.is_file():
            raise FileNotFoundError(job.path)
        job.phase = JobPhase.STAGED

    def schedule_remote_export(self, backend, job):
        item = backend.transfer_item(job.node.uid)
        direct = self._direct_download_target(job)
        if direct is None:
            folder = self.workspace.file_dir((item.id, job.node.size, job.node.hash))
            job.path = folder / "content"
        else:
            folder, job.path = direct.parent, direct
        job.transfer = self._downloader(backend)
        job.transfer_id = str(item.id)
        job.phase = JobPhase.DOWNLOADING
        job.transfer.download(item, target_dir=folder, output_name=job.path.name)

    def schedule_local_import(self, job, parent, name):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=min(4, os.cpu_count() or 1), thread_name_prefix="sync-copy")
        target = self._local_target(parent, name)
        if job.path.resolve() == target:
            job.phase = JobPhase.VERIFYING
            return
        job.future = self._executor.submit(self._copy_local, job.path, parent, name, self._check_copy_cancel)
        job.phase = JobPhase.UPLOADING
        job.transfer = None

    def schedule_remote_import(self, backend, job, parent, name):
        job.transfer = self._uploader(backend)
        job.transfer_id = job.id
        job.phase = JobPhase.UPLOADING
        if job.saved_upload is not None:
            job.transfer_id = job.saved_upload.frontend_id
            job.transfer.restore_backend_save(job.saved_upload, job.saved_state)
        else:
            job.transfer.upload(job.path, parent=backend.transfer_item(parent), name=name, file_id=job.id)

    def poll(self, check_cancel=lambda: None):
        """Advance individual jobs without draining either transfer engine."""
        self._wake.clear()
        verifying = []
        for job in list(self._active):
            check_cancel()
            try:
                if job.phase in (JobPhase.DOWNLOADING, JobPhase.UPLOADING):
                    if job.future is not None:
                        if not job.future.done():
                            continue
                        job.future.result()
                        job.future = None
                        job.phase = JobPhase.VERIFYING
                    else:
                        state = job.transfer.ctx.get_state(job.transfer_id)
                        if state is None:
                            continue
                        status = state.status.value
                        if status in ("failed", "save_failed", "aborted"):
                            raise state.error or RuntimeError(f"Transfer {status}: {job.name}")
                        if status != "completed":
                            continue
                        job.phase = JobPhase.STAGED if job.phase == JobPhase.DOWNLOADING else JobPhase.VERIFYING
                if job.phase == JobPhase.STAGED:
                    job.destination_parent = resolve_folder(job.destination_parent)
                    job.destination.schedule_import(job, job.destination_parent, job.transfer_name, self)
                if job.phase == JobPhase.VERIFYING:
                    verifying.append(job)
            except SyncTransferCancelled:
                raise
            except Exception as error:
                self._fail(job, error)
        # Collect completed saves before fetching any listing. Every file in
        # this group must already exist when its parent's snapshot is loaded.
        self._verification_children = {}
        try:
            for job in verifying:
                check_cancel()
                try:
                    self._verify(job)
                    self.cleanup(job)
                    job.phase = JobPhase.COMPLETED
                    job.report.files_copied += 1
                    self._retire(job)
                except SyncTransferCancelled:
                    raise
                except Exception as error:
                    self._fail(job, error)
        finally:
            self._verification_children = None

    def _verify(self, job):
        from .engine import _children, _find_file
        key = (job.destination, job.destination_parent)
        cache = self._verification_children
        if cache is None or key not in cache:
            _, children = _children(job.destination, job.destination_parent)
            if cache is not None:
                cache[key] = children
        else:
            children = cache[key]
        match, _ = _find_file(children, job.node, name=job.transfer_name)
        if match is None and job.replacements:
            # A rename can commit before its response times out; cleanup can
            # also fail after promotion. Reconcile that completed destination.
            promoted, _ = _find_file(children, job.node, name=job.name)
            if promoted is not None:
                return
        if match is None:
            raise RuntimeError(f"Destination size/CRC verification failed: {job.name}")
        # The original remains intact until the replacement has been verified.
        if job.replacements:
            current_ids = {child.uid for child in children}
            for uid in job.replacements:
                if uid in current_ids:
                    job.destination.remove(uid)
            job.destination.rename(match.uid, job.name)

    def _fail(self, job, error):
        job.failed_phase, job.phase, job.error = job.phase, JobPhase.FAILED, error
        job.report.files_failed += 1
        if job.transfer is None:
            if job.source.kind == "remote":
                job.transfer = self._downloader(job.source)
                job.transfer_id = str(job.node.uid)
            elif job.destination.kind == "remote":
                job.transfer = self._uploader(job.destination)
                job.transfer_id = job.id
        # Keep engine file states, including scheduling and verification errors.
        if job.transfer is not None and job.transfer_id is not None:
            job.transfer.record_failure(job.transfer_id, job.node, error)
        logger.warning("Sync file failed: %s (%s): %s", job.name, job.failed_phase.value, error)
        self._retire(job)

    def retry_files(self, engine, file_ids=None):
        """Replay failed jobs through the same coordinator, after its run closes.

        A resumed download validates cached output. Backend-save retries reuse
        Discord attachment metadata rather than uploading the content again.
        """
        if not self._closed:
            raise RuntimeError("Wait for synchronization to finish before retrying failed files")
        keys = set(file_ids) if file_ids is not None else set(engine.get_failed_states())
        jobs = [job for job in self.jobs if job.phase == JobPhase.FAILED
                and job.transfer is engine and job.transfer_id in keys]
        if not jobs:
            return 0
        with self.workspace.lock():
            with TransferManager(self.workspace, self.monitor, self.destination) as retry:
                for old in jobs:
                    state = engine.ctx.get_all_states().get(old.transfer_id)
                    saved = getattr(state, "backend_file", None)
                    job = replace(old, phase=JobPhase.QUEUED, error=None, failed_phase=None,
                                  transfer=None, transfer_id=None, future=None, cleanup_paths=[],
                                  saved_upload=saved, saved_state=state if saved is not None else None,
                                  resume_verification=old.failed_phase == JobPhase.VERIFYING)
                    old.report.files_failed -= 1
                    if job.resume_verification:
                        # No new upload/download is submitted for verification.
                        # Retain the original state as the inspection record.
                        job.transfer, job.transfer_id = engine, old.transfer_id
                    else:
                        self.monitor.history.supersede(engine, old.transfer_id)
                    retry.submit(job, old.source, old.destination, old.report)
                retry.finish()
                for job in retry.jobs:
                    if job.resume_verification and job.phase == JobPhase.COMPLETED:
                        state = job.transfer.ctx.get_all_states().get(job.transfer_id)
                        if state is not None:
                            state.status = type(state.status).COMPLETED
                            state.error = None
        return len(jobs)

    def discard_file(self, engine, key):
        if not self._closed:
            raise RuntimeError("Finish or abort synchronization before discarding file state")
        with self.workspace.lock():
            for job in self.jobs:
                if job.transfer is engine and job.transfer_id == key and job.phase in (JobPhase.FAILED, JobPhase.CANCELLED):
                    self.cleanup(job)
        return engine.ctx.remove_state(key)

    def _retire(self, job):
        if job in self._active:
            self._active.remove(job)
        self._budget.release(job)
        self._wake.set()

    def defer_deletion(self, backend, uid):
        self._deletions.append((backend, uid))

    def finish(self, check_cancel=lambda: None):
        while self._active:
            self.poll(check_cancel)
            self._wake.wait(0.05)
        # Never infer that a source tree is empty from a failed transfer/scan.
        for backend, uid in self._deletions:
            check_cancel()
            if not any(current.files_failed or current.folder_errors for current in self.reports):
                backend.remove(uid)
        self._deletions.clear()

    def close(self, cancel_pending=False):
        if self._closed:
            return
        self._closed = True
        if cancel_pending:
            self._stop.set()
        for transfer in [*self._downloaders.values(), *self._uploaders.values()]:
            transfer.shutdown(cancel_pending=cancel_pending)
            self.monitor.stop_observing(transfer)
        if self._executor is not None:
            self._executor.shutdown(wait=True, cancel_futures=cancel_pending)
        if cancel_pending:
            for job in list(self._active):
                job.phase = JobPhase.CANCELLED
                job.error = SyncTransferCancelled("Synchronization aborted")
                self._retire(job)

    def discard(self, backend, node):
        backend.discard_staged(node, self.workspace)

    def cleanup(self, job):
        for path in job.cleanup_paths:
            self.workspace.clean(path)
        job.cleanup_paths.clear()

    def _downloader(self, backend):
        if backend not in self._downloaders:
            engine = backend.create_transfer_downloader(storage=self.workspace, write_guard=None)
            self._downloaders[backend] = engine
            engine._retry_handler = self.retry_files
            engine._discard_handler = self.discard_file
            self.monitor.observe(engine, TransferProgressDirection.DOWNLOAD)
        return self._downloaders[backend]

    def _uploader(self, backend):
        if backend not in self._uploaders:
            engine = backend.create_transfer_uploader()
            self._uploaders[backend] = engine
            engine._retry_handler = self.retry_files
            engine._discard_handler = self.discard_file
            self.monitor.observe(engine, TransferProgressDirection.UPLOAD)
        return self._uploaders[backend]

    def _direct_download_target(self, job):
        if job.force_staging or job.replacements or self.destination is None or self.destination.kind != "local":
            return None
        parent = job.destination_parent
        if isinstance(parent, FolderTarget):
            parent = resolve_folder(parent)
            job.destination_parent = parent
        target = self.destination.download_target(parent, job.name)
        return Path(target) if target is not None else None

    @staticmethod
    def _copy_local(source, parent, name, check_cancel=lambda: None):
        target = TransferManager._local_target(parent, name)
        created = False
        try:
            with target.open("xb") as output:
                created = True
                with Path(source).open("rb") as input_file:
                    while chunk := input_file.read(1024 * 1024):
                        check_cancel()
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise
        return target

    def _check_copy_cancel(self):
        if self._stop.is_set():
            raise SyncTransferCancelled("Synchronization aborted")
        self._check_cancel()

    @staticmethod
    def _local_target(parent, name):
        parent = Path(parent).resolve()
        if not name or name in (".", "..") or "/" in name or "\\" in name or Path(name).name != name:
            raise ValueError(f"Not a local child name: {name!r}")
        target = parent / name
        if target.resolve().parent != parent:
            raise ValueError(f"Path escapes parent: {target}")
        return target
