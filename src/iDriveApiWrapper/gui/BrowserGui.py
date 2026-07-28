from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
import traceback
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError as exc:
    raise RuntimeError(
        "The remote browser GUI requires optional GUI dependencies. "
        "Install them with `pip install iDriveApiWrapper[gui]`. "
        "Install transfer support too with `pip install iDriveApiWrapper[transfer]`, "
        "or install everything with `pip install iDriveApiWrapper[all]`."
    ) from exc

from ..Config import APIConfig
from ..iDrive import Client
from ..exceptions import BackendMissingOrIncorrectResourcePasswordError
from ..models.File import File
from ..models.Folder import Folder
from ..models.Item import Item
from ..state.Storage import IdriveStorage
from ..syncer.Syncer import Syncer
from .transfer_errors import raise_transfer_errors
from .BreadcrumbsBar import BreadcrumbsBar
from .GuiUtils import apply_window_icon, file_icon_key, needs_resource_password, password_prompt_item, prompt_resource_password, safe_item_label, set_windows_app_user_model_id
from .SyncGui import SyncGui, SyncGuiAlreadyOpenError
from .TransferStatusBar import TransferStatusBar


class BrowserGuiApp:
    def __init__(self):
        set_windows_app_user_model_id()
        self.root = TkinterDnD.Tk()
        self.root.report_callback_exception = self._show_callback_exception
        self.root.title("iDrive Remote Browser")
        apply_window_icon(self.root)
        self.root.geometry("980x640")
        self.root.minsize(760, 460)

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
        self._transfer_cancelled = False
        self._sync_window = None
        self._ui_queue: queue.Queue[tuple] = queue.Queue()

        self._poll_ui_queue()
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
        self.table.heading("name", text="Name")
        self.table.heading("size", text="Size")
        self.table.heading("modified", text="Modified")
        self.table.heading("id", text="ID")
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
        self.transfer_status = TransferStatusBar(bottom, abort_icon=self.icons["action_cancel"], abort_command=self.abort_transfer)
        self.status_var = self.transfer_status.status_var
        self.progress_var = self.transfer_status.progress_var

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
            for item in selected:
                item.move_to_trash()
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
        if self.client is None or self.current_folder is None or self._busy:
            return

        existing_paths = [path for path in paths if path.exists()]
        if not existing_paths:
            messagebox.showinfo("Upload", "No existing files or folders were selected.", parent=self.root)
            return

        target_folder = self.current_folder
        if len(existing_paths) == 1:
            status = f"Uploading {existing_paths[0].name}..."
        else:
            status = f"Uploading {len(existing_paths)} item(s)..."
        self._set_busy(True, status)

        def work():
            uploader = self.client.get_uploader()
            self._set_active_transfer("upload", uploader)
            try:
                for path in existing_paths:
                    uploader.upload(path, parent=target_folder)
                uploader.join()
                raise_transfer_errors(uploader, "Upload")
            finally:
                self._clear_active_transfer()

        self._run_password_retryable_worker(work, lambda _result: self._operation_done("Upload complete", refresh=True), [target_folder])

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

        self.transfer_status.set_abort_enabled(False)
        self.transfer_status.set_status("Aborting transfer...")
        self._transfer_cancelled = True
        threading.Thread(target=lambda: transfer.shutdown(cancel_pending=True), daemon=True).start()

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
        children.sort(key=lambda item: (not item.is_dir, item.name.lower()))
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

    def _operation_done(self, message: str, *, refresh: bool = False) -> None:
        if self._transfer_cancelled:
            message = "Transfer aborted"
            refresh = False
        self._reset_transfer_progress()
        self._set_busy(False, message)
        if refresh:
            self.refresh()

    def _operation_failed(self, exc: Exception) -> None:
        if self._transfer_cancelled:
            self._reset_transfer_progress()
            self._set_busy(False, "Transfer aborted")
            return
        self._reset_transfer_progress()
        self._set_busy(False, "Error")
        messagebox.showerror("Error", str(exc), parent=self.root)

    def _operation_failed_with_password_retry(self, exc: Exception, items: list[Item | None], retry) -> None:
        if not isinstance(exc, BackendMissingOrIncorrectResourcePasswordError):
            self._operation_failed(exc)
            return

        self._reset_transfer_progress()
        self._set_busy(False, "Password required")
        item = password_prompt_item(items, self.current_folder)
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

    def _clear_root(self) -> None:
        for child in self.root.winfo_children():
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
        traceback.print_exception(exc_type, exc, tb)
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
            "action_rename": self._create_action_icon("rename"),
            "action_trash": self._create_action_icon("trash"),
            "action_cancel": self._create_action_icon("cancel"),
            "action_logout": self._create_action_icon("logout"),
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
        elif kind == "logout":
            draw.rectangle((3, 4, 10, 14), outline=gray, width=2)
            draw.line((9, 9, 15, 9), fill=red, width=2)
            draw.polygon([(13, 6), (16, 9), (13, 12)], fill=red)
        return ImageTk.PhotoImage(image)

    def _set_active_transfer(self, direction: str, transfer) -> None:
        with self._active_transfer_lock:
            self._active_transfer = transfer
            self._transfer_cancelled = False
        self._ui_queue.put(("done", lambda _payload: self.transfer_status.attach_transfer(direction, transfer), None))

    def _clear_active_transfer(self) -> None:
        with self._active_transfer_lock:
            self._active_transfer = None

    def _reset_transfer_progress(self) -> None:
        self._transfer_cancelled = False
        if hasattr(self, "transfer_status"):
            self.transfer_status.reset()

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
