from __future__ import annotations

import json
import logging
import queue
import re
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError as exc:
    raise RuntimeError(
        "The remote browser GUI requires optional GUI dependencies. "
        "Install them with `pip install idrive_toolkit[gui]`. "
        "Install transfer support too with `pip install idrive_toolkit[transfer]`, "
        "or install everything with `pip install idrive_toolkit[all]`."
    ) from exc

from ..Config import APIConfig
from ..iDrive import Client
from ..exceptions import BackendMissingOrIncorrectResourcePasswordError
from ..models.File import File
from ..models.Folder import Folder
from ..models.Item import Item
from ..state.Storage import IdriveStorage
from ..syncer.Syncer import Syncer
from ..utils import common
from ..version_check import UpdateInfo, check_for_update
from .transfer_errors import raise_transfer_errors
from .BreadcrumbsBar import BreadcrumbsBar
from .GuiUtils import apply_window_icon, file_icon_key, needs_resource_password, password_prompt_item, prompt_resource_password, safe_item_label, set_windows_app_user_model_id
from .SyncGui import SyncGui, SyncGuiAlreadyOpenError
from .TransferStatusBar import TransferStatusBar

logger = logging.getLogger("iDrive")
TK_STOP_EVENT = "break"


class _GuiStream:
    def __init__(self, original, write_log, level_name: str):
        self._original = original
        self._write_log = write_log
        self._level_name = level_name

    def write(self, text: str) -> int:
        if text:
            self._write_log(text, self._classify_text(text))
            if self._original is not None:
                self._original.write(text)
        return len(text)

    def flush(self) -> None:
        if self._original is not None:
            self._original.flush()

    def isatty(self) -> bool:
        if self._original is None:
            return False
        return bool(getattr(self._original, "isatty", lambda: False)())

    @property
    def encoding(self):
        return getattr(self._original, "encoding", None)

    def _classify_text(self, text: str) -> str:
        match = re.search(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", text)
        if match:
            return match.group(1)
        if "Traceback (most recent call last)" in text or "Exception" in text:
            return "EXCEPTION"
        return self._level_name


class _GuiLogHandler(logging.Handler):
    def __init__(self, write_log):
        super().__init__()
        self._write_log = write_log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level_name = "EXCEPTION" if record.exc_info else record.levelname
            self._write_log(self.format(record) + "\n", level_name)
        except Exception:
            self.handleError(record)


class BrowserGuiApp:
    def __init__(self):
        set_windows_app_user_model_id()
        self.root = TkinterDnD.Tk()
        self.root.report_callback_exception = self._show_callback_exception
        self.root.title("iDrive Remote Browser")
        apply_window_icon(self.root)
        self.root.geometry("1000x640")
        self.root.minsize(1080, 460)

        self.client: Client | None = None
        self.syncer: Syncer | None = None
        self.current_folder: Folder | None = None
        self.folder_stack: list[Folder] = []
        self.items: dict[str, Item] = {}
        self.icons: dict[str, tk.PhotoImage] = {}
        self.storage = IdriveStorage()
        self.config_path = self.storage.get_config_file("remote-browser.json")
        self.auth_path = self.storage.get_auth_file("remote-browser.json")
        self.config = self._load_config()
        self.auth = self._load_auth()
        self._busy = False
        self._active_transfer = None
        self._active_transfer_lock = threading.Lock()
        self._last_transfer = None
        self._last_transfer_direction = None
        self._active_upload_queue_lock = threading.Lock()
        self._active_upload_queue: list[tuple[Path, Folder]] = []
        self._accepting_active_uploads = False
        self._transfer_cancelled = False
        self._state_window = None
        self._state_table = None
        self._state_filter_failed = None
        self._state_rows: dict[str, dict[str, str]] = {}
        self._state_refresh_after_id = None
        self._browser_sort_column = "name"
        self._browser_sort_reverse = False
        self._state_sort_column = "added"
        self._state_sort_reverse = False
        self._sync_window = None
        self._ui_queue: queue.Queue[tuple] = queue.Queue()
        self._log_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._log_buffer: list[tuple[str, str]] = []
        self._log_buffer_lock = threading.Lock()
        self._max_log_chars = 500_000
        self._log_window = None
        self._log_text = None
        self._log_filter_var = tk.StringVar(value="All")
        self._log_filter_var.trace_add("write", lambda *_args: self._render_log_buffer())
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr
        self._stdout_proxy = None
        self._stderr_proxy = None
        self._log_handler = None

        self._install_log_capture()
        self._poll_ui_queue()
        self._poll_log_queue()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(1000, self._start_version_check)
        if not self._try_cached_login():
            self._build_login()

    def run(self) -> None:
        self.root.mainloop()

    def _build_login(self) -> None:
        self._clear_root()
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        panel = ttk.Frame(self.root, padding=24)
        panel.grid(row=0, column=0)
        panel.columnconfigure(1, weight=1)

        ttk.Label(panel, text="Base URL").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=6)
        self.base_url_var = tk.StringVar(value=self.config.get("base_url", "http://localhost:8000"))
        ttk.Entry(panel, textvariable=self.base_url_var, width=44).grid(row=0, column=1, sticky="ew", pady=6)

        ttk.Label(panel, text="Username").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=6)
        self.username_var = tk.StringVar(value=self.config.get("username", ""))
        ttk.Entry(panel, textvariable=self.username_var, width=44).grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(panel, text="Password").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=6)
        self.password_var = tk.StringVar()
        password_entry = ttk.Entry(panel, textvariable=self.password_var, show="*", width=44)
        password_entry.grid(row=2, column=1, sticky="ew", pady=6)
        password_entry.bind("<Return>", lambda _event: self.login())

        self.login_button = ttk.Button(panel, text="Login", command=self.login)
        self.login_button.grid(row=3, column=1, sticky="e", pady=(4, 0))
        self.logs_button = ttk.Button(panel, text="Logs", command=self.show_logs)
        self.logs_button.grid(row=3, column=0, sticky="w", pady=(4, 0))

        self.login_status_var = tk.StringVar(value="")
        ttk.Label(panel, textvariable=self.login_status_var).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))

    def _build_startup(self, status: str) -> None:
        self._clear_root()
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        self.login_status_var = tk.StringVar(value=status)
        ttk.Label(self.root, textvariable=self.login_status_var, padding=24).grid(row=0, column=0)

    def _build_browser(self) -> None:
        self._clear_root()
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        self.icons = self._create_icons()

        actions = ttk.Frame(self.root, padding=(10, 4))
        actions.grid(row=0, column=0, sticky="ew")

        self.back_button = self._button(actions, "Back", self.back, "action_back")
        self.back_button.pack(side="left", padx=(0, 6))
        self.refresh_button = self._button(actions, "Refresh", self.refresh, "action_refresh")
        self.refresh_button.pack(side="left", padx=(0, 6))
        self.download_button = self._button(actions, "Download", self.download_selected, "action_download")
        self.download_button.pack(side="left", padx=(0, 6))
        self.sync_button = self._button(actions, "Sync Folder", self.sync_selected_folder, "action_sync")
        self.sync_button.pack(side="left", padx=(0, 6))
        self.upload_file_button = self._button(actions, "Upload File", self.upload_file, "action_upload")
        self.upload_file_button.pack(side="left", padx=(0, 6))
        self.upload_folder_button = self._button(actions, "Upload Folder", self.upload_folder, "action_upload_folder")
        self.upload_folder_button.pack(side="left", padx=(0, 6))
        self.rename_button = self._button(actions, "Rename", self.rename_selected, "action_rename")
        self.rename_button.pack(side="left", padx=(0, 6))
        self.trash_button = self._button(actions, "Move to Trash", self.trash_selected, "action_trash")
        self.trash_button.pack(side="left", padx=(0, 6))
        self.logout_button = self._button(actions, "Logout", self.logout, "action_logout")
        self.logout_button.pack(side="right")
        self.logs_button = self._button(actions, "Logs", self.show_logs, "action_logs")
        self.logs_button.pack(side="right", padx=(0, 6))

        self.breadcrumbs = BreadcrumbsBar(self.root, self._navigate_breadcrumb)
        self.breadcrumbs.grid(row=1, column=0, sticky="ew", padx=4, pady=(2, 0))

        table_frame = ttk.Frame(self.root, padding=0)
        table_frame.grid(row=2, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        columns = ("name", "size", "modified", "id")
        self.table = ttk.Treeview(table_frame, columns=columns, show="tree headings", selectmode="extended")
        ttk.Style(self.root).configure("Treeview", rowheight=34)
        self.table.heading("#0", text="")
        self._configure_browser_sort_headings()
        self.table.column("#0", width=56, minwidth=56, stretch=False, anchor="center")
        self.table.column("name", width=300)
        self.table.column("size", width=110, stretch=False, anchor="e")
        self.table.column("modified", width=180, stretch=False)
        self.table.column("id", width=220)
        self.table.grid(row=0, column=0, sticky="nsew")
        self.table.bind("<Button-1>", self._clear_selection_on_empty_click, add="+")
        self.table.bind("<Double-1>", lambda _event: self.open_selected())
        self.table.bind("<Button-3>", self._show_context_menu)
        self.table.bind("<Escape>", self._clear_selection)
        self.table.bind("<<TreeviewSelect>>", lambda _event: self._update_selection_actions())
        self._register_external_drop_target(self.root)
        self._register_external_drop_target(table_frame)
        self._register_external_drop_target(self.table)

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
        self._update_transfer_states_button()

    def login(self) -> None:
        if self._busy:
            return

        base_url = self.base_url_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get()

        if not base_url or not username or not password:
            messagebox.showinfo("Login", "Base URL, username, and password are required.", parent=self.root)
            return

        self._set_busy(True, "Logging in...")
        self.login_button.configure(state="disabled")
        self.config["base_url"] = base_url
        self.config["username"] = username
        self._save_config()

        def work():
            return self._bootstrap_client(Client.login(base_url, username, password))

        self._run_worker(work, self._login_done, self._login_failed)

    def _login_done(self, result: tuple[Client, Syncer, Folder]) -> None:
        self.client, self.syncer, self.current_folder = result
        self.folder_stack = [self.current_folder]
        self._save_auth_from_client()
        self._set_busy(False, "Ready")
        self._build_browser()
        self._render_folder()

    def _login_failed(self, exc: Exception) -> None:
        self._set_busy(False, "")
        self.login_button.configure(state="normal")
        self.login_status_var.set(str(exc))
        messagebox.showerror("Login failed", str(exc), parent=self.root)

    def _try_cached_login(self) -> bool:
        token = self.auth.get("auth_token")
        device_id = self.auth.get("device_id")
        base_url = self.auth.get("base_url") or self.config.get("base_url")
        if not token or not device_id or not base_url:
            return False

        self._build_startup("Signing in...")

        def work():
            Client._validate_and_set_base(base_url)
            return self._bootstrap_client(Client(base_url, token, device_id))

        self._run_worker(work, self._login_done, self._cached_login_failed)
        return True

    def _cached_login_failed(self, exc: Exception) -> None:
        self.client = None
        self.syncer = None
        self.current_folder = None
        self._clear_auth()
        self._set_busy(False, "")
        self._build_login()
        self.login_status_var.set(f"Saved login failed ({str(exc)}). Sign in again.")

    def _bootstrap_client(self, client: Client) -> tuple[Client, Syncer, Folder]:
        syncer = client.get_syncer()
        root_folder = client.get_root()
        syncer.remote.get_item(root_folder)
        list(syncer.remote.list_children(root_folder))
        return client, syncer, root_folder

    def logout(self) -> None:
        if self._busy:
            return

        client = self.client
        self._set_busy(True, "Logging out...")

        def work():
            if client is not None:
                client.logout()

        self._run_worker(work, lambda _result: self._logout_done(), self._logout_failed)

    def _logout_done(self) -> None:
        self.client = None
        self.syncer = None
        self.current_folder = None
        self.folder_stack = []
        self.items.clear()
        self._clear_auth()
        self._set_busy(False, "")
        self._build_login()
        self.login_status_var.set("Logged out.")

    def _logout_failed(self, exc: Exception) -> None:
        self._logout_done()
        self.login_status_var.set(f"Logged out locally. Server logout failed: {exc}")

    def refresh(self) -> None:
        if self.current_folder is None or self._busy:
            return
        self._load_folder(self.current_folder, refresh=True)

    def back(self) -> None:
        if self._busy or len(self.folder_stack) <= 1:
            return
        self.folder_stack.pop()
        self._load_folder(self.folder_stack[-1], refresh=False)

    def open_selected(self) -> None:
        selected = self._selected_items()
        if len(selected) != 1:
            return

        item = selected[0]
        if isinstance(item, Folder):
            self._load_folder(item, refresh=False, push=True)
            return

        if isinstance(item, File):
            webbrowser.open(item.view_url)

    def _navigate_breadcrumb(self, folder_id: str) -> None:
        if self._busy:
            return

        for index, folder in enumerate(self.folder_stack):
            if str(folder.id) == str(folder_id):
                self.folder_stack = self.folder_stack[: index + 1]
                self._load_folder(folder, refresh=False)
                return

        self._load_folder(Folder(folder_id), refresh=False, push=True)

    def rename_selected(self) -> None:
        if self.syncer is None or self.current_folder is None or self._busy:
            return

        selected = self._selected_items()
        if len(selected) != 1:
            messagebox.showinfo("Rename", "Select exactly one item to rename.", parent=self.root)
            return

        item = selected[0]
        new_name = simpledialog.askstring("Rename", "New name", initialvalue=item.name, parent=self.root)
        if new_name is None:
            return

        new_name = new_name.strip()
        if not new_name or new_name == item.name:
            return

        self._set_busy(True, f"Renaming {item.name}...")

        def work():
            item.rename(new_name)
            self.syncer.remote.invalidate(str(self.current_folder.id))

        self._run_password_retryable_worker(work, lambda _result: self._operation_done("Rename complete", refresh=True), [item])

    def trash_selected(self) -> None:
        if self.syncer is None or self.current_folder is None or self._busy:
            return

        selected = self._selected_items()
        if not selected:
            messagebox.showinfo("Move to Trash", "Select one or more items.", parent=self.root)
            return

        if not messagebox.askyesno("Move to Trash", f"Move {len(selected)} item(s) to trash?", parent=self.root):
            return

        self._set_busy(True, f"Moving {len(selected)} item(s) to trash...")

        def work():
            common.move_to_trash(selected)
            for item in selected:
                self.syncer.remote.forget_tree(str(item.id))
            self.syncer.remote.invalidate(str(self.current_folder.id))

        self._run_password_retryable_worker(work, lambda _result: self._operation_done("Moved to trash", refresh=True), selected)

    def download_selected(self) -> None:
        if self.client is None or self.syncer is None or self._busy:
            return

        items = self._selected_items()
        if not items:
            messagebox.showinfo("Download", "Select one or more remote items.", parent=self.root)
            return

        target_dir = filedialog.askdirectory(title="Download to", initialdir=self.config.get("last_download_dir") or None)
        if not target_dir:
            return
        self.config["last_download_dir"] = target_dir
        self._save_config()

        self._set_busy(True, f"Downloading {len(items)} item(s)...")

        def work():
            downloader = self.client.get_downloader()
            self._set_active_transfer("download", downloader)
            try:
                for item in items:
                    downloader.download(data=item, target_dir=Path(target_dir))
                downloader.join()
                raise_transfer_errors(downloader, "Download")
            finally:
                self._clear_active_transfer()

        self._run_password_retryable_worker(work, lambda _result: self._operation_done("Download complete"), items)

    def upload_file(self) -> None:
        path = filedialog.askopenfilename(title="Upload file", initialdir=self.config.get("last_upload_dir") or None)
        if path:
            self.config["last_upload_dir"] = str(Path(path).parent)
            self._save_config()
            self._upload_paths([Path(path)])

    def upload_folder(self) -> None:
        path = filedialog.askdirectory(title="Upload folder", initialdir=self.config.get("last_upload_dir") or None)
        if path:
            self.config["last_upload_dir"] = path
            self._save_config()
            self._upload_paths([Path(path)])

    def sync_selected_folder(self) -> None:
        if self.client is None or self._busy:
            return

        folders = [item for item in self._selected_items() if isinstance(item, Folder)]
        if len(folders) != 1:
            messagebox.showinfo("Sync Folder", "Select exactly one remote folder.", parent=self.root)
            return

        folder = folders[0]
        if needs_resource_password(folder) and not self._prompt_resource_password(folder):
            return

        sync_dirs = self.config.setdefault("sync_dirs", {})
        initial_dir = sync_dirs.get(str(folder.id)) or self.config.get("last_sync_dir") or None
        local_dir = filedialog.askdirectory(title=f"Sync {safe_item_label(folder)} to local folder", initialdir=initial_dir)
        if not local_dir:
            return

        sync_dirs[str(folder.id)] = local_dir
        self.config["last_sync_dir"] = local_dir
        self._save_config()
        self._open_sync_gui(folder, Path(local_dir))

    def _open_sync_gui(self, folder: Folder, local_dir: Path) -> None:
        try:
            sync_gui = SyncGui(self.syncer, local_dir, folder, parent=self.root)
            self._sync_window = sync_gui.root
            sync_gui.run()
            self._sync_window = None
        except SyncGuiAlreadyOpenError:
            if self._widget_exists(self._sync_window):
                self._sync_window.lift()
                self._sync_window.focus_force()
            messagebox.showinfo("Sync Folder", "A sync window is already open.", parent=self.root)
        except BackendMissingOrIncorrectResourcePasswordError as exc:
            self._operation_failed_with_password_retry(
                exc,
                [folder],
                lambda: self._open_sync_gui(folder, local_dir),
            )

    def _upload_paths(self, paths: list[Path]) -> None:
        if self.client is None or self.current_folder is None:
            return

        existing_paths = [path for path in paths if path.exists()]
        if not existing_paths:
            messagebox.showinfo("Upload", "No existing files or folders were selected.", parent=self.root)
            return

        target_folder = self.current_folder
        if self._busy:
            if self._enqueue_active_upload(existing_paths, target_folder):
                self.status_var.set(f"Queued {len(existing_paths)} more upload item(s)...")
            return

        if len(existing_paths) == 1:
            status = f"Uploading {existing_paths[0].name}..."
        else:
            status = f"Uploading {len(existing_paths)} item(s)..."
        self._set_busy(True, status)
        with self._active_upload_queue_lock:
            self._active_upload_queue.clear()
            self._accepting_active_uploads = True

        def work():
            uploader = self.client.get_uploader()
            self._set_active_transfer("upload", uploader)
            try:
                batch = [(path, target_folder) for path in existing_paths]
                while batch:
                    for path, parent in batch:
                        uploader.upload(path, parent=parent)
                    uploader.join()
                    batch = self._take_queued_active_uploads()
                raise_transfer_errors(uploader, "Upload")
            finally:
                with self._active_upload_queue_lock:
                    self._accepting_active_uploads = False
                    self._active_upload_queue.clear()
                self._clear_active_transfer()

        self._run_password_retryable_worker(work, lambda _result: self._operation_done("Upload complete", refresh=True), [target_folder])

    def _enqueue_active_upload(self, paths: list[Path], parent: Folder) -> bool:
        with self._active_transfer_lock:
            is_active_upload = self._active_transfer is not None and self._last_transfer_direction == "upload"
        if not is_active_upload:
            return False

        with self._active_upload_queue_lock:
            if not self._accepting_active_uploads:
                return False
            self._active_upload_queue.extend((path, parent) for path in paths)
            return True

    def _take_queued_active_uploads(self) -> list[tuple[Path, Folder]]:
        with self._active_upload_queue_lock:
            if not self._active_upload_queue:
                self._accepting_active_uploads = False
                return []
            batch = self._active_upload_queue
            self._active_upload_queue = []
            return batch

    def _register_external_drop_target(self, widget) -> None:
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._drop_external_paths)
        except tk.TclError:
            return

    def _drop_external_paths(self, event) -> str:
        paths = [Path(path) for path in self.root.tk.splitlist(event.data)]
        if paths:
            self.config["last_upload_dir"] = str(paths[0].parent)
            self._save_config()
            self._upload_paths(paths)
        return "copy"

    def abort_transfer(self) -> None:
        with self._active_transfer_lock:
            transfer = self._active_transfer

        if transfer is None:
            return

        self.transfer_status.set_aborting()
        self._transfer_cancelled = True
        threading.Thread(target=lambda: transfer.shutdown(cancel_pending=True), daemon=True).start()

    def toggle_transfer_pause(self) -> None:
        with self._active_transfer_lock:
            transfer = self._active_transfer

        if transfer is None:
            return

        ctx = getattr(transfer, "ctx", None)
        is_paused = getattr(ctx, "is_paused", None)
        paused = bool(is_paused()) if callable(is_paused) else False

        if paused:
            transfer.resume_all()
            self.transfer_status.set_paused(False)
        else:
            transfer.pause_all()
            self.transfer_status.set_paused(True)

    def _load_folder(self, folder: Folder, *, refresh: bool, push: bool = False) -> None:
        if needs_resource_password(folder) and not self._prompt_resource_password(folder):
            return

        self._set_busy(True, f"Loading {safe_item_label(folder)}...")

        def work():
            if self.syncer is None:
                raise RuntimeError("Not logged in")

            folder_id = str(folder.id)
            if refresh:
                self.syncer.remote.invalidate(folder_id)
            loaded_folder = self.syncer.remote.get_item(folder_id)
            if not isinstance(loaded_folder, Folder):
                raise TypeError(f"Remote item is not a folder: {folder_id}")
            list(self.syncer.remote.list_children(loaded_folder))
            return loaded_folder

        def done(loaded_folder: Folder):
            self.current_folder = loaded_folder
            if push:
                self.folder_stack.append(loaded_folder)
            self._set_busy(False, "Ready")
            self._render_folder()

        self._run_password_retryable_worker(work, done, [folder])

    def _render_folder(self) -> None:
        if self.current_folder is None:
            return

        self.items.clear()
        for row in self.table.get_children():
            self.table.delete(row)

        if self.syncer is None:
            return

        children = [
            self.syncer.remote.require_cached_item(str(node.uid))
            for node in self.syncer.remote.list_children(self.current_folder)
        ]
        children = self._sort_browser_items(children)
        self.breadcrumbs.set_items(self.current_folder.breadcrumbs)
        for index, item in enumerate(children):
            row_id = str(index)
            self.items[row_id] = item
            self.table.insert(
                "",
                "end",
                iid=row_id,
                image=self.icons["folder" if item.is_dir else file_icon_key(item.name)],
                values=(
                    item.name,
                    "" if item.is_dir else self._format_bytes(item.size),
                    self._format_datetime(item.last_modified_at),
                    item.id,
                ),
            )

        self.back_button.configure(state="normal" if len(self.folder_stack) > 1 and not self._busy else "disabled")
        self._update_selection_actions()
        self.status_var.set(f"{len(children)} item(s)")

    def _selected_items(self) -> list[Item]:
        return [self.items[item_id] for item_id in self.table.selection() if item_id in self.items]

    def _clear_selection_on_empty_click(self, event) -> None:
        if self.table.identify_row(event.y):
            return
        self._clear_selection()

    def _clear_selection(self, _event=None) -> str:
        selection = self.table.selection()
        if selection:
            self.table.selection_remove(selection)
        self._update_selection_actions()
        return "break"

    def _show_context_menu(self, event) -> None:
        row_id = self.table.identify_row(event.y)
        if row_id:
            if row_id not in self.table.selection():
                self.table.selection_set(row_id)

        selected = self._selected_items()
        menu = tk.Menu(self.root, tearoff=False)

        if row_id:
            if len(selected) == 1 and isinstance(selected[0], Folder):
                self._menu_command(menu, "Open", self.open_selected, "action_open")
                self._menu_command(menu, "Sync", self.sync_selected_folder, "action_sync")
                menu.add_separator()
            self._menu_command(menu, "Download", self.download_selected, "action_download", state="normal" if selected else "disabled")
            menu.add_separator()
            self._menu_command(menu, "Info", self.show_selected_info, "action_details", state="normal" if len(selected) == 1 else "disabled")
            self._menu_command(menu, "Rename", self.rename_selected, "action_rename", state="normal" if len(selected) == 1 else "disabled")
            self._menu_command(menu, "Move to Trash", self.trash_selected, "action_trash", state="normal" if selected else "disabled")
            menu.add_separator()
        else:
            self.table.selection_remove(self.table.selection())
            self._menu_command(menu, "Upload File", self.upload_file, "action_upload")
            self._menu_command(menu, "Upload Folder", self.upload_folder, "action_upload_folder")
            menu.add_separator()

        self._menu_command(menu, "Refresh", self.refresh, "action_refresh")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _update_selection_actions(self) -> None:
        selected = self._selected_items()
        folder_selected = len(selected) == 1 and isinstance(selected[0], Folder)
        if self._widget_exists(getattr(self, "download_button", None)):
            self.download_button.configure(state="normal" if selected and not self._busy else "disabled")
        if self._widget_exists(getattr(self, "sync_button", None)):
            self.sync_button.configure(state="normal" if folder_selected and not self._busy else "disabled")
        if self._widget_exists(getattr(self, "rename_button", None)):
            self.rename_button.configure(state="normal" if len(selected) == 1 and not self._busy else "disabled")
        if self._widget_exists(getattr(self, "trash_button", None)):
            self.trash_button.configure(state="normal" if selected and not self._busy else "disabled")

    def show_selected_info(self) -> None:
        if self._busy:
            return

        selected = self._selected_items()
        if len(selected) != 1:
            messagebox.showinfo("Info", "Select exactly one item to inspect.", parent=self.root)
            return

        item = selected[0]
        self._set_busy(True, f"Loading info for {safe_item_label(item)}...")

        def work():
            item.refresh()
            return self._build_item_info_text(item)

        def done(text: str) -> None:
            self._set_busy(False, "Ready")
            self._show_copyable_details("Info", text)

        self._run_password_retryable_worker(work, done, [item])

    def _operation_done(self, message: str, *, refresh: bool = False) -> None:
        if self._transfer_cancelled:
            message = "Transfer aborted"
            refresh = False
        self._reset_transfer_progress()
        message = self._transfer_result_message(message)
        self._set_busy(False, message)
        if refresh:
            self.refresh()

    def _operation_failed(self, exc: Exception) -> None:
        if self._transfer_cancelled:
            self._reset_transfer_progress()
            self._set_busy(False, self._transfer_result_message("Transfer aborted"))
            return
        self._reset_transfer_progress()
        self._set_busy(False, self._transfer_result_message("Transfer failed"))
        messagebox.showerror("Error", str(exc), parent=self.root)

    def _operation_failed_with_password_retry(self, exc: Exception, items: list[Item | None], retry) -> None:
        if not isinstance(exc, BackendMissingOrIncorrectResourcePasswordError):
            self._operation_failed(exc)
            return

        self._reset_transfer_progress()
        self._set_busy(False, "Password required")
        item = password_prompt_item(exc, items)
        if item is None or self.syncer is None:
            messagebox.showerror("Password required", str(exc), parent=self.root)
            return

        if self._prompt_resource_password(item):
            retry()

    def _prompt_resource_password(self, item: Item) -> bool:
        return prompt_resource_password(self.root, item, self._remember_resource_password)

    def _remember_resource_password(self, item: Item, password: str) -> None:
        item.set_password(password)
        if self.syncer is not None:
            self.syncer.remote.set_item_password(item, password)

    def _run_worker(self, work, done, failed) -> None:
        def target():
            try:
                result = work()
            except Exception as exc:
                self._ui_queue.put(("failed", failed, exc))
            else:
                self._ui_queue.put(("done", done, result))

        threading.Thread(target=target, daemon=True).start()

    def _run_password_retryable_worker(self, work, done, password_items: list[Item | None]) -> None:
        def failed(exc: Exception) -> None:
            self._operation_failed_with_password_retry(
                exc,
                password_items,
                lambda: self._run_password_retryable_worker(work, done, password_items),
            )

        self._run_worker(work, done, failed)

    def show_logs(self) -> None:
        if self._widget_exists(self._log_window):
            self._log_window.lift()
            self._log_window.focus_force()
            return

        window = tk.Toplevel(self.root)
        window.title("Logs")
        window.geometry("900x420")
        window.minsize(520, 260)
        window.transient(self.root)
        apply_window_icon(window)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(0, weight=1)

        frame = ttk.Frame(window, padding=8)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        filters = ttk.Frame(frame)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(filters, text="Type").pack(side="left", padx=(0, 6))
        filter_box = ttk.Combobox(
            filters,
            textvariable=self._log_filter_var,
            values=("All", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "EXCEPTION"),
            state="readonly",
            width=12,
        )
        filter_box.pack(side="left")
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self._render_log_buffer())

        text = tk.Text(frame, wrap="word", state="disabled", font=("Consolas", 10))
        text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="Clear", command=self._clear_logs).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Close", command=window.destroy).pack(side="left")

        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.bind("<Escape>", lambda _event: window.destroy())
        self._log_window = window
        self._log_text = text
        self._discard_pending_log_updates()
        self._render_log_buffer()

    def show_transfer_states(self) -> None:
        direction, transfer = self._get_report_transfer()
        if transfer is None:
            messagebox.showinfo("File States", "No upload or download state is available yet.", parent=self.root)
            return

        if self._widget_exists(self._state_window):
            self._state_window.lift()
            self._state_window.focus_force()
            self._refresh_transfer_states()
            return

        window = tk.Toplevel(self.root)
        window.title(f"{direction.capitalize()} File States")
        window.geometry("980x480")
        window.minsize(700, 320)
        window.transient(self.root)
        apply_window_icon(window)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)

        summary = ttk.Label(window, text="")
        summary.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        self._state_summary = summary

        frame = ttk.Frame(window, padding=(10, 0, 10, 10))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        columns = ("added", "status", "name", "path", "progress", "reason")
        table = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
        state_titles = {
            "added": "Added",
            "status": "Status",
            "name": "Name",
            "path": "Path",
            "progress": "Progress",
            "reason": "Reason",
        }
        self._state_column_titles = state_titles
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

        failed = sum(1 for row in rows if row["is_failed"] == "1")
        aborted = sum(1 for row in rows if row["is_aborted"] == "1")
        if self._widget_exists(getattr(self, "_state_summary", None)):
            self._state_summary.configure(text=f"{direction.capitalize() if direction else 'Transfer'}: {len(rows)} file(s), {failed} failed, {aborted} aborted")
        if self._active_transfer is not None:
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
                    "reason": self._format_error_reason(error),
                    "is_failed": "1",
                    "is_aborted": "0",
                }
            )
        for state_id, state in ctx.get_all_states().items():
            with getattr(state, "lock", threading.Lock()):
                status = getattr(getattr(state, "status", ""), "value", getattr(state, "status", ""))
                error = getattr(state, "error", None)
                row = self._transfer_state_row(direction, str(state_id), state, records.get(state_id), str(status), error)
            rows.append(row)
        rows.sort(key=self._transfer_state_sort_key, reverse=self._state_sort_reverse)
        return rows

    def _transfer_result_message(self, message: str) -> str:
        direction, transfer = self._get_report_transfer()
        rows = self._collect_transfer_state_rows(direction, transfer)
        failed = sum(1 for row in rows if row["is_failed"] == "1")
        aborted = sum(1 for row in rows if row["is_aborted"] == "1")
        if failed:
            return f"{message} ({failed} failed - see File States)"
        if aborted:
            return f"{message} ({aborted} aborted - see File States)"
        return message

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

        is_aborted = status == "aborted"
        reason = self._format_error_reason(error)
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
            "reason": reason,
            "is_failed": "1" if status in ("failed", "save_failed") else "0",
            "is_aborted": "1" if is_aborted else "0",
        }

    def _format_error_reason(self, error) -> str:
        if error is None:
            return ""

        parts = []
        error_type = error.__class__.__name__
        message = getattr(error, "message", None) or str(error) or repr(error)
        parts.append(f"{error_type}: {message}")

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

    def _sort_browser_table(self, column: str) -> None:
        if self._browser_sort_column == column:
            self._browser_sort_reverse = not self._browser_sort_reverse
        else:
            self._browser_sort_column = column
            self._browser_sort_reverse = False
        self._configure_browser_sort_headings()
        self._render_folder()

    def _browser_item_sort_key(self, item: Item):
        column = self._browser_sort_column
        if column == "size":
            return 0 if item.is_dir else int(getattr(item, "size", 0) or 0)
        if column == "modified":
            return getattr(item, "last_modified_at", None) or datetime.min
        if column == "id":
            return str(item.id).lower()
        return item.name.lower()

    def _sort_browser_items(self, items: list[Item]) -> list[Item]:
        sorted_items = sorted(items, key=self._browser_item_sort_key, reverse=self._browser_sort_reverse)
        return sorted(sorted_items, key=lambda item: not item.is_dir)

    def _configure_browser_sort_headings(self) -> None:
        titles = {
            "name": "Name",
            "size": "Size",
            "modified": "Modified",
            "id": "ID",
        }
        self.table.heading("#0", text="")
        for column, title in titles.items():
            self.table.heading(column, text=self._sort_heading_title(title, column, self._browser_sort_column, self._browser_sort_reverse), command=lambda c=column: self._sort_browser_table(c))

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
            table.heading(column, text=self._sort_heading_title(title, column, self._state_sort_column, self._state_sort_reverse), command=lambda c=column: self._sort_transfer_states(c))

    @staticmethod
    def _sort_heading_title(title: str, column: str, active_column: str, reverse: bool) -> str:
        if column != active_column:
            return title
        return f"{title} {'v' if reverse else '^'}"

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

    def _build_item_info_text(self, item: Item) -> str:
        details = [
            f"Kind: {'Folder' if isinstance(item, Folder) else 'File'}",
            f"ID: {item.id}",
            f"Name: {self._value_or_dash(item.name)}",
            f"Parent ID: {self._value_or_dash(item.parent_id)}",
            f"Created: {self._value_or_dash(item.created_at)}",
            f"Last Modified: {self._value_or_dash(item.last_modified_at)}",
            f"In Trash Since: {self._value_or_dash(self._nullable_datetime(item, 'in_trash_since'))}",
            f"Locked: {item.is_locked}",
            f"Lock From: {self._value_or_dash(item.lock_from)}",
        ]

        if isinstance(item, File):
            details.extend(self._file_info_details(item))
        elif isinstance(item, Folder):
            details.extend(self._folder_info_details(item))

        return "\n".join(details)

    def _file_info_details(self, item: File) -> list[str]:
        details = [
            "",
            "File:",
            f"Size: {self._format_bytes(item.size)} ({self._value_or_dash(item.size)} bytes)",
            f"Extension: {self._value_or_dash(item.extension)}",
            f"Type: {self._value_or_dash(item.type)}",
            f"Encryption Method: {self._value_or_dash(item.encryption_method)}",
            f"CRC: {self._value_or_dash(item.crc)}",
            f"Duration: {self._value_or_dash(self._nullable_attr(item, 'duration'))}",
            f"Video Position: {self._value_or_dash(item.video_position)}",
            f"Media Position: {self._value_or_dash(getattr(item, '_media_position', None))}",
            f"Thumbnail URL: {self._value_or_dash(item.thumbnail_url)}",
            f"Download URL: {self._value_or_dash(item.download_url)}",
            f"View URL: {self._value_or_dash(item.view_url if item.download_url else None)}",
            f"Has Video Metadata: {item.isVideoMetadata}",
            f"Has RAW Metadata: {item.isRawMetadata}",
            f"Has Photo Metadata: {item.isPhotoMetadata}",
            f"Has Subtitles: {self._value_or_dash(getattr(item, '_hasSubtitles', None))}",
            f"Encryption Key: {self._value_or_dash(getattr(item, '_encryption_key', None))}",
            f"Encryption IV: {self._value_or_dash(getattr(item, '_encryption_iv', None))}",
        ]

        raw_metadata = self._nullable_attr(item, "rawMetadata")
        if raw_metadata is not None:
            details.extend(("", "RAW Metadata:", *self._object_fields(raw_metadata)))

        photo_metadata = self._nullable_attr(item, "photoMetadata")
        if photo_metadata is not None:
            details.extend(("", "Photo Metadata:", *self._object_fields(photo_metadata)))

        video_metadata = self._nullable_attr(item, "videoMetadata")
        if video_metadata is not None:
            details.extend(("", "Video Metadata:", *self._video_metadata_fields(video_metadata)))

        details.extend(("", "Tags:", *self._collection_fields(self._nullable_collection(item, "tags"))))
        details.extend(("", "Moments:", *self._collection_fields(self._nullable_collection(item, "moments"))))
        details.extend(("", "Subtitles:", *self._collection_fields(self._nullable_collection(item, "subtitles"))))

        return details

    def _folder_info_details(self, item: Folder) -> list[str]:
        details = [
            "",
            "Folder:",
            f"Folder Size: {self._format_bytes(item.folder_size)} ({self._value_or_dash(item.folder_size)} bytes)",
            f"File Count: {self._value_or_dash(item.file_count)}",
            f"Folder Count: {self._value_or_dash(item.folder_count)}",
            f"Hash: {self._value_or_dash(item.hash)}",
            f"Children: {len(item.children) if item.children is not None else 0}",
        ]

        details.extend(("", "Breadcrumbs:"))
        if item.breadcrumbs:
            for breadcrumb in item.breadcrumbs:
                details.append(f"- {breadcrumb.name} | ID: {breadcrumb.id} | Lock From: {self._value_or_dash(breadcrumb.lockFrom)}")
        else:
            details.append("-")

        try:
            stats = item.get_stats()
            details.extend(("", "Stats:", f"Total: {stats.total}", f"Used: {stats.used}"))
        except Exception as exc:
            details.extend(("", "Stats:", f"Error: {exc}"))

        try:
            usage = item.get_usage()
            details.extend(("", "Usage:", *self._dict_fields(usage)))
        except Exception as exc:
            details.extend(("", "Usage:", f"Error: {exc}"))

        return details

    def _show_copyable_details(self, title: str, text: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.geometry("760x500")
        dialog.minsize(520, 320)
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

    def _nullable_datetime(self, item: Item, attr_name: str):
        try:
            return getattr(item, attr_name)
        except (TypeError, ValueError):
            return None

    def _nullable_attr(self, item: object, attr_name: str):
        try:
            return getattr(item, attr_name)
        except AttributeError:
            return None

    def _nullable_collection(self, item: object, attr_name: str):
        try:
            return getattr(item, attr_name)
        except Exception as exc:
            return [f"Error: {exc}"]

    def _object_fields(self, value: object) -> list[str]:
        data = getattr(value, "__dict__", None)
        if not data:
            return [str(value)]
        return [f"{key.lstrip('_')}: {self._value_or_dash(field_value)}" for key, field_value in data.items()]

    def _dict_fields(self, value: dict) -> list[str]:
        if not value:
            return ["-"]
        return [f"{key}: {self._value_or_dash(field_value)}" for key, field_value in value.items()]

    def _collection_fields(self, values) -> list[str]:
        if not values:
            return ["Count: 0"]

        details = [f"Count: {len(values)}"]
        for index, value in enumerate(values, start=1):
            if isinstance(value, str):
                details.append(value)
                continue
            details.extend((f"Item {index}:", *self._object_fields(value)))
        return details

    def _video_metadata_fields(self, value: object) -> list[str]:
        details = [
            f"Brands: {self._value_or_dash(value.brands)}",
            f"MIME: {self._value_or_dash(value.mime)}",
            f"Has IOD: {self._value_or_dash(value.has_IOD)}",
            f"Has MOOV: {self._value_or_dash(value.has_moov)}",
            f"Progressive: {self._value_or_dash(value.is_progressive)}",
            f"Fragmented: {self._value_or_dash(value.is_fragmented)}",
            f"Video Tracks: {len(value.video_tracks)}",
            f"Audio Tracks: {len(value.audio_tracks)}",
            f"Subtitle Tracks: {len(value.subtitle_tracks)}",
        ]
        for label, tracks in (("Video Track", value.video_tracks), ("Audio Track", value.audio_tracks), ("Subtitle Track", value.subtitle_tracks)):
            for index, track in enumerate(tracks, start=1):
                details.extend((f"{label} {index}:", *self._object_fields(track)))
        return details

    @staticmethod
    def _value_or_dash(value) -> str:
        if value is None or value == "":
            return "-"
        return str(value)

    def _install_log_capture(self) -> None:
        self._stdout_proxy = _GuiStream(self._original_stdout, self._enqueue_log, "STDOUT")
        self._stderr_proxy = _GuiStream(self._original_stderr, self._enqueue_log, "STDERR")
        sys.stdout = self._stdout_proxy
        sys.stderr = self._stderr_proxy

        handler = _GuiLogHandler(self._enqueue_log)
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
        self._log_handler = handler

    def _enqueue_log(self, text: str, level_name: str = "OUTPUT") -> None:
        entry = (level_name, text)
        with self._log_buffer_lock:
            self._log_buffer.append(entry)
            total_chars = sum(len(part[1]) if isinstance(part, tuple) else len(part) for part in self._log_buffer)
            while self._log_buffer and total_chars > self._max_log_chars:
                old = self._log_buffer.pop(0)
                total_chars -= len(old[1]) if isinstance(old, tuple) else len(old)
        self._log_queue.put(entry)

    def _poll_log_queue(self) -> None:
        try:
            while True:
                level_name, text = self._log_queue.get_nowait()
                self._append_log_text(level_name, text)
        except queue.Empty:
            pass
        if self._widget_exists(self.root):
            self.root.after(100, self._poll_log_queue)

    def _append_log_text(self, level_name: str, text: str) -> None:
        if not self._widget_exists(self._log_text):
            return
        if not self._log_entry_visible(level_name):
            return
        self._log_text.configure(state="normal")
        self._log_text.insert("end", text)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _discard_pending_log_updates(self) -> None:
        try:
            while True:
                self._log_queue.get_nowait()
        except queue.Empty:
            pass

    def _render_log_buffer(self) -> None:
        if not self._widget_exists(self._log_text):
            return
        with self._log_buffer_lock:
            text = "".join(
                entry[1] if isinstance(entry, tuple) else entry
                for entry in self._log_buffer
                if self._log_entry_visible(entry[0] if isinstance(entry, tuple) else "OUTPUT")
            )
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.insert("end", text)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _log_entry_visible(self, level_name: str) -> bool:
        selected = self._log_filter_var.get() if self._log_filter_var is not None else "All"
        if level_name in ("STDOUT", "STDERR"):
            return True
        if selected == "All":
            return True

        levels = {
            "DEBUG": logging.DEBUG,
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "EXCEPTION": logging.ERROR,
            "CRITICAL": logging.CRITICAL,
        }
        selected_level = levels.get(selected)
        entry_level = levels.get(level_name)
        if selected_level is None or entry_level is None:
            return False
        return entry_level >= selected_level

    def _clear_logs(self) -> None:
        with self._log_buffer_lock:
            self._log_buffer.clear()
        if not self._widget_exists(self._log_text):
            return
        self._log_text.configure(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.configure(state="disabled")

    def _close(self) -> None:
        if self._log_handler is not None:
            logger.removeHandler(self._log_handler)
            self._log_handler = None
        if self._stdout_proxy is not None and sys.stdout is self._stdout_proxy:
            sys.stdout = self._original_stdout
        if self._stderr_proxy is not None and sys.stderr is self._stderr_proxy:
            sys.stderr = self._original_stderr
        self.root.destroy()

    def _start_version_check(self) -> None:
        def work():
            try:
                return check_for_update()
            except Exception:
                logger.exception("Error while checking for update")
                return None

        self._run_worker(work, self._version_check_done, lambda _exc: None)

    def _version_check_done(self, update: UpdateInfo | None) -> None:
        if update is None or not self._widget_exists(self.root):
            return

        # if self.config.get("dismissed_update_version") == update.latest_version:
        #     return

        if update.is_frozen:
            message = (
                f"Version {update.latest_version} is available.\n\n"
                f"You are running {update.current_version}.\n\n"
                "Open the GitHub release page to download the new browser GUI?"
            )
        else:
            message = (
                f"Version {update.latest_version} is available.\n\n"
                f"You are running {update.current_version}.\n\n"
                "Open the GitHub release page to download the new browser GUI?"
            )

        if messagebox.askyesno("Update available", message, parent=self.root):
            webbrowser.open(update.release_url)
        else:
            self.config["dismissed_update_version"] = update.latest_version
            self._save_config()

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                kind, callback, payload = self._ui_queue.get_nowait()
                callback(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_ui_queue)

    def _set_busy(self, busy: bool, status: str) -> None:
        self._busy = busy
        if hasattr(self, "status_var"):
            self.status_var.set(status)
        if hasattr(self, "login_status_var"):
            self.login_status_var.set(status)

        for name in ("back_button", "refresh_button", "download_button", "sync_button", "upload_file_button", "upload_folder_button", "rename_button", "trash_button", "logout_button"):
            button = getattr(self, name, None)
            if self._widget_exists(button):
                button.configure(state="disabled" if busy else "normal")
        if not busy and self._widget_exists(getattr(self, "sync_button", None)):
            self._update_selection_actions()
        if hasattr(self, "transfer_status") and self._widget_exists(getattr(self.transfer_status, "parent", None)):
            with self._active_transfer_lock:
                has_transfer = self._active_transfer is not None
            self.transfer_status.set_abort_enabled(busy and has_transfer)
            self.transfer_status.set_pause_enabled(busy and has_transfer)

    def _clear_root(self) -> None:
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                continue
            child.destroy()
        for name in (
            "back_button",
            "refresh_button",
            "download_button",
            "sync_button",
            "upload_file_button",
            "upload_folder_button",
            "rename_button",
            "trash_button",
            "logout_button",
            "login_button",
            "table",
            "breadcrumbs",
            "transfer_status",
            "transfer_states_button",
            "logs_button",
        ):
            if hasattr(self, name):
                setattr(self, name, None)

    def _widget_exists(self, widget) -> bool:
        if widget is None:
            return False
        try:
            return bool(widget.winfo_exists())
        except tk.TclError:
            return False

    def _show_callback_exception(self, exc_type, exc, tb) -> None:
        logger.exception(exc)
        messagebox.showerror("Error", "".join(traceback.format_exception_only(exc_type, exc)).strip(), parent=self.root)

    def _load_config(self) -> dict:
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2, sort_keys=True)

    def _load_auth(self) -> dict:
        if not self.auth_path.exists():
            return {}
        try:
            with open(self.auth_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_auth_from_client(self) -> None:
        self.auth = {
            "base_url": APIConfig.base_url,
            "username": self.config.get("username") or self.auth.get("username", ""),
            "auth_token": APIConfig.token,
            "device_id": APIConfig.device_id,
        }
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.auth_path, "w", encoding="utf-8") as f:
            json.dump(self.auth, f, indent=2, sort_keys=True)

    def _clear_auth(self) -> None:
        self.auth = {}
        if self.auth_path.exists():
            self.auth_path.unlink()

    def _create_icons(self) -> dict[str, tk.PhotoImage]:
        return {
            "folder": self._create_folder_icon(),
            "file": self._create_file_icon("#8a8f98", "F"),
            "file_image": self._create_file_icon("#2f9e44", "I"),
            "file_video": self._create_file_icon("#7048e8", "V"),
            "file_audio": self._create_file_icon("#d6336c", "A"),
            "file_text": self._create_file_icon("#1971c2", "T"),
            "file_archive": self._create_file_icon("#e67700", "Z"),
            "file_code": self._create_file_icon("#0b7285", "C"),
            "file_pdf": self._create_file_icon("#c92a2a", "P"),
            "action_back": self._create_action_icon("back"),
            "action_refresh": self._create_action_icon("refresh"),
            "action_download": self._create_action_icon("download"),
            "action_sync": self._create_action_icon("sync"),
            "action_upload": self._create_action_icon("upload"),
            "action_upload_folder": self._create_action_icon("upload_folder"),
            "action_open": self._create_action_icon("open"),
            "action_details": self._create_action_icon("details"),
            "action_rename": self._create_action_icon("rename"),
            "action_trash": self._create_action_icon("trash"),
            "action_cancel": self._create_action_icon("cancel"),
            "action_pause": self._create_action_icon("pause"),
            "action_resume": self._create_action_icon("resume"),
            "action_logout": self._create_action_icon("logout"),
            "action_logs": self._create_action_icon("logs"),
        }

    def _button(self, parent, text: str, command, icon_key: str) -> ttk.Button:
        return ttk.Button(parent, text=text, image=self.icons[icon_key], compound="left", command=command)

    def _menu_command(self, menu: tk.Menu, label: str, command, icon_key: str, state: str = "normal") -> None:
        menu.add_command(
            label=label,
            image=self.icons[icon_key],
            compound="left",
            command=command,
            state=state,
        )

    def _create_folder_icon(self) -> tk.PhotoImage:
        image = Image.new("RGBA", (28, 28), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((3, 8, 24, 24), radius=2, fill="#d9a928", outline="#9b7418")
        draw.rounded_rectangle((3, 5, 13, 11), radius=2, fill="#efc85a", outline="#b0831e")
        draw.rounded_rectangle((4, 10, 25, 25), radius=2, fill="#f4c542", outline="#b0831e")
        return ImageTk.PhotoImage(image)

    def _create_file_icon(self, color: str, label: str) -> tk.PhotoImage:
        image = Image.new("RGBA", (28, 28), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((6, 3, 22, 25), radius=2, fill="#ffffff", outline=color, width=2)
        draw.polygon((16, 3, 22, 9, 16, 9), fill="#e9ecef", outline=color)
        font = self._icon_font(8)
        bbox = draw.textbbox((0, 0), label, font=font)
        draw.text((14 - (bbox[2] - bbox[0]) // 2, 14), label, fill=color, font=font)
        return ImageTk.PhotoImage(image)

    def _create_action_icon(self, kind: str) -> tk.PhotoImage:
        image = Image.new("RGBA", (18, 18), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        blue = "#2563eb"
        green = "#2f9e44"
        red = "#d92d20"
        gray = "#475467"

        if kind == "back":
            draw.polygon([(5, 9), (10, 4), (10, 7), (15, 7), (15, 11), (10, 11), (10, 14)], fill=gray)
        elif kind == "refresh":
            draw.arc((3, 3, 15, 15), 35, 305, fill=blue, width=2)
            draw.polygon([(14, 3), (15, 8), (11, 6)], fill=blue)
        elif kind == "download":
            draw.line((9, 3, 9, 12), fill=green, width=2)
            draw.polygon([(5, 9), (9, 14), (13, 9)], fill=green)
            draw.line((4, 15, 14, 15), fill=gray, width=2)
        elif kind == "upload":
            draw.line((9, 15, 9, 6), fill=blue, width=2)
            draw.polygon([(5, 9), (9, 4), (13, 9)], fill=blue)
            draw.line((4, 16, 14, 16), fill=gray, width=2)
        elif kind == "upload_folder":
            draw.rounded_rectangle((2, 7, 16, 15), radius=2, fill="#f4c542", outline="#b0831e")
            draw.line((9, 15, 9, 5), fill=blue, width=2)
            draw.polygon([(5, 8), (9, 3), (13, 8)], fill=blue)
        elif kind == "sync":
            draw.arc((3, 2, 15, 10), 15, 180, fill=blue, width=2)
            draw.arc((3, 8, 15, 16), 195, 360, fill=green, width=2)
            draw.polygon([(4, 4), (7, 2), (7, 6)], fill=blue)
            draw.polygon([(14, 14), (11, 16), (11, 12)], fill=green)
        elif kind == "open":
            draw.rectangle((4, 6, 14, 15), outline=blue, width=2)
            draw.line((8, 10, 15, 3), fill=blue, width=2)
            draw.polygon([(15, 3), (15, 8), (10, 3)], fill=blue)
        elif kind == "details":
            draw.ellipse((8, 3, 10, 5), fill=blue)
            draw.line((9, 8, 9, 14), fill=blue, width=2)
            draw.ellipse((3, 3, 15, 15), outline=gray, width=2)
        elif kind == "rename":
            draw.line((4, 14, 14, 4), fill=blue, width=2)
            draw.polygon([(13, 3), (15, 5), (14, 7), (11, 4)], fill=blue)
            draw.line((3, 16, 15, 16), fill=gray, width=2)
        elif kind == "trash":
            draw.rectangle((5, 7, 13, 16), outline=red, width=2)
            draw.line((4, 5, 14, 5), fill=red, width=2)
            draw.line((7, 3, 11, 3), fill=red, width=2)
        elif kind == "cancel":
            draw.ellipse((3, 3, 15, 15), outline=red, width=2)
            draw.line((6, 6, 12, 12), fill=red, width=2)
            draw.line((12, 6, 6, 12), fill=red, width=2)
        elif kind == "pause":
            draw.rectangle((5, 4, 7, 14), fill=gray)
            draw.rectangle((11, 4, 13, 14), fill=gray)
        elif kind == "resume":
            draw.polygon([(6, 4), (14, 9), (6, 14)], fill=green)
        elif kind == "logout":
            draw.rectangle((3, 4, 10, 14), outline=gray, width=2)
            draw.line((9, 9, 15, 9), fill=red, width=2)
            draw.polygon([(13, 6), (16, 9), (13, 12)], fill=red)
        elif kind == "logs":
            draw.rectangle((4, 3, 14, 15), outline=gray, width=2)
            draw.line((6, 7, 12, 7), fill=blue, width=1)
            draw.line((6, 10, 12, 10), fill=blue, width=1)
            draw.line((6, 13, 10, 13), fill=blue, width=1)
        return ImageTk.PhotoImage(image)

    def _set_active_transfer(self, direction: str, transfer) -> None:
        with self._active_transfer_lock:
            self._active_transfer = transfer
            self._last_transfer = transfer
            self._last_transfer_direction = direction
            self._transfer_cancelled = False
        self._ui_queue.put(("done", lambda _payload: self._attach_active_transfer_ui(direction, transfer), None))

    def _clear_active_transfer(self) -> None:
        with self._active_transfer_lock:
            if self._active_transfer is not None:
                self._last_transfer = self._active_transfer
            self._active_transfer = None
        self._ui_queue.put(("done", lambda _payload: self._update_transfer_states_button(), None))

    def _reset_transfer_progress(self) -> None:
        self._transfer_cancelled = False
        if hasattr(self, "transfer_status"):
            self.transfer_status.reset()
        self._update_transfer_states_button()

    def _attach_active_transfer_ui(self, direction: str, transfer) -> None:
        if hasattr(self, "transfer_status") and self._widget_exists(getattr(self.transfer_status, "parent", None)):
            self.transfer_status.attach_transfer(direction, transfer)
        self._update_transfer_states_button()

    def _get_report_transfer(self):
        with self._active_transfer_lock:
            if self._active_transfer is not None:
                return self._last_transfer_direction, self._active_transfer
            return self._last_transfer_direction, self._last_transfer

    def _update_transfer_states_button(self) -> None:
        button = getattr(self, "transfer_states_button", None)
        if not self._widget_exists(button):
            return
        _direction, transfer = self._get_report_transfer()
        button.configure(state="normal" if transfer is not None else "disabled")

    def _icon_font(self, size: int):
        try:
            return ImageFont.truetype("segoeui.ttf", size)
        except OSError:
            return ImageFont.load_default()

    def _format_datetime(self, value) -> str:
        return "" if value is None else str(value)

    def _format_bytes(self, value: int | None) -> str:
        if value is None:
            return ""
        return TransferStatusBar.format_bytes(value)


def main() -> None:
    BrowserGuiApp().run()


if __name__ == "__main__":
    main()
