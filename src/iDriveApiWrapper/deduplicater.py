from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models.File import File
from .models.Folder import Folder
from .utils import common


@dataclass(frozen=True)
class DuplicateGroup:
    crc: int
    size: int
    files: list[File]


class Deduplicater:
    def find_duplicates(self, folder: Folder, recursive: bool = False) -> list[DuplicateGroup]:
        files_by_size_crc: dict[tuple[int, int], list[File]] = defaultdict(list)

        for file in self._iter_files(folder, recursive=recursive):
            crc = file.crc
            size = file.size
            if crc is None or size is None:
                continue
            files_by_size_crc[(int(size), int(crc))].append(file)

        return [
            DuplicateGroup(size=size, crc=crc, files=files)
            for (size, crc), files in files_by_size_crc.items()
            if len(files) > 1
        ]

    def interactive(self, folder: Folder, recursive: bool = False) -> None:
        groups = self.find_duplicates(folder, recursive=recursive)
        if not groups:
            print("No duplicate CRC groups found.")
            return

        print(f"Found {len(groups)} duplicate CRC groups.")

        for group_index, group in enumerate(groups, 1):
            while True:
                self._print_group(group_index, len(groups), group)
                self._print_actions()

                raw = input("dedupe> ").strip()
                if not raw:
                    continue

                parts = raw.split()
                command = parts[0].lower()
                args = parts[1:]

                if command in ("s", "skip", "next"):
                    break

                if command in ("d", "delete"):
                    files = self._select_many(group, args)
                    if files:
                        common.move_to_trash(files)
                        break
                    continue

                print("Unknown action.")

    def _iter_files(self, folder: Folder, recursive: bool) -> Iterable[File]:
        for item in folder.children:
            if isinstance(item, File):
                yield item
                continue

            if recursive and isinstance(item, Folder):
                yield from self._iter_files(item, recursive=True)

    def _print_group(self, group_index: int, group_count: int, group: DuplicateGroup) -> None:
        print()
        print(f"Group {group_index}/{group_count} SIZE={group.size} CRC={group.crc} duplicates={len(group.files)}")
        print(f"{'#':>3} {'NAME':<38} {'SIZE':>12} {'ID':<24}")
        print("-" * 84)

        for index, file in enumerate(group.files, 1):
            name = self._truncate(file.name, 38)
            size = file.size
            print(f"{index:>3} {name:<38} {size:>12} {file.id:<24}")
            print(f"    download : {self._inline_url(file.download_url)}")
            if file.thumbnail_url:
                print(f"    thumbnail: {self._inline_url(file.thumbnail_url)}")

    def _print_actions(self) -> None:
        print()
        print("Actions:")
        print("  skip          next duplicate group")
        print("  delete <n...> move selected files to trash")

    def _select_many(self, group: DuplicateGroup, args: list[str]) -> list[File]:
        if not args:
            print("Missing file number.")
            return []

        files = []
        for arg in args:
            if not arg.isdigit():
                print(f"Invalid file number: {arg}")
                return []

            index = int(arg) - 1
            if not 0 <= index < len(group.files):
                print(f"File number out of range: {arg}")
                return []

            files.append(group.files[index])

        return files

    def _inline_url(self, url: str | None) -> str:
        if not url:
            return "-"

        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["inline"] = "True"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    def _truncate(self, value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[:max_len - 3] + "..."
