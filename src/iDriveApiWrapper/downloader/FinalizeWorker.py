import logging
import os

from ..downloader.FileFinalizer import FileFinalizer
from .DownloadContext import DownloadContext
from .path_utlis import safe_rmtree, safe_move_src_only
from .state import FileDownloadStatus
from ..exceptions import PathDoesntExistError

logger = logging.getLogger("iDrive")

# Cleaned v.1

class FinalizeWorker:
    def __init__(self, finalize_q, ctx: DownloadContext):
        self.fq = finalize_q
        self.ctx = ctx
        self.finalizer = FileFinalizer()

    def run(self) -> None:
        while True:
            fid = self.fq.get()

            if fid is None:
                self.fq.task_done()
                break

            state = self.ctx.states.get(fid)
            record = self.ctx.records.get(fid)

            if state is None or record is None:
                self.fq.task_done()
                continue

            try:
                cancelled = False
                with state.lock:
                    if state.status == FileDownloadStatus.CANCELLED:
                        cancelled = True

                if not cancelled:
                    # ---- safe to finalize ----
                    self.finalizer.finalize(record)

                    output_dir = record.output_dir
                    if not os.path.isdir(output_dir):
                        raise PathDoesntExistError(
                            f"Target directory does not exist: {output_dir}"
                        )

                    safe_move_src_only(record.output_path, record.final_user_output_path)
                    safe_rmtree(record.file_dir)

                    with state.lock:
                        state.status = FileDownloadStatus.COMPLETED

                    # user callback (never under lock)
                    try:
                        if record.on_complete:
                            record.on_complete(fid, state)
                    except Exception:
                        logger.exception(f"[FinalizeWorker] on_complete callback failed for file {fid}")

            except Exception as e:
                with state.lock:
                    state.status = FileDownloadStatus.FAILED
                    state.error = e
                logger.exception(f"[FinalizeWorker] Finalization failed for file {fid}")

            finally:
                # terminal → block forever
                self.ctx.recompute_run_event(state)
                self.fq.task_done()
