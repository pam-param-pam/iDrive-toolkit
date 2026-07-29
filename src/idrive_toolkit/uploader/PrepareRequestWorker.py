import logging
import time
import uuid
import zlib
from collections import defaultdict
from pathlib import Path
from queue import Full, Queue
from typing import Iterator

from .models import DiscordAttachment, DiscordRequest, UploadInput, UploadFileState, Crypto, ThumbnailAttachment, ChunkAttachment, FileUploadStatus, FileArtifacts, \
    ResponsePayload, SubtitleAttachment
from .extractor import get_file_extension, _run_ffprobe, _is_type, extract_video_metadata_if_needed, extract_thumbnail_if_needed, extract_subtitles_if_needed
from ..uploader.Encryptor import Encryptor
from ..uploader.UploadContext import UploadContext

logger = logging.getLogger("iDrive")


class FileProfiler:
    def __init__(self, file_name: str):
        self.file_name = file_name
        self.times = defaultdict(float)
        self.counts = defaultdict(int)

    def measure(self, name):
        class _Ctx:
            def __enter__(_self):
                _self.start = time.perf_counter()
                return _self

            def __exit__(_self, exc_type, exc, tb):
                duration = time.perf_counter() - _self.start
                self.times[name] += duration
                self.counts[name] += 1

        return _Ctx()

    def report(self):
        total = sum(self.times.values())
        logger.debug(f"\n=== FILE PROFILE: {self.file_name} ===")
        for k, v in sorted(self.times.items(), key=lambda x: -x[1]):
            count = self.counts[k]
            avg = v / count if count else 0
            pct = (v / total * 100) if total else 0
            logger.debug(f"{k:20} total={v:8.2f}s  avg={avg:6.4f}s  calls={count:6d}  {pct:5.1f}%")


class _RequestBuilder:
    def __init__(self, ctx: UploadContext):
        self.ctx = ctx
        self.attachments: list[DiscordAttachment] = []
        self.total_size = 0

    def can_fit(self, attachment: DiscordAttachment) -> bool:
        return len(self.attachments) < self.ctx.max_attachments and self.total_size + attachment.size <= self.ctx.max_size

    def add(self, attachment: DiscordAttachment) -> None:
        if attachment.size > self.ctx.max_size:
            raise RuntimeError(
                f"{attachment.__class__.__name__} size {attachment.size} exceeds max Discord content size {self.ctx.max_size}"
            )
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
    def __init__(self, input_queue: Queue[UploadInput], upload_queue: Queue[DiscordRequest], response_queue: Queue[ResponsePayload], ctx: UploadContext):
        self._input_queue = input_queue
        self._upload_queue = upload_queue
        self.response_queue = response_queue
        self._builder = _RequestBuilder(ctx)
        self.ctx = ctx

    def run(self) -> None:
        while True:
            item = self._input_queue.get()
            if item is None:
                self._input_queue.task_done()
                break

            initial_state_ids = set(self.ctx.get_all_states())
            try:
                for request in self.prepare_upload(item):
                    if not self._put_until_stopped(self._upload_queue, request):
                        break

            except Exception as e:
                logger.exception(f"Failed to prepare request on {item.path}")
                self.ctx.add_error(e)
                self._fail_new_request_states(initial_state_ids, e)

            finally:
                self.ctx.complete_upload_request()
                self._input_queue.task_done()

        req = self._builder.flush()
        if req:
            self._put_until_stopped(self._upload_queue, req)

    def prepare_upload(self, input_item: UploadInput) -> Iterator[DiscordRequest]:
        if self.ctx.stop_requested.is_set():
            return

        path = input_item.path
        parent = input_item.parent
        lock_from_id = input_item.lock_from_id
        prof = FileProfiler(path.name)

        if path.is_dir():
            new_parent = parent.create_subfolder(path.name)
            for child in path.iterdir():
                if self.ctx.stop_requested.is_set():
                    return
                yield from self.prepare_upload(UploadInput(path=child, parent=new_parent, lock_from_id=lock_from_id))
            return

        # ---- precompute stat once ----
        stat = path.stat()
        file_size = stat.st_size
        created_at = int(stat.st_mtime * 1000)
        extension = get_file_extension(path.name)

        frontend_id = str(uuid.uuid4())

        state = UploadFileState()
        self.ctx.register(frontend_id, state)

        self.ctx.set_status(frontend_id, FileUploadStatus.SCANNING)

        encryption_method = self.ctx.encryption_method

        probe = None
        duration = None
        video_metadata = None

        is_video = _is_type(self.ctx.extensions, extension, "Video")
        if is_video:
            probe = _run_ffprobe(str(path), self.ctx.extensions, extension)

            with prof.measure("video metadata"):
                video_metadata, duration = extract_video_metadata_if_needed(self.ctx.extensions, extension, path, probe)

        with prof.measure("crypto"):
            file_crypto = Crypto.generate(encryption_method)

        self.ctx.set_artifacts(frontend_id, FileArtifacts(
            frontend_id=frontend_id,
            name=path.name,
            extension=extension,
            created_at=created_at,
            size=file_size,
            parent_id=parent.id,
            lock_from_id=lock_from_id,
            parent_password=parent.password if parent.is_locked else None,
            encryption_method=encryption_method,
            file_crypto=file_crypto,
            duration=duration,
            video_metadata=video_metadata,
            local_path=str(path)
        ))

        if file_size == 0:
            self._put_until_stopped(
                self.response_queue,
                ResponsePayload(
                    response=None,
                    request=None,
                    frontend_id=frontend_id,
                    is_empty=True,
                )
            )
            return

        with prof.measure("thumbnail"):
            thumbnail = extract_thumbnail_if_needed(self.ctx.extensions, extension, path)
            if thumbnail:
                thumbnail_crypto = Crypto.generate(encryption_method)
                thumb_encryptor = Encryptor(method=thumbnail_crypto.method, key=thumbnail_crypto.key, iv=thumbnail_crypto.iv)
                encrypted_thumb = thumb_encryptor.encrypt(thumbnail.data)
                att = ThumbnailAttachment(frontend_id=frontend_id, data=encrypted_thumb, crypto=thumbnail_crypto)
                if not self._should_skip_oversized_extracted_attachment(att, path):
                    self.ctx.set_expected_thumbnail(frontend_id, True)

                    req = self._builder.flush_if_needed(att)
                    if req:
                        yield req
                    self._builder.add(att)

        if is_video:
            with prof.measure("subtitles"):
                for sub in extract_subtitles_if_needed(self.ctx.extensions, extension, path, probe):
                    subtitle_crypto = Crypto.generate(encryption_method)
                    sub_encryptor = Encryptor(method=subtitle_crypto.method, key=subtitle_crypto.key, iv=subtitle_crypto.iv)
                    encrypted_sub = sub_encryptor.encrypt(sub.data)
                    att = SubtitleAttachment(frontend_id=frontend_id, data=encrypted_sub, language=sub.language, is_forced=sub.is_forced, crypto=subtitle_crypto)
                    self.ctx.increment_expected_subtitles(frontend_id)

                    if self._should_skip_oversized_extracted_attachment(att, path):
                        with self.ctx.states[frontend_id].lock:
                            self.ctx.states[frontend_id].expected_subtitles -= 1
                        continue

                    req = self._builder.flush_if_needed(att)
                    if req:
                        yield req
                    self._builder.add(att)

        # ---- file encryption ----
        file_encryptor = Encryptor(
            method=file_crypto.method,
            key=file_crypto.key,
            iv=file_crypto.iv
        )

        offset = 0
        sequence = 1
        max_size = self.ctx.max_size
        overall_crc = 0

        with open(path, "rb") as f:
            while offset < file_size:
                if self.ctx.stop_requested.is_set():
                    return

                remaining_request = self._builder.remaining_size()
                remaining_file = file_size - offset

                # ---- PRE-FLUSH (like JS) ----
                if (remaining_request < max_size // 3 < remaining_file) or len(self._builder.attachments) >= self.ctx.max_attachments:
                    with prof.measure("flush"):
                        req = self._builder.flush()
                    if req:
                        yield req
                    continue

                # ---- DECIDE CHUNK SIZE ----
                take = min(remaining_request, remaining_file)

                if take <= 0:
                    req = self._builder.flush()
                    if req:
                        yield req
                    continue

                # ---- READ ----
                with prof.measure("file_read"):
                    raw_chunk = f.read(take)

                if not raw_chunk:
                    break

                # ---- CRC ----
                with prof.measure("crc"):
                    overall_crc = zlib.crc32(raw_chunk, overall_crc)
                    chunk_crc = zlib.crc32(raw_chunk)

                # ---- ENCRYPT ----
                with prof.measure("encrypt"):
                    encrypted = file_encryptor.encrypt(raw_chunk)

                # ---- BUILD ATTACHMENT ----
                att = ChunkAttachment(
                    frontend_id=frontend_id,
                    data=encrypted,
                    sequence=sequence,
                    offset=offset,
                    crypto=file_crypto,
                    crc=chunk_crc
                )

                # ---- ADD ----
                with prof.measure("builder"):
                    self._builder.add(att)

                self.ctx.increment_expected_chunks(frontend_id)

                offset += len(raw_chunk)
                sequence += 1

                # ---- POST-FLUSH (like JS "after push") ----
                if self._builder.total_size >= self.ctx.max_size or \
                        len(self._builder.attachments) >= self.ctx.max_attachments - 1:
                    with prof.measure("flush"):
                        req = self._builder.flush()
                    if req:
                        yield req

        self.ctx.set_crc(frontend_id, overall_crc)

        prof.report()

        req = self._builder.flush()
        if req:
            yield req

    def _put_until_stopped(self, queue: Queue, item) -> bool:
        while not self.ctx.stop_requested.is_set():
            try:
                queue.put(item, timeout=0.5)
                return True
            except Full:
                continue
        return False

    def _fail_new_request_states(self, initial_state_ids: set[str], error: Exception) -> None:
        for file_id, state in self.ctx.get_all_states().items():
            if file_id in initial_state_ids:
                continue

            with state.lock:
                if state.is_terminal():
                    continue
                state.error = error
                state.status = FileUploadStatus.FAILED

    def _should_skip_oversized_extracted_attachment(self, attachment: DiscordAttachment, path: Path) -> bool:
        if attachment.size <= self.ctx.max_size:
            return False

        logger.warning(
            "[PrepareRequestWorker] Skipping oversized extracted attachment type=%s file=%r local_path=%r size=%s max_content_size=%s",
            attachment.__class__.__name__,
            path.name,
            str(path),
            attachment.size,
            self.ctx.max_size,
        )
        return True
