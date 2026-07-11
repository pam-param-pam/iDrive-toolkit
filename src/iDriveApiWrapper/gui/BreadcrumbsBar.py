from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable, Iterable, Protocol


class BreadcrumbLike(Protocol):
    id: str
    name: str


class BreadcrumbsBar(ttk.Frame):
    def __init__(self, parent: tk.Misc, command: Callable[[str], None]):
        super().__init__(parent)
        self.command = command
        self._items: list[tuple[str, str]] = []

    def set_items(self, breadcrumbs: Iterable[BreadcrumbLike | tuple[str, str]]) -> None:
        self._items = [self._normalize(item) for item in breadcrumbs]
        self._render()

    def clear(self) -> None:
        self._items = []
        self._render()

    def _render(self) -> None:
        for child in self.winfo_children():
            child.destroy()

        for index, (folder_id, name) in enumerate(self._items):
            if index:
                ttk.Label(self, text="/").pack(side="left", padx=4)

            state = "disabled" if index == len(self._items) - 1 else "normal"
            ttk.Button(
                self,
                text=name or folder_id,
                command=lambda folder_id=folder_id: self.command(folder_id),
                state=state,
            ).pack(side="left")

    @staticmethod
    def _normalize(item: BreadcrumbLike | tuple[str, str]) -> tuple[str, str]:
        if isinstance(item, tuple):
            return str(item[0]), str(item[1])
        return str(item.id), str(item.name)
