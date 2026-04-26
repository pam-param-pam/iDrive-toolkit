import time
import traceback
import uuid
import zlib
from collections import defaultdict
from queue import Queue
from typing import Iterator

from .Extractor import extract_thumbnail_if_needed, get_file_extension, extract_video_metadata_if_needed, extract_subtitles_if_needed, _run_ffprobe, _is_type
from .models import DiscordAttachment, DiscordRequest, UploadInput, UploadFileState, Crypto, ThumbnailAttachment, SubtitleAttachment, ChunkAttachment, FileUploadStatus, FileArtifacts, \
    BackendFile, ResponsePayload
from ..uploader.Encryptor import Encryptor
from ..uploader.UploadContext import UploadContext


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

        print(f"\n=== FILE PROFILE: {self.file_name} ===")
        for k, v in sorted(self.times.items(), key=lambda x: -x[1]):
            count = self.counts[k]
            avg = v / count if count else 0
            pct = (v / total * 100) if total else 0
            print(f"{k:20} total={v:8.2f}s  avg={avg:6.4f}s  calls={count:6d}  {pct:5.1f}%")


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

            try:
                for request in self.prepare_upload(item):
                    self._upload_queue.put(request)

            except Exception as e:
                print(f"[PrepareRequestWorker] FAILED on {item.path}: {e!r}")
                traceback.print_exc()

            finally:
                self._input_queue.task_done()

        print("Prepare request worker is done")
        req = self._builder.flush()
        if req:
            self._upload_queue.put(req)

    def prepare_upload(self, input_item: UploadInput) -> Iterator[DiscordRequest]:
        path = input_item.path
        parent = input_item.parent
        lock_from_id = input_item.lock_from_id
        prof = FileProfiler(path.name)

        if path.is_dir():
            new_parent = parent.create_subfolder(path.name)
            for child in path.iterdir():
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
            parent_password=parent.get_password(),
            encryption_method=encryption_method,
            file_crypto=file_crypto,
            duration=duration,
            video_metadata=video_metadata
        ))

        if file_size == 0:
            self.response_queue.put(ResponsePayload(
                    response=None,
                    request=None,
                    frontend_id=frontend_id,
                    is_empty=True,
                ))
            return

        with prof.measure("thumbnail"):
            thumbnail = extract_thumbnail_if_needed(self.ctx.extensions, extension, path)
            if thumbnail:
                thumbnail_crypto = Crypto.generate(encryption_method)
                thumb_encryptor = Encryptor(method=thumbnail_crypto.method, key=thumbnail_crypto.key, iv=thumbnail_crypto.iv)
                encrypted_thumb = thumb_encryptor.encrypt(thumbnail.data)
                att = ThumbnailAttachment(frontend_id=frontend_id, data=encrypted_thumb, crypto=thumbnail_crypto)
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
                remaining_request = self._builder.remaining_size()
                remaining_file = file_size - offset

                # ---- PRE-FLUSH (like JS) ----
                if (remaining_request < max_size // 3 < remaining_file) \
                        or len(self._builder.attachments) >= self.ctx.max_attachments:
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
