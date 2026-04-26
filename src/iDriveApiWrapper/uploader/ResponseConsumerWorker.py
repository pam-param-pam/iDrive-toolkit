import logging
from queue import Queue
from typing import Optional, Dict

from .UploadContext import UploadContext
from .models import ResponsePayload, UploadFileState, BackendFile, BackendFragment, BackendThumbnail, BackendSubtitle, ChunkAttachment, ThumbnailAttachment, SubtitleAttachment, \
    DiscordAttachment, FileUploadStatus

logger = logging.getLogger("iDrive")


class ResponseConsumerWorker:
    def __init__(self, response_queue: Queue[ResponsePayload], ready_files_queue: Queue[BackendFile], ctx: UploadContext):
        self.response_queue = response_queue
        self.ready_files_queue = ready_files_queue
        self.ctx = ctx
        self._backend_state: Dict[str, BackendFile] = {}

    def run(self) -> None:
        while True:
            payload: Optional[ResponsePayload] = self.response_queue.get()
            if payload is None:
                self.response_queue.task_done()
                break

            try:
                self._handle_response(payload)
            except Exception:
                logger.exception("[ResponseConsumerWorker] Failed processing response")
            finally:
                self.response_queue.task_done()

    def _handle_response(self, payload: ResponsePayload) -> None:
        # Handle empty files
        if payload.is_empty:
            state = self.ctx.get_state(payload.frontend_id)
            backend_file = self._get_or_create_backend_file(state)

            with state.lock:
                state.status = FileUploadStatus.SAVING

            self.ready_files_queue.put(backend_file)
            return

        request = payload.request
        response = payload.response

        discord_attachments = response.json()["attachments"]

        for idx, attachment in enumerate(request.attachments):
            discord_attachment = discord_attachments[idx]

            self._fill_attachment_info(
                attachment=attachment,
                discord_response=response.json(),
                discord_attachment=discord_attachment,
            )

    def _get_or_create_backend_file(self, state: UploadFileState) -> BackendFile:
        artifacts = state.artifacts
        file_id = str(artifacts.frontend_id)
        if file_id not in self._backend_state:
            self._backend_state[file_id] = BackendFile(
                name=artifacts.name,
                parent_id=artifacts.parent_id,
                extension=artifacts.extension,
                size=artifacts.size,
                frontend_id=str(artifacts.frontend_id),
                encryption_method=artifacts.file_crypto.method.value,
                created_at=artifacts.created_at,
                duration=artifacts.duration,
                iv=artifacts.file_crypto.iv_b64(),
                key=artifacts.file_crypto.key_b64(),
                crc=0,
                parent_password=artifacts.parent_password,
                lock_from=artifacts.lock_from_id,
                thumbnail=None,  # filled later
                videoMetadata=artifacts.video_metadata,
                subtitles=[],
                fragments=[],
            )

        return self._backend_state[file_id]

    def _fill_attachment_info(self, attachment: ChunkAttachment | ThumbnailAttachment | SubtitleAttachment | DiscordAttachment, discord_response: dict, discord_attachment: dict) -> None:
        state: UploadFileState = self.ctx.get_state(attachment.frontend_id)
        backend_file = self._get_or_create_backend_file(state)

        if isinstance(attachment, ChunkAttachment):
            backend_file.crc = state.artifacts.file_crc

            backend_file.fragments.append(
                BackendFragment(
                    fragment_sequence=attachment.sequence,
                    fragment_size=len(attachment.data),
                    channel_id=discord_response["channel_id"],
                    message_id=discord_response["id"],
                    attachment_id=discord_attachment["id"],
                    message_author_id=discord_response["author"]["id"],
                    offset=attachment.offset,
                    crc=attachment.crc,
                )
            )

            state.uploaded_chunks += 1

        elif isinstance(attachment, ThumbnailAttachment):
            backend_file.thumbnail = BackendThumbnail(
                size=len(attachment.data),
                channel_id=discord_response["channel_id"],
                message_id=discord_response["id"],
                attachment_id=discord_attachment["id"],
                iv=attachment.crypto.iv_b64(),
                key=attachment.crypto.key_b64(),
                message_author_id=discord_response["author"]["id"],
            )

            state.uploaded_thumbnail = True

        elif isinstance(attachment, SubtitleAttachment):
            backend_file.subtitles.append(
                BackendSubtitle(
                    size=len(attachment.data),
                    channel_id=discord_response["channel_id"],
                    message_id=discord_response["id"],
                    attachment_id=discord_attachment["id"],
                    language=attachment.language,
                    is_forced=attachment.is_forced,
                    iv=attachment.crypto.iv_b64(),
                    key=attachment.crypto.key_b64(),
                    message_author_id=discord_response["author"]["id"],
                )
            )

            state.uploaded_subtitles += 1

        if state.is_fully_uploaded():
            with state.lock:
                state.status = FileUploadStatus.SAVING
            backend_file = self._backend_state.pop(str(state.artifacts.frontend_id))
            self.ready_files_queue.put(backend_file)
