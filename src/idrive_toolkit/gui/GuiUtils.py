from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog
from typing import Iterable, Callable

from ..exceptions import BackendMissingOrIncorrectResourcePasswordError
from ..models.Item import Item


APP_USER_MODEL_ID = "idrive_toolkit.RemoteBrowser"
APP_ICON_ICO_PATH = Path(__file__).with_name("app_icon.ico")
APP_ICON_PNG_PATH = Path(__file__).with_name("app_icon.png")

FILE_ICON_EXTENSIONS: tuple[tuple[str, set[str]], ...] = (
    ("file_image", {"jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "heic", "raw"}),
    ("file_video", {"mp4", "mkv", "mov", "avi", "webm", "wmv", "m4v"}),
    ("file_audio", {"mp3", "wav", "flac", "aac", "ogg", "m4a", "opus"}),
    ("file_text", {"txt", "md", "rtf", "doc", "docx", "odt"}),
    ("file_archive", {"zip", "rar", "7z", "tar", "gz", "bz2", "xz"}),
    ("file_code", {"py", "js", "ts", "html", "css", "json", "xml", "yaml", "yml", "java", "c", "cpp", "h", "cs", "go", "rs", "php"}),
    ("file_pdf", {"pdf"}),
)


def file_icon_key(name: str) -> str:
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    for icon_key, extensions in FILE_ICON_EXTENSIONS:
        if extension in extensions:
            return icon_key
    return "file"


def apply_window_icon(window: tk.Misc) -> None:
    if APP_ICON_ICO_PATH.exists():
        try:
            window.iconbitmap(default=str(APP_ICON_ICO_PATH))
        except tk.TclError:
            pass

    if not APP_ICON_PNG_PATH.exists():
        return

    try:
        icon = tk.PhotoImage(file=str(APP_ICON_PNG_PATH))
        window.iconphoto(True, icon)
    except tk.TclError:
        return
    setattr(window, "_app_icon_photo", icon)


def set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        return


def safe_item_label(item: Item) -> str:
    return str(getattr(item, "_name", None) or getattr(item, "_id", None) or item.id)


def needs_resource_password(item: Item) -> bool:
    return item.is_locked and not item.password


def password_prompt_item(
    error: BackendMissingOrIncorrectResourcePasswordError,
    items: Iterable[Item | None] = (),
) -> Item | None:
    required = _required_password_folder(error)
    if required is None:
        return None

    required_id, required_name = required
    for item in items:
        if item is not None and str(getattr(item, "_id", item.id)) == required_id:
            item._name = getattr(item, "_name", None) or required_name
            item._is_locked = True
            item._lock_from = getattr(item, "_lock_from", None) or required_id
            return item

    from ..models.Folder import Folder

    item = Folder(required_id)
    item._name = required_name
    item._is_locked = True
    item._lock_from = required_id
    return item


def _required_password_folder(error: BackendMissingOrIncorrectResourcePasswordError) -> tuple[str, str] | None:
    data = error.json()
    if not isinstance(data, dict):
        return None

    required = data.get("requiredFolderPasswords")
    if not isinstance(required, list) or not required:
        return None

    folder = required[0]
    if not isinstance(folder, dict) or not folder.get("id"):
        return None

    folder_id = str(folder["id"])
    return folder_id, str(folder.get("name") or folder_id)


def prompt_resource_password(parent: tk.Misc, item: Item, remember: Callable[[Item, str], None]) -> bool:
    password = simpledialog.askstring("Folder Password", f"Password for {safe_item_label(item)}", show="*", parent=parent)
    if password is None:
        return False

    try:
        item.check_password(password)
    except BackendMissingOrIncorrectResourcePasswordError:
        messagebox.showerror("Folder Password", "Missing or incorrect password.", parent=parent)
        return False
    except Exception as exc:
        messagebox.showerror("Folder Password", str(exc), parent=parent)
        return False

    remember(item, password)
    return True
