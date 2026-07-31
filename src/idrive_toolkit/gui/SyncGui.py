from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import tkinter as tk
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageDraw, ImageFont, ImageTk

from ..exceptions import BackendMissingOrIncorrectResourcePasswordError
from ..syncer.BaseScanner import Node, NodeKind, NodeOrigin
from ..syncer.DiffEngine import DiffEntry, DiffResult, NodeStatus
from ..syncer.Syncer import ChangedFileStrategy, RenamedFileStrategy, SyncConflictError
from ..syncer.formatting import conflict_summary, entry_local_label, entry_name, entry_remote_label, remote_label
from ..syncer.progress import DiffProgress, DiffProgressPhase, TransferProgress
from ..syncer.transfer import SyncTransferCancelled
from ..models.Item import Item
from ..models.Folder import Folder
from .BreadcrumbsBar import BreadcrumbsBar
from .GuiUtils import apply_window_icon, file_icon_key, needs_resource_password, password_prompt_item, prompt_resource_password, set_windows_app_user_model_id
from .TransferStatusBar import TransferStatusBar


TK_STOP_EVENT = "break"
logger = logging.getLogger("iDrive")


class SyncGuiAlreadyOpenError(RuntimeError):
    pass


class SyncGui:
    _open_window: tk.Misc | None = None

    def __init__(self, syncer, local_root: Path, remote_root: Folder | str, parent: tk.Misc | None = None):
        if self.__class__._open_window is not None and self.__class__._open_window.winfo_exists():
            raise SyncGuiAlreadyOpenError("A sync window is already open.")

        self.syncer = syncer
        self.initial_local_root = Path(local_root).resolve()
        self.initial_remote_id = self.syncer.remote.normalize_id(remote_root)
        self.syncer.set_sync_boundary(self.initial_local_root, self.initial_remote_id)
        self.stack: list[tuple[Path, Folder | str]] = [(Path(local_root), remote_root)]
        self.show_same = False
        self.result = DiffResult()
        self.entries: list[DiffEntry] = []
        self._busy = False
        self._sort_column = "name"
        self._sort_reverse = False
        self._state_window = None
        self._state_table = None
        self._state_filter_failed = None
        self._state_rows: dict[str, dict[str, str]] = {}
        self._state_refresh_after_id = None
        self._state_sort_column = "added"
        self._state_sort_reverse = False
        self._attached_transfer = None
        self._owns_root = parent is None
        self._closed = False

        if self._owns_root:
            set_windows_app_user_model_id()
        self.root = tk.Tk() if self._owns_root else tk.Toplevel(parent)
        self.__class__._open_window = self.root
        self.root.report_callback_exception = self._show_callback_exception
        self.root.title("iDrive Interactive Sync")
        apply_window_icon(self.root)
        self.root.geometry("1300x760")
        self.root.minsize(1300, 560)

        self.icons = self._create_icons()
        self._build_widgets()
        self.syncer.set_transfer_progress_callback(self._queue_transfer_progress)
        self.syncer.set_status_callback(self._queue_status)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        root_item = self._current_remote_folder()
        if root_item is not None and needs_resource_password(root_item):
            if not self._prompt_resource_password(root_item):
                self.status_var.set("Password required")
                return
        self.refresh()

    def run(self) -> None:
        if self._owns_root:
            self.root.mainloop()
        else:
            self.root.wait_window()

    def _on_close(self) -> None:
        if self._closed:
            return
        was_busy = self._busy
        self._closed = True
        self.syncer.set_transfer_progress_callback(None)
        self.syncer.set_status_callback(None)
        self.syncer.abort_current_transfer()
        if not was_busy:
            self.syncer.clear_sync_boundary()
        if self.__class__._open_window is self.root:
            self.__class__._open_window = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _window_alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.root.winfo_exists())
        except tk.TclError:
            return False

    def _widget_exists(self, widget) -> bool:
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def _safe_after(self, callback) -> None:
        if self._closed:
            return

        def run_if_open():
            if self._window_alive():
                callback()

        try:
            self.root.after(0, run_if_open)
        except tk.TclError:
            pass

    def _show_callback_exception(self, exc_type, exc, tb) -> None:
        traceback.print_exception(exc_type, exc, tb)
        messagebox.showerror("Unhandled error inside the application", "".join(traceback.format_exception_only(exc_type, exc)).strip(), parent=self.root)

    def _build_widgets(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        top = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)

        ttk.Label(top, text="Local").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.local_var = tk.StringVar()
        ttk.Label(top, textvariable=self.local_var).grid(row=0, column=1, sticky="ew")

        ttk.Label(top, text="Remote").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self.remote_var = tk.StringVar()
        self.breadcrumbs = BreadcrumbsBar(top, self._navigate_breadcrumb)
        self.breadcrumbs.grid(row=1, column=1, sticky="ew")

        self.summary_var = tk.StringVar()
        ttk.Label(top, textvariable=self.summary_var).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

        actions = ttk.Frame(self.root, padding=(10, 4))
        actions.grid(row=1, column=0, sticky="ew")

        self.buttons: list[ttk.Button] = []
        self.selection_buttons: dict[str, ttk.Button] = {}

        view_actions = ttk.LabelFrame(actions, text="View", padding=(6, 4))
        view_actions.pack(side="left", padx=(0, 8))
        self.back_button = self._button(view_actions, "Back", self.back, "action_back")
        self.back_button.pack(side="left", padx=(0, 6))
        self._button(view_actions, "Refresh", self.refresh, "action_refresh").pack(side="left", padx=(0, 6))
        self.same_button = self._button(view_actions, "Show same", self.toggle_same, "action_same")
        self.same_button.pack(side="left")

        sync_actions = ttk.LabelFrame(actions, text="Sync", padding=(6, 4))
        sync_actions.pack(side="left", padx=(0, 8))
        self._selection_button(sync_actions, "sync_all", "Sync All", self.sync_all, "action_sync").pack(side="left", padx=(0, 6))
        self._selection_button(sync_actions, "upload", "Upload Local", self.upload_local, "action_upload").pack(side="left", padx=(0, 6))
        self._selection_button(sync_actions, "create_remote_folder", "Create Remote Folder", self.create_selected_remote_folder, "action_create_folder").pack(side="left", padx=(0, 6))
        self._selection_button(sync_actions, "download", "Download Remote", self.download_remote, "action_download").pack(side="left", padx=(0, 6))
        self._selection_button(sync_actions, "resolve", "Resolve Changed", self.resolve_changed, "action_resolve").pack(side="left")

        item_actions = ttk.LabelFrame(actions, text="Item", padding=(6, 4))
        item_actions.pack(side="left")
        self._selection_button(item_actions, "details", "Details", self.show_selected_details, "action_details").pack(side="left", padx=(0, 6))
        self._selection_button(item_actions, "delete_local", "Delete Local", self.delete_local, "action_delete").pack(side="left", padx=(0, 6))
        self._selection_button(item_actions, "trash_remote", "Trash Remote", self.trash_remote, "action_trash").pack(side="left")

        table_frame = ttk.Frame(self.root, padding=(10, 4))
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("name", "status", "size", "local", "remote")
        ttk.Style(self.root).configure("Treeview", rowheight=34)
        self.table = ttk.Treeview(table_frame, columns=columns, show="tree headings", selectmode="extended")
        self._configure_sort_headings()

        self.table.column("#0", width=56, minwidth=56, stretch=False, anchor="center")
        self.table.column("name", width=260)
        self.table.column("status", width=110, stretch=False)
        self.table.column("size", width=110, stretch=False, anchor="e")
        self.table.column("local", width=360)
        self.table.column("remote", width=260)
        self.table.grid(row=0, column=0, sticky="nsew")
        self.table.bind("<Button-1>", self._clear_selection_on_empty_click, add="+")
        self.table.bind("<Double-1>", self._on_double_click)
        self.table.bind("<Button-3>", self._show_context_menu)
        self.table.bind("<Escape>", self._clear_selection)
        self.table.bind("<<TreeviewSelect>>", lambda _event: self._update_selection_actions())
        self.table.tag_configure("disabled", foreground="#8a8f98", background="#f1f3f5")

        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.table.configure(yscrollcommand=scroll.set)

        bottom = ttk.Frame(self.root, padding=(10, 4, 10, 10))
        bottom.grid(row=3, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)
        self.transfer_status = TransferStatusBar(
            bottom,
            abort_icon=self.icons["action_cancel"],
            abort_command=self.abort_transfer,
            pause_icon=self.icons["action_pause"],
            resume_icon=self.icons["action_resume"],
            pause_command=self.toggle_transfer_pause,
        )
        self.status_var = self.transfer_status.status_var
        self.progress_var = self.transfer_status.progress_var
        self.transfer_states_button = ttk.Button(bottom, text="File States", command=self.show_transfer_states, state="disabled")
        self.transfer_states_button.grid(row=0, column=4, sticky="e", padx=(6, 0))

    def _button(self, parent, text: str, command, icon_key: str | None = None) -> ttk.Button:
        image = self.icons.get(icon_key) if icon_key else None
        button = ttk.Button(parent, text=text, image=image, compound="left", command=command)
        self.buttons.append(button)
        return button

    def _selection_button(self, parent, action: str, text: str, command, icon_key: str) -> ttk.Button:
        button = self._button(parent, text, command, icon_key)
        self.selection_buttons[action] = button
        return button

    def _create_icons(self) -> dict[str, tk.PhotoImage]:
        icons = {
            "folder": self._create_folder_icon(),
            "action_refresh": self._create_action_icon("refresh"),
            "action_back": self._create_action_icon("back"),
            "action_same": self._create_action_icon("same"),
            "action_sync": self._create_action_icon("sync"),
            "action_upload": self._create_action_icon("upload"),
            "action_create_folder": self._create_action_icon("create_folder"),
            "action_download": self._create_action_icon("download"),
            "action_resolve": self._create_action_icon("resolve"),
            "action_details": self._create_action_icon("details"),
            "action_delete": self._create_action_icon("delete"),
            "action_trash": self._create_action_icon("trash"),
            "action_open": self._create_action_icon("open"),
            "action_cancel": self._create_action_icon("cancel"),
            "action_pause": self._create_action_icon("pause"),
            "action_resume": self._create_action_icon("resume"),
        }
        for key, color, label in (
            ("file", "#6b7785", ""),
            ("file_image", "#2f9e44", "IMG"),
            ("file_video", "#7950f2", "VID"),
            ("file_audio", "#f08c00", "AUD"),
            ("file_text", "#1971c2", "TXT"),
            ("file_archive", "#5f3dc4", "ZIP"),
            ("file_code", "#0b7285", "{}"),
            ("file_pdf", "#e03131", "PDF"),
        ):
            icons[key] = self._create_file_icon(color, label)

        return icons

    def _create_file_icon(self, accent: str, label: str) -> tk.PhotoImage:
        image = Image.new("RGBA", (28, 28), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 3, 22, 25), radius=2, fill="#ffffff", outline="#9aa7b4")
        draw.polygon([(16, 3), (22, 9), (16, 9)], fill="#dfe8f2", outline="#9aa7b4")
        draw.rounded_rectangle((6, 17, 22, 25), radius=2, fill=accent)
        if label:
            font = self._icon_font(8)
            bbox = draw.textbbox((0, 0), label, font=font)
            x = 14 - (bbox[2] - bbox[0]) // 2
            draw.text((x, 17), label, fill="#ffffff", font=font)
        return ImageTk.PhotoImage(image)

    def _create_folder_icon(self) -> tk.PhotoImage:
        image = Image.new("RGBA", (28, 28), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((3, 8, 24, 24), radius=2, fill="#d9a928", outline="#9b7418")
        draw.rounded_rectangle((3, 5, 13, 11), radius=2, fill="#efc85a", outline="#b0831e")
        draw.rounded_rectangle((4, 10, 25, 25), radius=2, fill="#f4c542", outline="#b0831e")
        draw.rectangle((5, 11, 24, 14), fill="#ffd86b")
        return ImageTk.PhotoImage(image)

    def _create_action_icon(self, kind: str) -> tk.PhotoImage:
        image = Image.new("RGBA", (18, 18), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        blue = "#2563eb"
        green = "#2f9e44"
        red = "#d92d20"
        gray = "#475467"

        if kind == "refresh":
            draw.arc((3, 3, 15, 15), 35, 305, fill=blue, width=2)
            draw.polygon([(14, 3), (15, 8), (11, 6)], fill=blue)
        elif kind == "back":
            draw.polygon([(5, 9), (10, 4), (10, 7), (15, 7), (15, 11), (10, 11), (10, 14)], fill=gray)
        elif kind == "same":
            draw.ellipse((3, 5, 15, 13), outline=blue, width=2)
            draw.ellipse((7, 7, 11, 11), fill=blue)
        elif kind == "sync":
            draw.arc((3, 2, 15, 10), 15, 180, fill=blue, width=2)
            draw.arc((3, 8, 15, 16), 195, 360, fill=green, width=2)
            draw.polygon([(4, 4), (7, 2), (7, 6)], fill=blue)
            draw.polygon([(14, 14), (11, 16), (11, 12)], fill=green)
        elif kind == "upload":
            draw.line((9, 14, 9, 4), fill=green, width=3)
            draw.polygon([(4, 8), (9, 3), (14, 8)], fill=green)
            draw.line((4, 15, 14, 15), fill=gray, width=2)
        elif kind == "create_folder":
            draw.rounded_rectangle((2, 7, 14, 15), radius=2, fill="#f4c542", outline="#b0831e")
            draw.rounded_rectangle((2, 5, 8, 9), radius=1, fill="#efc85a", outline="#b0831e")
            draw.line((13, 4, 13, 12), fill=green, width=2)
            draw.line((9, 8, 17, 8), fill=green, width=2)
        elif kind == "download":
            draw.line((9, 4, 9, 14), fill=blue, width=3)
            draw.polygon([(4, 10), (9, 15), (14, 10)], fill=blue)
            draw.line((4, 15, 14, 15), fill=gray, width=2)
        elif kind == "resolve":
            draw.line((5, 13, 13, 5), fill=gray, width=3)
            draw.ellipse((3, 11, 7, 15), fill=gray)
            draw.rectangle((11, 3, 15, 7), fill=blue)
        elif kind == "details":
            draw.ellipse((3, 3, 15, 15), outline=blue, width=2)
            draw.rectangle((8, 7, 10, 13), fill=blue)
            draw.rectangle((8, 4, 10, 6), fill=blue)
        elif kind == "delete":
            draw.line((5, 5, 13, 13), fill=red, width=3)
            draw.line((13, 5, 5, 13), fill=red, width=3)
        elif kind == "trash":
            draw.rectangle((5, 7, 13, 15), outline=red, width=2)
            draw.line((4, 6, 14, 6), fill=red, width=2)
            draw.line((7, 4, 11, 4), fill=red, width=2)
        elif kind == "open":
            draw.rounded_rectangle((3, 5, 15, 14), radius=2, fill="#f4c542", outline="#b0831e")
            draw.polygon([(10, 8), (16, 8), (16, 14), (10, 14)], fill="#ffffff", outline=blue)
        elif kind == "cancel":
            draw.ellipse((3, 3, 15, 15), outline=red, width=2)
            draw.line((6, 6, 12, 12), fill=red, width=2)
            draw.line((12, 6, 6, 12), fill=red, width=2)
        elif kind == "pause":
            draw.rectangle((5, 4, 7, 14), fill=gray)
            draw.rectangle((11, 4, 13, 14), fill=gray)
        elif kind == "resume":
            draw.polygon([(6, 4), (14, 9), (6, 14)], fill=green)

        return ImageTk.PhotoImage(image)

    def _icon_font(self, size: int):
        try:
            return ImageFont.truetype("segoeui.ttf", size)
        except OSError:
            return ImageFont.load_default()

    def refresh(self) -> None:
        self._load_current_level(clear_cache=True)

    def _load_current_level(self, *, clear_cache: bool, password_items: list[Item] | None = None) -> None:
        local_root, remote_root = self.stack[-1]

        def work():
            if clear_cache:
                self.syncer.clear_memory_cache(remote_root)
            return self.syncer.diff(local_root, remote_root, progress=self._queue_diff_progress)

        def done(result: DiffResult):
            self.result = result
            self.entries = self._display_entries(result)
            self._render()

        status = "Refreshing diff..." if clear_cache else "Scanning diff..."
        if password_items is None:
            password_items = [item] if (item := self._current_remote_folder()) is not None else None
        self._run_worker(status, work, done, password_items=password_items)

    def _queue_diff_progress(self, progress: DiffProgress) -> None:
        self._safe_after(lambda progress=progress: self._apply_diff_progress(progress))

    def _apply_diff_progress(self, progress: DiffProgress) -> None:
        if not self._window_alive():
            return
        self.status_var.set(self._format_diff_progress(progress))
        self.transfer_status.set_progress(progress.current, progress.total)

    def _format_diff_progress(self, progress: DiffProgress) -> str:
        if progress.current is None or progress.total is None:
            return progress.message

        if progress.phase == DiffProgressPhase.LOCAL_FOLDER_HASH:
            return f"{progress.message} ({progress.current} folders hashed out of {progress.total})"

        if progress.unit:
            return f"{progress.message} ({progress.current} {progress.unit}/{progress.total})"

        return f"{progress.message} ({progress.current}/{progress.total})"

    def _queue_transfer_progress(self, progress: TransferProgress) -> None:
        self._safe_after(lambda progress=progress: self._apply_transfer_progress(progress))

    def _apply_transfer_progress(self, progress: TransferProgress) -> None:
        if not self._window_alive():
            return
        self._attach_current_transfer_if_needed()
        self.transfer_status.apply_sync_progress(progress)
        self._update_transfer_states_button()

    def _queue_status(self, status: str) -> None:
        self._safe_after(lambda status=status: self._set_busy(True, status))

    def _format_bytes(self, value: int) -> str:
        return TransferStatusBar.format_bytes(value)

    def back(self) -> None:
        if self._busy:
            return

        current_local_root, current_remote_root = self.stack[-1]
        if not self._can_go_back(current_local_root, current_remote_root):
            return

        parent_local_root = Path(current_local_root).parent
        parent_remote_root = self._parent_remote_root(current_remote_root, parent_local_root)
        self.stack.append((parent_local_root, parent_remote_root))
        self._load_current_level(clear_cache=False)

    def _navigate_breadcrumb(self, folder_id: str) -> None:
        if self._busy:
            return

        local_root, remote_root = self.stack[-1]
        breadcrumbs = self._current_sync_breadcrumbs(remote_root)
        target_index = next((index for index, breadcrumb in enumerate(breadcrumbs) if str(breadcrumb.id) == str(folder_id)), None)
        if target_index is None:
            return

        distance = len(breadcrumbs) - 1 - target_index
        target_local_root = Path(local_root) if distance == 0 else Path(local_root).parents[distance - 1]
        if not (target_local_root.resolve() == self.initial_local_root or self.initial_local_root in target_local_root.resolve().parents):
            return

        target_item = self._remote_item_by_id(folder_id)
        if target_item is not None and needs_resource_password(target_item) and not self._prompt_resource_password(target_item):
            return

        self.stack.append((target_local_root, folder_id))
        self._load_current_level(clear_cache=False, password_items=[target_item] if target_item is not None else None)

    def toggle_same(self) -> None:
        if self._busy:
            return
        self.show_same = not self.show_same
        self.same_button.configure(text="Hide same" if self.show_same else "Show same")
        self.entries = self._display_entries(self.result)
        self._render()

    def _display_entries(self, result: DiffResult) -> list[DiffEntry]:
        entries = []
        entries.extend(result.only_local)
        entries.extend(result.only_remote)
        entries.extend(result.changed)
        entries.extend(result.renamed)
        entries.extend(result.conflicts)
        if self.show_same:
            entries.extend(result.same)

        return self._sort_entries(entries)

    def _sort_table(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._configure_sort_headings()
        self.entries = self._sort_entries(self.entries)
        self._render()

    def _entry_sort_key(self, entry: DiffEntry):
        column = self._sort_column
        if column == "status":
            return entry.status.value
        if column == "size":
            return self._entry_size_value(entry)
        if column == "local":
            return entry_local_label(entry).lower()
        if column == "remote":
            return entry_remote_label(entry).lower()
        return entry_name(entry).lower(), entry.status.value

    def _sort_entries(self, entries: list[DiffEntry]) -> list[DiffEntry]:
        sorted_entries = sorted(entries, key=self._entry_sort_key, reverse=self._sort_reverse)
        return sorted(sorted_entries, key=lambda entry: not entry.is_folder)

    def _entry_size_label(self, entry: DiffEntry) -> str:
        value = self._entry_size_value(entry)
        return "-" if value < 0 else self._format_bytes(value)

    def _entry_size_value(self, entry: DiffEntry) -> int:
        sizes = [
            node.size
            for node in (entry.local, entry.remote)
            if node is not None and node.kind != NodeKind.FOLDER and node.size is not None
        ]
        return max(sizes) if sizes else -1

    def _configure_sort_headings(self) -> None:
        titles = {
            "name": "Name",
            "status": "Status",
            "size": "Size",
            "local": "Local",
            "remote": "Remote",
        }
        self.table.heading("#0", text="")
        for column, title in titles.items():
            self.table.heading(column, text=self._sort_heading_title(title, column), command=lambda c=column: self._sort_table(c))

    def _sort_heading_title(self, title: str, column: str) -> str:
        if column != self._sort_column:
            return title
        return f"{title} {'v' if self._sort_reverse else '^'}"

    def open_selected_entry(self) -> None:
        selected = self._selected_entries()
        if len(selected) != 1:
            messagebox.showinfo("Selection", "Select one row to open.", parent=self.root)
            return
        self._open_entry(selected[0])

    def show_selected_details(self) -> None:
        selected = self._selected_entries()
        if len(selected) != 1:
            messagebox.showinfo("Selection", "Select one row to inspect.", parent=self.root)
            return
        self._show_entry_details(selected[0])

    def sync_all(self) -> None:
        selected = self._selected_entries()
        if selected:
            self._sync_selected_entries(selected)
            return

        if self.result.conflicts:
            messagebox.showwarning("Conflicts", conflict_summary(self.result.conflicts), parent=self.root)
            return
        strategy = self._ask_changed_strategy()
        if strategy is None:
            return
        renamed_strategy = self._ask_renamed_strategy() if self.result.renamed else RenamedFileStrategy.USE_LOCAL_NAME
        if renamed_strategy is None:
            return
        if not messagebox.askyesno("Sync All", "Apply all changes at this level and recurse into changed folders?", parent=self.root):
            return
        local_root, remote_root = self.stack[-1]
        self._run_action(
            "Syncing this level...",
            lambda: self.syncer.sync_one_level(local_root, remote_root, strategy=strategy, renamed_strategy=renamed_strategy),
        )

    def _sync_selected_entries(self, entries: list[DiffEntry]) -> None:
        conflicts = [entry for entry in entries if entry.status == NodeStatus.CONFLICT]
        if conflicts:
            messagebox.showwarning("Conflicts", conflict_summary(conflicts), parent=self.root)
            return

        syncable_entries = [
            entry
            for entry in entries
            if entry.status in (NodeStatus.ONLY_LOCAL, NodeStatus.ONLY_REMOTE, NodeStatus.CHANGED, NodeStatus.RENAMED)
        ]
        if not syncable_entries:
            messagebox.showinfo("Sync Selected", "No selected entries need syncing.", parent=self.root)
            return

        changed_entries = [entry for entry in syncable_entries if entry.status == NodeStatus.CHANGED]
        strategy: ChangedFileStrategy | None = None
        if changed_entries:
            strategy = self._ask_changed_strategy()
            if strategy is None:
                return
        renamed_entries = [entry for entry in syncable_entries if entry.status == NodeStatus.RENAMED]
        renamed_strategy: RenamedFileStrategy | None = None
        if renamed_entries:
            renamed_strategy = self._ask_renamed_strategy()
            if renamed_strategy is None:
                return

        if not messagebox.askyesno("Sync Selected", f"Sync {len(syncable_entries)} selected entr{'y' if len(syncable_entries) == 1 else 'ies'}?", parent=self.root):
            return

        def work():
            self.syncer.sync_entries(
                syncable_entries,
                strategy=strategy,
                renamed_strategy=renamed_strategy,
            )

        self._run_action("Syncing selected entries...", work, password_items=self._remote_items_for_entries(syncable_entries, include_parent=True))

    def upload_local(self) -> None:
        entries = self._selected_entries() or self.result.only_local
        self._apply_entries(entries, self.syncer.upload_local_entries, "Upload selected local-only entries?")

    def create_selected_remote_folder(self) -> None:
        selected = self._selected_entries()
        if len(selected) != 1 or not self._can_create_remote_folder(selected[0]):
            messagebox.showinfo("Create Remote Folder", "Select one local-only folder with an existing remote parent.", parent=self.root)
            return

        self._confirm_create_remote_folder(selected[0])

    def download_remote(self) -> None:
        entries = self._selected_entries() or self.result.only_remote
        self._apply_entries(entries, self.syncer.download_remote_entries, "Download selected remote-only entries?")

    def resolve_changed(self) -> None:
        if not self.result.changed:
            messagebox.showinfo("Changed", "No changed entries. Conflict entries require manual rename/delete/move resolution.", parent=self.root)
            return
        strategy = self._ask_changed_strategy()
        if strategy is None:
            return
        entries = self._selected_entries() or self.result.changed
        self._apply_entries(
            entries,
            lambda selected_entries: self.syncer.resolve_changed_entries(selected_entries, strategy=strategy),
            "Resolve selected changed entries?",
        )

    def delete_local(self) -> None:
        entries = self._selected_entries() or self.entries
        local_entries = [entry for entry in entries if entry.local is not None]
        if not local_entries:
            messagebox.showinfo("Delete Local", "No selected or visible local entries to delete.", parent=self.root)
            return
        if not messagebox.askyesno("Delete Local", f"Delete {len(local_entries)} local entr{'y' if len(local_entries) == 1 else 'ies'}?", parent=self.root):
            return
        self._run_action("Deleting local entries...", lambda: self.syncer.delete_local_entries(local_entries))

    def trash_remote(self) -> None:
        entries = self._selected_entries() or self.entries
        remote_entries = [entry for entry in entries if entry.remote is not None]
        if not remote_entries:
            messagebox.showinfo("Trash Remote", "No selected or visible remote entries to move to trash.", parent=self.root)
            return
        if not messagebox.askyesno("Trash Remote", f"Move {len(remote_entries)} remote entr{'y' if len(remote_entries) == 1 else 'ies'} to trash?", parent=self.root):
            return
        self._run_action(
            "Moving remote entries to trash...",
            lambda: self.syncer.trash_remote_entries(remote_entries),
            password_items=self._remote_items_for_entries(remote_entries),
        )

    def abort_transfer(self) -> None:
        self.transfer_status.set_aborting()
        self.syncer.abort_current_transfer()

    def toggle_transfer_pause(self) -> None:
        direction, transfer = self.syncer.get_active_transfer()
        if transfer is None:
            return

        ctx = getattr(transfer, "ctx", None)
        is_paused = getattr(ctx, "is_paused", None)
        paused = bool(is_paused()) if callable(is_paused) else False

        if paused:
            if self.syncer.resume_current_transfer():
                self.transfer_status.set_paused(False)
        else:
            if self.syncer.pause_current_transfer():
                self.transfer_status.set_paused(True)

    def _attach_current_transfer_if_needed(self) -> None:
        direction, transfer = self.syncer.get_active_transfer()
        if transfer is None or direction is None or self._attached_transfer is transfer:
            return
        self._attached_transfer = transfer
        self.transfer_status.attach_transfer(direction, transfer)

    def _get_report_transfer(self):
        return self.syncer.get_current_transfer()

    def _update_transfer_states_button(self) -> None:
        button = getattr(self, "transfer_states_button", None)
        if not self._widget_exists(button):
            return
        _direction, transfer = self._get_report_transfer()
        button.configure(state="normal" if transfer is not None else "disabled")

    def show_transfer_states(self) -> None:
        direction, transfer = self._get_report_transfer()
        if transfer is None:
            messagebox.showinfo("File States", "No upload or download state is available yet.", parent=self.root)
            return

        if self._widget_exists(self._state_window):
            self._state_window.lift()
            self._state_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        window.title("File States")
        window.geometry("1100x520")
        window.minsize(760, 320)
        window.transient(self.root)
        apply_window_icon(window)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        frame = ttk.Frame(window, padding=8)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        columns = ("added", "status", "name", "path", "progress", "reason")
        table = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        self._state_column_titles = {
            "added": "Added",
            "status": "Status",
            "name": "Name",
            "path": "Path",
            "progress": "Progress",
            "reason": "Reason",
        }
        for column, title, width in (
            ("added", "Added", 150),
            ("status", "Status", 120),
            ("name", "Name", 220),
            ("path", "Path", 340),
            ("progress", "Progress", 110),
            ("reason", "Reason", 260),
        ):
            table.heading(column, text=title, command=lambda c=column: self._sort_transfer_states(c))
            table.column(column, width=width, stretch=column in ("name", "path", "reason"))
        table.tag_configure("failed", foreground="#b42318")
        table.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        table.configure(yscrollcommand=scroll.set)

        controls = ttk.Frame(frame)
        controls.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        controls.columnconfigure(0, weight=1)
        self._state_filter_failed = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Failed/aborted only", variable=self._state_filter_failed, command=self._refresh_transfer_states).grid(row=0, column=0, sticky="w")
        ttk.Button(controls, text="Copy Selected Paths", command=lambda: self._copy_state_values("path", selected=True)).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(controls, text="Copy Selected Names", command=lambda: self._copy_state_values("name", selected=True)).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(controls, text="Copy Failed/Aborted", command=lambda: self._copy_state_values("path", failed_only=True)).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(controls, text="Refresh", command=self._refresh_transfer_states).grid(row=0, column=4, padx=(0, 6))
        ttk.Button(controls, text="Close", command=window.destroy).grid(row=0, column=5)

        menu = tk.Menu(window, tearoff=False)
        menu.add_command(label="Copy Path", command=lambda: self._copy_state_values("path", selected=True))
        menu.add_command(label="Copy Name", command=lambda: self._copy_state_values("name", selected=True))
        menu.add_command(label="Copy Reason", command=lambda: self._copy_state_values("reason", selected=True))
        table.bind("<Button-3>", lambda event: self._show_state_context_menu(event, menu))
        table.bind("<Control-c>", lambda _event: self._copy_state_values("path", selected=True))
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.bind("<Escape>", lambda _event: window.destroy())

        self._state_window = window
        self._state_table = table
        self._configure_state_sort_headings()
        self._refresh_transfer_states()

    def _refresh_transfer_states(self) -> None:
        self._state_refresh_after_id = None
        if not self._widget_exists(self._state_table):
            return

        direction, transfer = self._get_report_transfer()
        rows = self._collect_transfer_state_rows(direction, transfer)
        failed_only = bool(self._state_filter_failed and self._state_filter_failed.get())
        shown = [row for row in rows if not failed_only or row["is_failed"] == "1" or row["is_aborted"] == "1"]
        selected_keys = {
            row["key"]
            for item_id in self._state_table.selection()
            if (row := self._state_rows.get(str(item_id))) is not None
        }

        for item_id in self._state_table.get_children():
            self._state_table.delete(item_id)
        self._state_rows = {}
        restored_selection = []
        for index, row in enumerate(shown):
            iid = str(index)
            self._state_rows[iid] = row
            tags = ("failed",) if row["is_failed"] == "1" else ()
            self._state_table.insert("", "end", iid=iid, values=(row["added"], row["status"], row["name"], row["path"], row["progress"], row["reason"]), tags=tags)
            if row["key"] in selected_keys:
                restored_selection.append(iid)
        if restored_selection:
            self._state_table.selection_set(restored_selection)

        _active_direction, active_transfer = self.syncer.get_active_transfer()
        if active_transfer is not None:
            self._schedule_transfer_states_refresh()

    def _schedule_transfer_states_refresh(self) -> None:
        if self._state_refresh_after_id is not None or not self._widget_exists(self._state_table):
            return
        self._state_refresh_after_id = self.root.after(1000, self._refresh_transfer_states)

    def _collect_transfer_state_rows(self, direction: str | None, transfer) -> list[dict[str, str]]:
        if transfer is None:
            return []
        ctx = getattr(transfer, "ctx", None)
        if ctx is None:
            return []

        records = getattr(ctx, "records", {})
        rows = []
        for index, error in enumerate(getattr(ctx, "errors", []), start=1):
            rows.append(
                {
                    "key": f"transfer-error:{index}",
                    "added": "",
                    "added_at": str(float("inf")),
                    "status": "failed",
                    "name": f"transfer error {index}",
                    "path": f"transfer error {index}",
                    "progress": "",
                    "progress_value": "0",
                    "reason": self._format_error_reason(error),
                    "is_failed": "1",
                    "is_aborted": "0",
                }
            )

        for state_id, state in ctx.get_all_states().items():
            with getattr(state, "lock", threading.Lock()):
                status = getattr(getattr(state, "status", ""), "value", getattr(state, "status", ""))
                error = getattr(state, "error", None)
                rows.append(self._transfer_state_row(direction, str(state_id), state, records.get(state_id), str(status), error))
        rows.sort(key=self._transfer_state_sort_key, reverse=self._state_sort_reverse)
        return rows

    def _transfer_state_row(self, direction: str | None, state_id: str, state, record, status: str, error) -> dict[str, str]:
        name = state_id
        path = state_id
        size_total = getattr(state, "size_total", 0) or 0
        current = getattr(state, "bytes_downloaded", None)
        if current is None:
            current = getattr(state, "bytes_uploaded", 0) or 0

        artifacts = getattr(state, "artifacts", None)
        if artifacts is not None:
            name = str(getattr(artifacts, "name", "") or name)
            path = str(getattr(artifacts, "local_path", "") or name)
            size_total = getattr(artifacts, "size", size_total) or size_total

        if record is not None:
            file_info = getattr(record, "file_info", None)
            if file_info is not None:
                name = str(getattr(file_info, "name", "") or name)
                path = str(getattr(record, "final_user_output_path", "") or getattr(file_info, "path", "") or path)

        added_at = float(getattr(state, "added_at", 0.0) or 0.0)
        return {
            "key": state_id,
            "added": self._format_added_timestamp(added_at),
            "added_at": str(added_at),
            "status": status,
            "name": name,
            "path": path,
            "progress": f"{self._format_bytes(int(current))}/{self._format_bytes(int(size_total))}",
            "progress_value": str(int(current)),
            "reason": self._format_error_reason(error),
            "is_failed": "1" if status in ("failed", "save_failed") else "0",
            "is_aborted": "1" if status == "aborted" else "0",
        }

    def _format_error_reason(self, error) -> str:
        if error is None:
            return ""

        parts = []
        message = getattr(error, "message", None) or str(error) or repr(error)
        parts.append(f"{error.__class__.__name__}: {message}")

        status = getattr(error, "status", None)
        if status is not None and f"HTTP {status}" not in message:
            parts.append(f"HTTP {status}")

        method = getattr(error, "method", None)
        url = getattr(error, "url", None)
        if method or url:
            target = " ".join(str(part) for part in (method, url) if part)
            if target and target not in message:
                parts.append(target)

        text = getattr(error, "text", None)
        if text and text not in message:
            parts.append(str(text))

        cause = getattr(error, "cause", None) or getattr(error, "__cause__", None)
        if cause is not None:
            cause_message = str(cause)
            if cause_message and cause_message not in message:
                parts.append(f"caused by {cause.__class__.__name__}: {cause_message}")

        return " | ".join(parts)

    def _format_added_timestamp(self, value: float) -> str:
        if value <= 0:
            return ""
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")

    def _sort_transfer_states(self, column: str) -> None:
        if self._state_sort_column == column:
            self._state_sort_reverse = not self._state_sort_reverse
        else:
            self._state_sort_column = column
            self._state_sort_reverse = False
        self._configure_state_sort_headings()
        self._refresh_transfer_states()

    def _transfer_state_sort_key(self, row: dict[str, str]):
        column = self._state_sort_column
        if column == "added":
            return float(row.get("added_at", "0") or 0)
        if column == "progress":
            return int(row.get("progress_value", "0") or 0)
        return row.get(column, "").lower()

    def _configure_state_sort_headings(self) -> None:
        table = getattr(self, "_state_table", None)
        if not self._widget_exists(table):
            return
        for column, title in getattr(self, "_state_column_titles", {}).items():
            suffix = ""
            if column == self._state_sort_column:
                suffix = " v" if self._state_sort_reverse else " ^"
            table.heading(column, text=f"{title}{suffix}", command=lambda c=column: self._sort_transfer_states(c))

    def _copy_state_values(self, key: str, *, selected: bool = False, failed_only: bool = False) -> None:
        if not self._widget_exists(self._state_table):
            return
        item_ids = self._state_table.selection() if selected else self._state_table.get_children()
        values = []
        for item_id in item_ids:
            row = self._state_rows.get(str(item_id))
            if row is None or (failed_only and row["is_failed"] != "1" and row["is_aborted"] != "1"):
                continue
            value = row.get(key, "")
            if value:
                values.append(value)
        if not values:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(values))

    def _show_state_context_menu(self, event, menu: tk.Menu) -> None:
        row_id = self._state_table.identify_row(event.y)
        if row_id:
            if row_id not in self._state_table.selection():
                self._state_table.selection_set(row_id)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _apply_entries(self, entries: list[DiffEntry], handler, prompt: str) -> None:
        if not entries:
            messagebox.showinfo("Nothing", "Nothing to apply.", parent=self.root)
            return
        if not messagebox.askyesno("Confirm", prompt, parent=self.root):
            return

        self._run_action("Applying entries...", lambda: handler(entries), password_items=self._remote_items_for_entries(entries, include_parent=True))

    def _selected_entries(self) -> list[DiffEntry]:
        selected = []
        for item_id in self.table.selection():
            values = self.table.item(item_id, "values")
            if not values:
                continue
            selected.append(self.entries[int(item_id)])
        return selected

    def _clear_selection_on_empty_click(self, event) -> None:
        if self.table.identify_row(event.y):
            return
        self._clear_selection()

    def _clear_selection(self, _event=None) -> str:
        selection = self.table.selection()
        if selection:
            self.table.selection_remove(selection)
        self._update_selection_actions()
        return TK_STOP_EVENT

    def _entry_from_item_id(self, item_id: str) -> DiffEntry | None:
        if not item_id:
            return None
        try:
            return self.entries[int(item_id)]
        except (ValueError, IndexError):
            return None

    def _remote_items_for_entries(self, entries: list[DiffEntry], *, include_parent: bool = False) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()
        for entry in entries:
            remote_ids = []
            if entry.remote_id is not None:
                remote_ids.append(entry.remote_id)
            if include_parent and entry.parent_remote_id is not None:
                remote_ids.append(entry.parent_remote_id)
            for remote_id in remote_ids:
                remote_id = str(remote_id)
                if self.syncer.is_missing_remote_folder(remote_id):
                    continue
                if remote_id in seen:
                    continue
                seen.add(remote_id)
                try:
                    item = self.syncer.remote.require_cached_item(remote_id)
                except KeyError:
                    item = self.syncer.remote.get_item(remote_id)
                items.append(item)
        return items

    def _on_double_click(self, event) -> str | None:
        if self._busy:
            return TK_STOP_EVENT

        item_id = self.table.identify_row(event.y)
        entry = self._entry_from_item_id(item_id)
        if entry is None:
            return
        self.table.selection_set(item_id)
        self._open_entry(entry)
        return None

    def _show_context_menu(self, event) -> str | None:
        if self._busy:
            return TK_STOP_EVENT

        item_id = self.table.identify_row(event.y)
        entry = self._entry_from_item_id(item_id)
        if entry is None:
            return

        self.table.selection_set(item_id)
        menu = tk.Menu(self.root, tearoff=False)
        menu.add_command(
            label="Open",
            image=self.icons["action_open"],
            compound="left",
            command=lambda: self._open_entry(entry),
            state=self._menu_state(self._can_open_entry(entry)),
        )
        menu.add_command(
            label="Details",
            image=self.icons["action_details"],
            compound="left",
            command=lambda: self._show_entry_details(entry),
        )
        menu.add_separator()
        menu.add_command(
            label="Upload Local",
            image=self.icons["action_upload"],
            compound="left",
            command=lambda: self._confirm_single_upload(entry),
            state=self._menu_state(entry.status == NodeStatus.ONLY_LOCAL),
        )
        menu.add_command(
            label="Download Remote",
            image=self.icons["action_download"],
            compound="left",
            command=lambda: self._confirm_single_download(entry),
            state=self._menu_state(entry.status == NodeStatus.ONLY_REMOTE),
        )
        menu.add_command(
            label="Resolve Changed",
            image=self.icons["action_resolve"],
            compound="left",
            command=lambda: self._confirm_single_resolve(entry),
            state=self._menu_state(entry.status == NodeStatus.CHANGED and not entry.is_folder),
        )
        menu.add_separator()
        menu.add_command(
            label="Delete Local",
            image=self.icons["action_delete"],
            compound="left",
            command=lambda: self._confirm_single_delete_local(entry),
            state=self._menu_state(entry.local is not None),
        )
        menu.add_command(
            label="Trash Remote",
            image=self.icons["action_trash"],
            compound="left",
            command=lambda: self._confirm_single_trash_remote(entry),
            state=self._menu_state(entry.remote is not None),
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return None

    def _menu_state(self, enabled: bool) -> str:
        return "normal" if enabled and not self._busy else "disabled"

    def _confirm_single_upload(self, entry: DiffEntry) -> None:
        if messagebox.askyesno("Upload", f"Upload {entry_name(entry)}?", parent=self.root):
            self._run_action("Uploading selected entry...", lambda: self.syncer.upload_local_entries([entry]))

    def _confirm_create_remote_folder(self, entry: DiffEntry) -> None:
        if messagebox.askyesno("Create Remote Folder", f"Create remote folder {entry_name(entry)}?", parent=self.root):
            self._run_action(
                "Creating remote folder...",
                lambda: self._create_remote_folder(entry),
                password_items=self._remote_items_for_entries([entry], include_parent=True),
            )

    def _create_remote_folder(self, entry: DiffEntry) -> Folder:
        if not self._can_create_remote_folder(entry):
            raise ValueError(f"Cannot create remote folder for {entry_name(entry)}")

        return self.syncer.create_remote_folder(entry)

    def _can_create_remote_folder(self, entry: DiffEntry) -> bool:
        if entry.status != NodeStatus.ONLY_LOCAL or not entry.is_folder:
            return False
        if entry.local_path is None or entry.parent_remote_id is None:
            return False
        return not self.syncer.is_missing_remote_folder(str(entry.parent_remote_id))

    def _confirm_single_download(self, entry: DiffEntry) -> None:
        if messagebox.askyesno("Download", f"Download {entry_name(entry)}?", parent=self.root):
            self._run_action("Downloading selected entry...", lambda: self.syncer.download_remote_entries([entry]))

    def _confirm_single_resolve(self, entry: DiffEntry) -> None:
        strategy = self._ask_changed_strategy()
        if strategy and messagebox.askyesno("Resolve", f"Resolve {entry_name(entry)} with {strategy.value}?", parent=self.root):
            self._run_action("Resolving selected entry...", lambda: self.syncer.resolve_changed_entries([entry], strategy=strategy))

    def _confirm_single_delete_local(self, entry: DiffEntry) -> None:
        if messagebox.askyesno("Delete Local", f"Delete local {entry_name(entry)}?", parent=self.root):
            self._run_action("Deleting local entry...", lambda: self.syncer.delete_local_entries([entry]))

    def _confirm_single_trash_remote(self, entry: DiffEntry) -> None:
        if messagebox.askyesno("Trash Remote", f"Move remote {entry_name(entry)} to trash?", parent=self.root):
            self._run_action(
                "Moving remote entry to trash...",
                lambda: self.syncer.trash_remote_entries([entry]),
                password_items=self._remote_items_for_entries([entry]),
            )

    def _can_open_entry(self, entry: DiffEntry) -> bool:
        if entry.is_folder:
            if entry.remote_id is not None:
                return True
            if entry.local_path is None:
                return False

            _local_root, remote_root = self.stack[-1]
            return not self.syncer.is_missing_remote_folder(self.syncer.remote.normalize_id(remote_root))
        return entry.local is not None or entry.remote is not None

    def _open_entry(self, entry: DiffEntry) -> None:
        if entry.is_folder:
            self._open_folder_entry(entry)
            return

        self._open_file_entry(entry)

    def _open_folder_entry(self, entry: DiffEntry) -> None:
        local_path = entry.local_path
        if local_path is None:
            messagebox.showinfo("Open", "This folder has no local path projection.", parent=self.root)
            return

        remote_id = entry.remote_id
        password_items = self._remote_items_for_entries([entry])
        target_item = password_items[0] if password_items else None
        if target_item is not None and needs_resource_password(target_item) and not self._prompt_resource_password(target_item):
            return

        if remote_id is None:
            _current_local_root, current_remote_root = self.stack[-1]
            current_remote_id = self.syncer.remote.normalize_id(current_remote_root)
            if self.syncer.is_missing_remote_folder(current_remote_id):
                messagebox.showinfo("Open", "Create the parent remote folder before opening deeper local-only folders.", parent=self.root)
                return

            remote_id = self.syncer.missing_remote_folder_id(local_path, entry.parent_remote_id)

        self.stack.append((Path(local_path), remote_id))
        self._load_current_level(clear_cache=False, password_items=password_items or None)

    def _parent_remote_root(self, remote_root: Folder | str, parent_local_root: Path) -> Folder | str:
        remote_id = self.syncer.remote.normalize_id(remote_root)
        if self.syncer.is_missing_remote_folder(remote_id):
            _local_path, parent_remote_id = self.syncer.missing_remote_folder_info(remote_id)
            return parent_remote_id if parent_remote_id is not None else remote_root

        try:
            remote_item = self.syncer.remote.require_cached_item(remote_id)
        except KeyError:
            remote_item = self.syncer.remote.get_item(remote_id)

        parent_remote_id = remote_item.parent_id
        if parent_remote_id is None:
            return remote_root
        return parent_remote_id

    def _can_go_back(self, local_root: Path, remote_root: Folder | str) -> bool:
        local_root = Path(local_root)
        remote_id = self.syncer.remote.normalize_id(remote_root)
        if local_root.resolve() == self.initial_local_root and remote_id == self.initial_remote_id:
            return False

        if local_root.parent == local_root:
            return False

        if self.syncer.is_missing_remote_folder(remote_id):
            _local_path, parent_remote_id = self.syncer.missing_remote_folder_info(remote_id)
            return parent_remote_id is not None

        try:
            remote_item = self.syncer.remote.require_cached_item(remote_id)
        except KeyError:
            remote_item = self.syncer.remote.get_item(remote_id)

        return remote_item.parent_id is not None

    def _open_file_entry(self, entry: DiffEntry) -> None:
        if entry.local is None:
            self._open_remote_file(entry)
            return

        local_path = entry.local_path
        if local_path is None:
            return

        path = Path(local_path)
        if not path.exists():
            messagebox.showwarning("Open", f"Local path does not exist:\n{path}", parent=self.root)
            return

        try:
            self._open_path_native(path)
        except Exception as exc:
            messagebox.showerror("Open", str(exc), parent=self.root)

    def _open_remote_file(self, entry: DiffEntry) -> None:
        remote_id = entry.remote_id
        if remote_id is None:
            return

        try:
            remote_item = self.syncer.remote.require_cached_item(remote_id)
        except KeyError:
            remote_item = self.syncer.remote.get_item(remote_id)

        view_url = remote_item.view_url
        webbrowser.open(view_url)

    def _open_path_native(self, path: Path) -> None:
        if hasattr(os, "startfile"):
            os.startfile(path)
            return

        if os.name == "posix":
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.Popen([opener, str(path)])
            return

        raise RuntimeError(f"No native opener available for {path}")

    def _show_entry_details(self, entry: DiffEntry) -> None:
        details = [
            f"Name: {entry_name(entry)}",
            f"Status: {entry.status.value}",
            f"Kind: {'folder' if entry.is_folder else 'file'}",
        ]
        if entry.message:
            details.extend(("", "Message:", entry.message))

        details.extend(("", "Local:", *self._node_details(entry.local, entry.local_path)))
        details.extend(("", "Remote:", *self._node_details(entry.remote, entry.remote_id)))

        self._show_copyable_details("Details", "\n".join(details))

    def _show_copyable_details(self, title: str, text: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("720x460")
        dialog.minsize(480, 300)
        dialog.transient(self.root)

        frame = ttk.Frame(dialog, padding=10)
        frame.grid(row=0, column=0, sticky="nsew")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        details_text = tk.Text(frame, wrap="word", font=("Consolas", 10), undo=False)
        details_text.grid(row=0, column=0, sticky="nsew")
        details_scroll = ttk.Scrollbar(frame, orient="vertical", command=details_text.yview)
        details_scroll.grid(row=0, column=1, sticky="ns")
        details_text.configure(yscrollcommand=details_scroll.set)
        details_text.insert("1.0", text)
        details_text.configure(state="disabled")

        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Copy All", command=lambda: self._copy_text_to_clipboard(text)).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Close", command=dialog.destroy).pack(side="left")

        details_text.bind("<Control-a>", lambda _event: self._select_all_text(details_text))
        details_text.bind("<Control-A>", lambda _event: self._select_all_text(details_text))
        details_text.focus_set()

    def _copy_text_to_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _select_all_text(self, widget: tk.Text) -> str:
        widget.tag_add("sel", "1.0", "end-1c")
        widget.mark_set("insert", "1.0")
        widget.see("insert")
        return TK_STOP_EVENT

    def _node_details(self, node: Node | None, display_id) -> list[str]:
        if node is None:
            return ["-"]

        return [
            f"Path/ID: {display_id if display_id is not None else node.uid}",
            f"Parent: {node.parent_uid if node.parent_uid is not None else '-'}",
            f"Created: {node.created_at}",
            f"Modified: {node.modified_at if node.modified_at is not None else '-'}",
            f"Size: {self._node_size_label(node)}",
            f"Hash: {node.hash if node.hash is not None else '-'}",
        ]

    def _node_size_label(self, node: Node) -> str:
        if node.kind != NodeKind.FOLDER:
            return self._format_bytes(node.size) if node.size is not None else "-"

        size = self._node_size(node)
        return self._format_bytes(size)

    def _node_size(self, node: Node) -> int:
        if node.source == NodeOrigin.LOCAL:
            return self.syncer.local.get_folder_size(node.uid)

        node_id = str(node.uid)
        return self.syncer.remote.get_folder_size(node_id)


    def _render(self) -> None:
        local_root, remote_root = self.stack[-1]
        self.local_var.set(str(local_root))
        self.remote_var.set(remote_label(remote_root))
        breadcrumbs = self._current_sync_breadcrumbs(remote_root)
        if breadcrumbs:
            self.breadcrumbs.set_items(breadcrumbs)
        else:
            self.breadcrumbs.set_items([(self.syncer.remote.normalize_id(remote_root), remote_label(remote_root))])
        self.back_button.configure(state="normal" if self._can_go_back(local_root, remote_root) else "disabled")
        self.summary_var.set(
            f"local={len(self.result.only_local)}  "
            f"remote={len(self.result.only_remote)}  "
            f"changed={len(self.result.changed)}  "
            f"renamed={len(self.result.renamed)}  "
            f"conflicts={len(self.result.conflicts)}  "
            f"same={len(self.result.same)}"
        )

        for item in self.table.get_children():
            self.table.delete(item)

        for index, entry in enumerate(self.entries):
            self.table.insert(
                "",
                "end",
                iid=str(index),
                image=self._entry_icon(entry),
                values=(
                    entry_name(entry),
                    entry.status.value,
                    self._entry_size_label(entry),
                    entry_local_label(entry),
                    entry_remote_label(entry),
                ),
            )

        self._update_selection_actions()

        if not self.entries:
            self.status_var.set("No visible differences.")
        elif self.result.conflicts:
            self.status_var.set("Conflicts require manual rename/delete/move resolution.")
        else:
            self.status_var.set("Ready")
        self._update_navigation_actions()

    def _update_selection_actions(self) -> None:
        if self._busy:
            return

        selected = self._selected_entries()
        local_selected = [entry for entry in selected if entry.local is not None]
        remote_selected = [entry for entry in selected if entry.remote is not None]
        local_only_selected = [entry for entry in selected if entry.status == NodeStatus.ONLY_LOCAL]
        remote_only_selected = [entry for entry in selected if entry.status == NodeStatus.ONLY_REMOTE]
        changed_file_selected = [entry for entry in selected if entry.status == NodeStatus.CHANGED and not entry.is_folder]

        states = {
            "sync_all": True,
            "upload": bool(local_only_selected) and len(local_only_selected) == len(selected),
            "create_remote_folder": len(selected) == 1 and self._can_create_remote_folder(selected[0]),
            "download": bool(remote_only_selected) and len(remote_only_selected) == len(selected),
            "resolve": bool(changed_file_selected) and len(changed_file_selected) == len(selected),
            "details": len(selected) == 1,
            "delete_local": bool(local_selected) and len(local_selected) == len(selected),
            "trash_remote": bool(remote_selected) and len(remote_selected) == len(selected),
        }

        self.selection_buttons["sync_all"].configure(text="Sync Selected" if selected else "Sync All")
        for action, enabled in states.items():
            self.selection_buttons[action].configure(state="normal" if enabled else "disabled")

    def _update_navigation_actions(self) -> None:
        if self._busy:
            return

        local_root, remote_root = self.stack[-1]
        self.back_button.configure(state="normal" if self._can_go_back(local_root, remote_root) else "disabled")

    def _current_sync_breadcrumbs(self, remote_root: Folder | str):
        remote_id = self.syncer.remote.normalize_id(remote_root)
        if self.syncer.is_missing_remote_folder(remote_id):
            local_path, parent_remote_id = self.syncer.missing_remote_folder_info(remote_id)
            if parent_remote_id is None:
                return [(remote_id, local_path.name)]
            return [*self._current_sync_breadcrumbs(parent_remote_id), (remote_id, local_path.name)]

        try:
            remote_item = self.syncer.remote.require_cached_item(remote_id)
        except KeyError:
            remote_item = self.syncer.remote.get_item(remote_id)
        if not isinstance(remote_item, Folder):
            return []

        breadcrumbs = list(remote_item.breadcrumbs or [])
        root_index = next((index for index, breadcrumb in enumerate(breadcrumbs) if str(breadcrumb.id) == self.initial_remote_id), None)
        if root_index is None:
            return [(remote_item.id, remote_item.name)]
        return breadcrumbs[root_index:]

    def _entry_icon(self, entry: DiffEntry) -> tk.PhotoImage:
        if entry.is_folder:
            return self.icons["folder"]
        return self.icons[file_icon_key(entry_name(entry))]

    def _ask_changed_strategy(self) -> ChangedFileStrategy | None:
        choices = [
            ChangedFileStrategy.SKIP,
            ChangedFileStrategy.NEWER,
            ChangedFileStrategy.UPLOAD_LOCAL,
            ChangedFileStrategy.DOWNLOAD_REMOTE,
            ChangedFileStrategy.ERROR,
        ]
        selected: ChangedFileStrategy | None = None

        dialog = tk.Toplevel(self.root)
        dialog.title("Changed Strategy")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.columnconfigure(0, weight=1)

        ttk.Label(dialog, text="Changed file strategy").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        value_var = tk.StringVar(value=ChangedFileStrategy.SKIP.value)
        strategy_select = ttk.Combobox(
            dialog,
            textvariable=value_var,
            values=[strategy.value for strategy in choices],
            state="readonly",
            width=24,
        )
        strategy_select.grid(row=1, column=0, sticky="ew", padx=12)

        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, sticky="e", padx=12, pady=12)

        def accept() -> None:
            nonlocal selected
            selected = next((strategy for strategy in choices if strategy.value == value_var.get()), None)
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="OK", command=accept).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())

        self._center_dialog_on_parent(dialog)
        strategy_select.focus_set()
        dialog.grab_set()
        self.root.wait_window(dialog)
        return selected

    def _ask_renamed_strategy(self) -> RenamedFileStrategy | None:
        choices = [
            RenamedFileStrategy.USE_LOCAL_NAME,
            RenamedFileStrategy.USE_REMOTE_NAME,
        ]
        selected: RenamedFileStrategy | None = None

        dialog = tk.Toplevel(self.root)
        dialog.title("Renamed Strategy")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.columnconfigure(0, weight=1)

        ttk.Label(dialog, text="Renamed file strategy").grid(row=0, column=0, sticky="w", padx=12, pady=(12, 6))
        value_var = tk.StringVar(value=RenamedFileStrategy.USE_LOCAL_NAME.value)
        strategy_select = ttk.Combobox(
            dialog,
            textvariable=value_var,
            values=[strategy.value for strategy in choices],
            state="readonly",
            width=24,
        )
        strategy_select.grid(row=1, column=0, sticky="ew", padx=12)

        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, sticky="e", padx=12, pady=12)

        def accept() -> None:
            nonlocal selected
            selected = next((strategy for strategy in choices if strategy.value == value_var.get()), None)
            dialog.destroy()

        def cancel() -> None:
            dialog.destroy()

        ttk.Button(buttons, text="Cancel", command=cancel).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="OK", command=accept).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.bind("<Return>", lambda _event: accept())
        dialog.bind("<Escape>", lambda _event: cancel())

        self._center_dialog_on_parent(dialog)
        strategy_select.focus_set()
        dialog.grab_set()
        self.root.wait_window(dialog)
        return selected

    def _center_dialog_on_parent(self, dialog: tk.Toplevel) -> None:
        dialog.update_idletasks()
        parent_x = self.root.winfo_rootx()
        parent_y = self.root.winfo_rooty()
        parent_width = self.root.winfo_width()
        parent_height = self.root.winfo_height()
        dialog_width = dialog.winfo_reqwidth()
        dialog_height = dialog.winfo_reqheight()

        x = parent_x + max((parent_width - dialog_width) // 2, 0)
        y = parent_y + max((parent_height - dialog_height) // 2, 0)
        dialog.geometry(f"+{x}+{y}")

    def _run_action(self, status: str, work, *, password_items: list[Item] | None = None) -> None:
        self._run_worker(status, work, lambda _result: self._load_current_level(clear_cache=False), password_items=password_items)

    def _run_worker(self, status: str, work, done, *, password_items: list[Item] | None = None) -> None:
        if self._busy:
            return
        self._set_busy(True, status)

        def target():
            try:
                result = work()
            except SyncConflictError as exc:
                self._safe_after(lambda exc=exc: self._worker_failed("Sync conflict", exc))
            except SyncTransferCancelled as exc:
                self._safe_after(lambda exc=exc: self._worker_cancelled(exc))
            except BackendMissingOrIncorrectResourcePasswordError as exc:
                self._safe_after(lambda exc=exc: self._worker_password_required(status, work, done, password_items, exc))
            except Exception as exc:
                logger.exception("Something failed.", exc_info=exc)
                self._safe_after(lambda exc=exc: self._worker_failed("Error", exc))
            else:
                self._safe_after(lambda: self._worker_done(done, result))
            finally:
                if self._closed:
                    self.syncer.clear_sync_boundary()

        threading.Thread(target=target, daemon=True).start()

    def _worker_done(self, done, result) -> None:
        if not self._window_alive():
            return
        self._set_busy(False, "Ready")
        done(result)

    def _worker_failed(self, title: str, exc: Exception) -> None:
        if not self._window_alive():
            return
        self._set_busy(False, "Ready")
        messagebox.showerror(title, str(exc), parent=self.root)

    def _worker_cancelled(self, exc: SyncTransferCancelled) -> None:
        if not self._window_alive():
            return
        self._set_busy(False, str(exc))

    def _worker_password_required(
        self,
        status: str,
        work,
        done,
        password_items: list[Item] | None = None,
        exc: BackendMissingOrIncorrectResourcePasswordError | None = None,
    ) -> None:
        if not self._window_alive():
            return
        self._set_busy(False, "Password required")
        if exc is None:
            messagebox.showerror("Folder Password", "Password is required, but the backend did not provide password details.", parent=self.root)
            return

        item = password_prompt_item(exc, password_items or [])
        if item is None:
            messagebox.showerror("Folder Password", str(exc), parent=self.root)
            return

        if self._prompt_resource_password(item):
            self._run_worker(status, work, done, password_items=password_items)

    def _current_remote_folder(self) -> Folder | None:
        _local_root, remote_root = self.stack[-1]
        remote_id = self.syncer.remote.normalize_id(remote_root)

        item = self._remote_item_by_id(remote_id)
        return item if isinstance(item, Folder) else None

    def _remote_item_by_id(self, remote_id: str) -> Item | None:
        if self.syncer.is_missing_remote_folder(str(remote_id)):
            return None

        try:
            return self.syncer.remote.require_cached_item(remote_id)
        except KeyError:
            return self.syncer.remote.get_item(remote_id)

    def _prompt_resource_password(self, item: Item) -> bool:
        if not self._window_alive():
            return False
        return prompt_resource_password(self.root, item, self.syncer.remote.set_item_password)

    def _set_busy(self, busy: bool, status: str) -> None:
        if not self._window_alive():
            return
        self._busy = busy
        self.status_var.set(status)
        self.transfer_status.set_abort_enabled(False)
        self.transfer_status.set_pause_enabled(False)
        self._update_transfer_states_button()
        if not busy:
            self.transfer_status.set_progress(0, 0)
            self.transfer_status.clear_transfer()
            self._attached_transfer = None
        state = "disabled" if busy else "normal"
        for button in self.buttons:
            button.configure(state=state)
        if busy:
            self.table.configure(selectmode="none")
            for item in self.table.get_children():
                self.table.item(item, tags=("disabled",))
        else:
            self.table.configure(selectmode="extended")
            for item in self.table.get_children():
                self.table.item(item, tags=())
            self._update_navigation_actions()
            self._update_selection_actions()
