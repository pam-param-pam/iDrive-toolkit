import uuid
import zlib
from queue import Queue
from typing import Iterator

from .Extractor import extract_video_metadata_if_needed, extract_thumbnail_if_needed, extract_subtitles_if_needed, get_file_extension
from .models import DiscordAttachment, DiscordRequest, UploadInput, UploadFileState, Crypto, ThumbnailAttachment, SubtitleAttachment, ChunkAttachment, FileUploadStatus, FileArtifacts
from ..uploader.Encryptor import Encryptor
from ..uploader.UploadContext import UploadContext


class _RequestBuilder:
    def __init__(self, ctx: UploadContext):
        self.ctx = ctx
        self.attachments: list[DiscordAttachment] = []
        self.total_size = 0

    def can_fit(self, attachment: DiscordAttachment) -> bool:
        return len(self.attachments) < self.ctx.max_attachments and self.total_size + attachment.size <= self.ctx.max_size

    def add(self, attachment: DiscordAttachment) -> None:
        self.attachments.append(attachment)
        self.total_size += attachment.size

    def flush(self) -> DiscordRequest | None:
        if not self.attachments:
            return None
        req = DiscordRequest(attachments=self.attachments)
        self.attachments = []
        self.total_size = 0
        return req

    def flush_if_needed(self, attachment: DiscordAttachment) -> DiscordRequest | None:
        return self.flush() if not self.can_fit(attachment) else None

    def remaining_size(self) -> int:
        return self.ctx.max_size - self.total_size


class PrepareRequestWorker:
    def __init__(self, input_queue: Queue[UploadInput], upload_queue: Queue[DiscordRequest], ctx: UploadContext):
        self._input_queue = input_queue
        self._upload_queue = upload_queue
        self._builder = _RequestBuilder(ctx)
        self.ctx = ctx

    def run(self) -> None:
        while True:
            item = self._input_queue.get()
            if item is None:
                self._input_queue.task_done()
                break

            try:
                for request in self.prepare_upload(item):
                    self._upload_queue.put(request)

            finally:
                self._input_queue.task_done()

        req = self._builder.flush()
        if req:
            self._upload_queue.put(req)

    def prepare_upload(self, input_item: UploadInput) -> Iterator[DiscordRequest]:
        path = input_item.path
        parent = input_item.parent
        lock_from_id = input_item.lock_from_id

        if path.is_dir():
            new_parent = parent.create_subfolder(path.name)
            for child in path.iterdir():
                yield from self.prepare_upload(UploadInput(path=child, parent=new_parent, lock_from_id=lock_from_id))
            return

        frontend_id = str(uuid.uuid4())
        state = UploadFileState()
        self.ctx.states[frontend_id] = state

        # try:
        state.status = FileUploadStatus.SCANNING

        extension = get_file_extension(path.name)

        encryption_method = self.ctx.encryption_method
        video_metadata, duration = extract_video_metadata_if_needed(self.ctx.extensions, extension, path)
        file_crypto = Crypto.generate(encryption_method)

        state.artifacts = FileArtifacts(
            frontend_id=frontend_id,
            name=path.name,
            extension=extension,
            created_at=int(path.stat().st_mtime * 1000),
            size=path.stat().st_size,
            parent_id=parent.id,
            lock_from_id=lock_from_id,
            parent_password=parent.get_password(),
            encryption_method=encryption_method,
            file_crypto=file_crypto,
            duration=duration,
            video_metadata=video_metadata
        )

        thumbnail = extract_thumbnail_if_needed(self.ctx.extensions, extension, path)
        if thumbnail:
            thumbnail_crypto = Crypto.generate(encryption_method)
            thumb_encryptor = Encryptor(method=thumbnail_crypto.method, key=thumbnail_crypto.key, iv=thumbnail_crypto.iv)
            encrypted_thumb = thumb_encryptor.encrypt(thumbnail.data)
            att = ThumbnailAttachment(frontend_id=frontend_id, data=encrypted_thumb, crypto=thumbnail_crypto)
            state.expected_thumbnail = True
            req = self._builder.flush_if_needed(att)
            if req:
                yield req
            self._builder.add(att)

        for sub in extract_subtitles_if_needed(self.ctx.extensions, extension, path):
            subtitle_crypto = Crypto.generate(encryption_method)
            sub_encryptor = Encryptor(method=subtitle_crypto.method, key=subtitle_crypto.key, iv=subtitle_crypto.iv)
            encrypted_sub = sub_encryptor.encrypt(sub.data)
            att = SubtitleAttachment(frontend_id=frontend_id, data=encrypted_sub, language=sub.language, is_forced=sub.is_forced, crypto=subtitle_crypto)
            state.expected_subtitles += 1
            req = self._builder.flush_if_needed(att)
            if req:
                yield req
            self._builder.add(att)

        file_encryptor = Encryptor(method=file_crypto.method, key=file_crypto.key, iv=file_crypto.iv)

        offset = 0
        sequence = 1
        file_size = path.stat().st_size
        max_size = self._builder.ctx.max_size
        overall_crc = 0

        with open(path, "rb") as f:
            while offset < file_size:
                remaining_request = self._builder.remaining_size()
                remaining_file = file_size - offset

                if remaining_request < max_size // 3 < remaining_file:
                    req = self._builder.flush()
                    if req:
                        yield req
                    continue

                take = min(remaining_request, remaining_file)
                raw_chunk = f.read(take)
                overall_crc = zlib.crc32(raw_chunk, overall_crc)
                chunk_crc = zlib.crc32(raw_chunk)

                if not raw_chunk:
                    break

                encrypted = file_encryptor.encrypt(raw_chunk)
                att = ChunkAttachment(frontend_id=frontend_id, data=encrypted, sequence=sequence, offset=offset, crypto=file_crypto, crc=chunk_crc)
                state.expected_chunks += 1
                req = self._builder.flush_if_needed(att)
                if req:
                    yield req
                self._builder.add(att)

                offset += len(raw_chunk)
                sequence += 1

        state.artifacts.file_crc = overall_crc

        req = self._builder.flush()
        if req:
            yield req

        # except Exception as err:
        #     state.status = FileUploadStatus.FAILED
        #     state.error = err
