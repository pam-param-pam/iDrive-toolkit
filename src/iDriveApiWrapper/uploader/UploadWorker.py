import logging
import time
import threading
import uuid
from typing import Dict, Set, Callable
from queue import Queue

from .DiscordUploader import DiscordUploader
from .models import DiscordRequest, UploadFileState, ResponsePayload
from ..exceptions import DiscordRateLimitError, BackendRateLimitError, BackendServiceUnavailableError, BackendServerTimeout, DiscordServerTimeout

logger = logging.getLogger("iDrive")

#todo unchecked
class UploadConfig:
    pass


class UploadWorker:
    def __init__(self,
                 upload_queue: Queue[DiscordRequest],
                 response_queue: Queue[ResponsePayload],
                 upload_states: Dict[uuid.UUID, UploadFileState],
                 max_retries: int,
                 global_pause: threading.Event):

        self.upload_queue = upload_queue
        self.upload_states = upload_states
        self._get_config = get_config
        self.max_retries = max_retries
        self.global_pause = global_pause
        self.http = DiscordUploader(self._get_config,  global_pause, upload_states)

    def run(self) -> None:
        while True:
            task = self.upload_queue.get()
            print("FOUND TASK", task)
            if task is None:
                self.upload_queue.task_done()
                break

            file_ids = self._file_ids_from_task(task)
            states = self._states_for_file_ids(file_ids)

            if not states:
                print("No states found")
                self.upload_queue.task_done()
                continue

            if self._any_cancelled(states):
                print("CANCELED")
                self.upload_queue.task_done()
                continue

            if not self._can_run_now(states):
                print("CANNED")
                self.upload_queue.put(task)
                self.upload_queue.task_done()
                time.sleep(0.05)
                continue

            try:
                self._mark_uploading(states)
                self._upload(task)
                self._mark_progress(task)
                self._mark_completed_if_done(states)

            except (DiscordRateLimitError, BackendRateLimitError) as e:
                if task.retries >= self.max_retries:
                    self._fail_states(states, e)
                else:
                    logger.warning(f"[UploadWorker] Throttled ({e.__class__.__name__}) → retrying in {e.wait}s (retry {task.retries}) request={task.request_id}")
                    time.sleep(e.wait)
                    task.retries += 1
                    self.upload_queue.put(task)

            except (BackendServiceUnavailableError, BackendServerTimeout, DiscordServerTimeout) as e:
                self._mark_retrying_network(states)
                logger.warning(f"[UploadWorker] Network issue ({e.__class__.__name__}) → waiting 5s request={task.request_id}")
                time.sleep(5)
                self.upload_queue.put(task)

            except Exception as e:
                self._fail_states(states, e)
                logger.exception(f"[UploadWorker] Unexpected failure request={task.request_id}")

            finally:
                self.upload_queue.task_done()

    def _upload(self, task: DiscordRequest) -> None:
        if self._any_cancelled(self._states_for_file_ids(self._file_ids_from_task(task))):
            return
        self.http.upload(task)

    def _file_ids_from_task(self, task: DiscordRequest) -> Set[uuid.UUID]:
        ids: Set[uuid.UUID] = set()
        for att in task.attachments:
            ids.add(att.frontend_id)
        return ids

    def _states_for_file_ids(self, file_ids: Set[uuid.UUID]) -> Dict[uuid.UUID, UploadFileState]:
        out: Dict[uuid.UUID, UploadFileState] = {}
        for fid in file_ids:
            st = self.upload_states.get(fid)
            if st is not None:
                out[fid] = st
        return out

    def _any_cancelled(self, states: Dict[uuid.UUID, UploadFileState]) -> bool:
        for st in states.values():
            if st.cancelled:
                return True
        return False

    def _can_run_now(self, states: Dict[uuid.UUID, UploadFileState]) -> bool:
        if not self.global_pause.is_set():
            return False
        for st in states.values():
            if not st.pause_event.is_set():
                return False
        return True

    def _mark_uploading(self, states: Dict[uuid.UUID, UploadFileState]) -> None:
        for st in states.values():
            with st.lock:
                if not st.is_terminal():
                    st.status = UploadFileStatus.UPLOADING

    def _mark_retrying_network(self, states: Dict[uuid.UUID, UploadFileState]) -> None:
        for st in states.values():
            with st.lock:
                if not st.is_terminal():
                    st.status = UploadFileStatus.RETRYING_NETWORK

    def _fail_states(self, states: Dict[uuid.UUID, UploadFileState], e: Exception) -> None:
        for st in states.values():
            with st.lock:
                st.error = e
                if not st.cancelled:
                    st.status = UploadFileStatus.FAILED

    def _mark_progress(self, task: DiscordRequest) -> None:
        for att in task.attachments:
            st = self.upload_states.get(att.frontend_id)
            if st is None:
                continue
            with st.lock:
                if st.is_terminal() or st.cancelled:
                    continue
                if isinstance(att, ChunkAttachment):
                    st.uploaded_chunks += 1
                elif isinstance(att, SubtitleAttachment):
                    st.uploaded_subtitles += 1
                elif isinstance(att, ThumbnailAttachment):
                    st.uploaded_thumbnail += 1

    def _mark_completed_if_done(self, states: Dict[uuid.UUID, UploadFileState]) -> None:
        for st in states.values():
            with st.lock:
                if st.cancelled or st.is_terminal():
                    continue
                if st.is_fully_extracted():
                    st.status = UploadFileStatus.COMPLETED
