from __future__ import annotations

from pathlib import Path
from typing import Callable

from .DiffEngine import DiffEntry, DiffResult, NodeStatus
from .Syncer import ChangedFileStrategy
from .formatting import conflict_summary, entry_local_label, entry_name, entry_remote_label, remote_label
from ..models.Folder import Folder


class SyncInteractiveConsole:
    def __init__(self, syncer):
        self.syncer = syncer

    def run(self, local_root: Path, remote_root: Folder | str) -> None:
        show_same = False
        stack: list[tuple[Path, Folder | str]] = [(Path(local_root), remote_root)]

        while stack:
            current_local_root, current_remote_root = stack[-1]
            result = self.syncer.diff(current_local_root, current_remote_root)
            entries = self.interactive_entries(result, show_same=show_same)

            self._print_interactive_table(current_local_root, current_remote_root, result, entries)
            self._print_interactive_actions(show_same)

            raw_choice = input("> ").strip().lower()
            choice_parts = raw_choice.split()
            choice = choice_parts[0] if choice_parts else ""
            choice_args = choice_parts[1:]

            if choice in ("q", "quit", "exit"):
                return

            if choice in ("b", "back"):
                if len(stack) == 1:
                    print("Already at top level.")
                else:
                    stack.pop()
                continue

            if choice in ("same", "toggle"):
                show_same = not show_same
                continue

            if choice in ("all", "sync"):
                if result.conflicts:
                    print(conflict_summary(result.conflicts))
                    continue

                strategy = self._prompt_changed_file_strategy()
                if self._confirm("Apply all changes at this level and recurse into changed folders?"):
                    self.syncer.sync_one_level(current_local_root, current_remote_root, strategy=strategy)
                continue

            if choice in ("local", "upload"):
                self._apply_entries(result.only_local, self.syncer._handle_only_local, "Upload local-only entries?")
                continue

            if choice in ("remote", "download"):
                self._apply_entries(result.only_remote, self.syncer._handle_only_remote, "Download remote-only entries?")
                continue

            if choice in ("delete_local", "del_local", "rm_local"):
                self._delete_local_entries(self._select_entries(entries, choice_args))
                continue

            if choice in ("delete_remote", "del_remote", "trash_remote"):
                self._delete_remote_entries(self._select_entries(entries, choice_args))
                continue

            if choice in ("changed", "resolve"):
                if not result.changed:
                    print("No changed entries. Conflict entries require manual rename/delete/move resolution.")
                    continue

                strategy = self._prompt_changed_file_strategy()
                self._apply_entries(
                    result.changed,
                    lambda entry: self.syncer._handle_changed(entry, strategy=strategy),
                    "Resolve changed entries?",
                )
                continue

            if choice in ("conflict", "conflicts"):
                if not result.conflicts:
                    print("No conflict entries.")
                else:
                    print(conflict_summary(result.conflicts))
                continue

            if choice.isdigit():
                index = int(choice) - 1
                if not 0 <= index < len(entries):
                    print("Invalid number.")
                    continue

                entry = entries[index]
                self._handle_interactive_entry(entry, stack)
                continue

            print("Unknown action.")

    @staticmethod
    def interactive_entries(result: DiffResult, *, show_same: bool) -> list[DiffEntry]:
        entries = []
        entries.extend(result.only_local)
        entries.extend(result.only_remote)
        entries.extend(result.changed)
        entries.extend(result.conflicts)
        if show_same:
            entries.extend(result.same)
        return entries

    def _select_entries(self, entries: list[DiffEntry], args: list[str]) -> list[DiffEntry]:
        if not args:
            return entries

        selected = []
        for arg in args:
            if not arg.isdigit():
                print(f"Invalid number: {arg}")
                return []

            index = int(arg) - 1
            if not 0 <= index < len(entries):
                print(f"Invalid number: {arg}")
                return []

            selected.append(entries[index])

        return selected

    def _handle_interactive_entry(self, entry: DiffEntry, stack: list[tuple[Path, Folder | str]]) -> None:
        if entry.status == NodeStatus.ONLY_LOCAL:
            if self._confirm(f"Upload {entry_name(entry)}?"):
                self.syncer._handle_only_local(entry)
            return

        if entry.status == NodeStatus.ONLY_REMOTE:
            if self._confirm(f"Download {entry_name(entry)}?"):
                self.syncer._handle_only_remote(entry)
            return

        if entry.status == NodeStatus.CHANGED:
            if entry.is_folder:
                stack.append((self.syncer._require_local_path(entry), self.syncer._require_remote_id(entry)))
                return

            strategy = self._prompt_changed_file_strategy()
            if self._confirm(f"Resolve changed file {entry_name(entry)} with {strategy.value}?"):
                self.syncer._handle_changed(entry, strategy=strategy)
            return

        if entry.status == NodeStatus.SAME and entry.is_folder:
            stack.append((self.syncer._require_local_path(entry), self.syncer._require_remote_id(entry)))
            return

        if entry.status == NodeStatus.CONFLICT:
            print(entry.message or "Conflict requires manual resolution.")
            self._print_entry_delete_actions(entry)
            return

        print("No action for this entry.")

    def _print_entry_delete_actions(self, entry: DiffEntry) -> None:
        actions = []
        if entry.local is not None:
            actions.append("delete_local <number>")
        if entry.remote is not None:
            actions.append("delete_remote <number>")
        if actions:
            print("Delete actions: " + ", ".join(actions))

    def _apply_entries(self, entries: list[DiffEntry], handler: Callable[[DiffEntry], None], prompt: str) -> None:
        if not entries:
            print("Nothing to apply.")
            return

        if not self._confirm(prompt):
            return

        for entry in entries:
            handler(entry)

    def _print_interactive_table(
        self,
        local_root: Path,
        remote_root: Folder | str,
        result: DiffResult,
        entries: list[DiffEntry],
    ) -> None:
        print()
        print(f"Local : {local_root}")
        print(f"Remote: {remote_label(remote_root)}")
        print(
            f"Diff  : local={len(result.only_local)} "
            f"remote={len(result.only_remote)} "
            f"changed={len(result.changed)} "
            f"conflicts={len(result.conflicts)} "
            f"same={len(result.same)}"
        )

        if not entries:
            print("No visible differences.")
            return

        print(f"{'#':>3} {'STATUS':<12} {'KIND':<6} {'NAME':<34} {'LOCAL':<54} {'REMOTE':<22} {'DETAIL'}")
        print("-" * 158)

        for index, entry in enumerate(entries, 1):
            status = entry.status.value
            kind = "dir" if entry.is_folder else "file"
            name = self._truncate(entry_name(entry), 34)
            local_path = self._truncate(entry_local_label(entry), 54)
            remote_id = self._truncate(entry_remote_label(entry), 22)
            detail = self._truncate(entry.message or "", 28)
            print(f"{index:>3} {status:<12} {kind:<6} {name:<34} {local_path:<54} {remote_id:<22} {detail}")

    def _print_interactive_actions(self, show_same: bool) -> None:
        same_label = "hide same" if show_same else "show same"
        print()
        print("Actions:")
        print("  all       sync local-only, remote-only, and changed folders")
        print("  local     upload local-only entries")
        print("  remote    download remote-only entries")
        print("  delete_local  delete visible local entries")
        print("  delete_remote move visible remote entries to trash")
        print("  changed   resolve changed entries")
        print("  conflicts inspect duplicate-name conflicts")
        print(f"  same      {same_label}")
        print("  <number>  open folder or apply one entry")
        print("  back      previous folder")
        print("  quit      exit")

    def _delete_local_entries(self, entries: list[DiffEntry]) -> None:
        local_entries = [entry for entry in entries if entry.local is not None]
        if not local_entries:
            print("No visible local entries to delete.")
            return

        if not self._confirm(f"Delete {len(local_entries)} visible local entries?"):
            return

        for entry in local_entries:
            self.syncer._delete_local_entry(entry)

    def _delete_remote_entries(self, entries: list[DiffEntry]) -> None:
        remote_entries = [entry for entry in entries if entry.remote is not None]
        if not remote_entries:
            print("No visible remote entries to move to trash.")
            return

        if not self._confirm(f"Move {len(remote_entries)} visible remote entries to trash?"):
            return

        for entry in remote_entries:
            self.syncer._delete_remote_entry(entry)

    def _prompt_changed_file_strategy(self) -> ChangedFileStrategy:
        print()
        print("Changed file strategy:")
        print("  1  newer           use newer modified timestamp")
        print("  2  upload_local    replace remote file with local")
        print("  3  download_remote replace local file with remote")
        print("  4  skip            leave changed files untouched")
        print("  5  error           stop on changed files")

        choices = {
            "1": ChangedFileStrategy.NEWER,
            "newer": ChangedFileStrategy.NEWER,
            "2": ChangedFileStrategy.UPLOAD_LOCAL,
            "upload_local": ChangedFileStrategy.UPLOAD_LOCAL,
            "upload": ChangedFileStrategy.UPLOAD_LOCAL,
            "3": ChangedFileStrategy.DOWNLOAD_REMOTE,
            "download_remote": ChangedFileStrategy.DOWNLOAD_REMOTE,
            "download": ChangedFileStrategy.DOWNLOAD_REMOTE,
            "4": ChangedFileStrategy.SKIP,
            "skip": ChangedFileStrategy.SKIP,
            "5": ChangedFileStrategy.ERROR,
            "error": ChangedFileStrategy.ERROR,
        }

        while True:
            choice = input("strategy> ").strip().lower()
            strategy = choices.get(choice)
            if strategy is not None:
                return strategy
            print("Invalid strategy.")

    def _confirm(self, prompt: str) -> bool:
        return input(f"{prompt} [y/N] ").strip().lower() == "y"

    def _truncate(self, value: str, max_len: int) -> str:
        if len(value) <= max_len:
            return value
        return value[:max_len - 3] + "..."
