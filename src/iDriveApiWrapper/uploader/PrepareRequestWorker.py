import uuid
from queue import Queue
from typing import Iterator

from .models import DiscordAttachment, DiscordRequest, UploadInput, UploadFileState, UploadFileStatus, Crypto, ThumbnailAttachment, SubtitleAttachment, ChunkAttachment
from ..uploader.Encryptor import Encryptor
from ..uploader.UploadContext import UploadContext
from ..uploader.VideoExtractor import extract_thumbnail_if_needed, extract_subtitles_if_needed


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

        file_id = uuid.uuid4()

        state = UploadFileState(expected_chunks=0, expected_subtitles=0, expected_thumbnail=0)
        state.status = UploadFileStatus.SCANNING
        self._file_states[file_id] = state

        method = self._builder.ctx.encryption_method

        thumbnail = extract_thumbnail_if_needed(path)
        if thumbnail:
            thumbnail_crypto = Crypto.generate(method)
            thumb_encryptor = Encryptor(method=thumbnail_crypto.method, key=thumbnail_crypto.key, iv=thumbnail_crypto.iv)
            encrypted_thumb = thumb_encryptor.encrypt(thumbnail.data)
            att = ThumbnailAttachment(frontend_id=file_id, data=encrypted_thumb, crypto=thumbnail_crypto)
            state.expected_thumbnail += 1
            req = self._builder.flush_if_needed(att)
            if req:
                yield req
            self._builder.add(att)

        for sub in extract_subtitles_if_needed(path):
            subtitle_crypto = Crypto.generate(method)
            sub_encryptor = Encryptor(method=subtitle_crypto.method, key=subtitle_crypto.key, iv=subtitle_crypto.iv)
            encrypted_sub = sub_encryptor.encrypt(sub.data)
            att = SubtitleAttachment(frontend_id=file_id, data=encrypted_sub, language=sub.language, is_forced=sub.is_forced, crypto=subtitle_crypto)
            state.expected_subtitles += 1
            req = self._builder.flush_if_needed(att)
            if req:
                yield req
            self._builder.add(att)

        file_crypto = Crypto.generate(method)
        file_encryptor = Encryptor(method=file_crypto.method, key=file_crypto.key, iv=file_crypto.iv)

        offset = 0
        sequence = 1
        file_size = path.stat().st_size
        max_size = self._builder.ctx.max_size

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
                if not raw_chunk:
                    break

                encrypted = file_encryptor.encrypt(raw_chunk)
                att = ChunkAttachment(frontend_id=file_id, data=encrypted, sequence=sequence, offset=offset, crypto=file_crypto)
                state.expected_chunks += 1
                req = self._builder.flush_if_needed(att)
                if req:
                    yield req
                self._builder.add(att)

                offset += len(raw_chunk)
                sequence += 1

        req = self._builder.flush()
        if req:
            yield req

        state.status = UploadFileStatus.READY
