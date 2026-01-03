import logging
from queue import Queue
from typing import Optional, Dict

from .UploadContext import UploadContext
from .models import ResponsePayload, UploadFileState, BackendFile, BackendFragment, BackendThumbnail, BackendSubtitle, ChunkAttachment, ThumbnailAttachment, SubtitleAttachment, \
    DiscordAttachment

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

    # -------------------------------------------------
    # Core logic (JS: handleDiscordResult)
    # -------------------------------------------------

    def _handle_response(self, payload: ResponsePayload) -> None:
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

    # -------------------------------------------------
    # Backend state handling (JS: getOrCreateState)
    # -------------------------------------------------

    def _get_or_create_backend_file(self, st: UploadFileState) -> BackendFile:
        fid = st

        if fid not in self._backend_state:
            self._backend_state[fid] = BackendFile(
                name=st.name,
                parent_id=st.folder_id,
                extension=st.extension,
                size=st.size,
                frontend_id=fid,
                encryption_method=int(st.encryption_method),
                created_at=st.created_at,
                duration=st.duration,
                iv=st.iv,
                key=st.key,
                crc=0,
                parent_password=st.file_obj.parent_password,
                lock_from=st.file_obj.lock_from,
                thumbnail=None,
                videoMetadata=None,
                subtitles=[],
                fragments=[],
            )

        return self._backend_state[fid]

    # -------------------------------------------------
    # Attachment processing (JS: fillAttachmentInfo)
    # -------------------------------------------------

    def _fill_attachment_info(self, attachment: ChunkAttachment | ThumbnailAttachment | SubtitleAttachment | DiscordAttachment, discord_response: dict, discord_attachment: dict) -> None:
        st: UploadFileState = self.ctx.get_state(attachment.frontend_id)
        backend_file = self._get_or_create_backend_file(st)

        if isinstance(attachment, ChunkAttachment):
            backend_file.crc = st.crc

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

            st.increment_chunk()

        elif isinstance(attachment, ThumbnailAttachment):
            backend_file.thumbnail = BackendThumbnail(
                size=len(attachment.raw_blob),
                channel_id=discord_response["channel_id"],
                message_id=discord_response["id"],
                attachment_id=discord_attachment["id"],
                iv=attachment.iv,
                key=attachment.key,
                message_author_id=discord_response["author"]["id"],
            )

            st.mark_thumbnail_uploaded()

        elif isinstance(attachment, SubtitleAttachment):
            backend_file.subtitles.append(
                BackendSubtitle(
                    size=len(attachment.data),
                    channel_id=discord_response["channel_id"],
                    message_id=discord_response["id"],
                    attachment_id=discord_attachment["id"],
                    language=attachment.language,
                    is_forced=attachment.is_forced,
                    iv=attachment.crypto.iv,
                    key=attachment.crypto.key,
                    message_author_id=discord_response["author"]["id"],
                )
            )

            if st.extracted_subtitle_count == len(backend_file.subtitles):
                st.mark_subtitles_uploaded()

        # -------------------------------------------------
        # Finalization (JS: isFullyUploaded)
        # -------------------------------------------------

        if st.is_fully_uploaded():
            st.mark_file_uploaded()
            backend_file = self._backend_state.pop(st.file_obj.frontend_id)
            self.ready_files_queue.put(backend_file)
