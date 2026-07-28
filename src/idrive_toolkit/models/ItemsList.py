from typing import List, Union, Callable, Optional
from datetime import datetime

from .File import File
from .Folder import Folder


class ItemsList:
    def __init__(self, items: List[Union[File, Folder]]):
        self._items = items

    def filter_by_size(self, min_size: Optional[int] = None, max_size: Optional[int] = None) -> 'ItemsList':
        filtered = self._items
        if min_size is not None:
            filtered = [item for item in filtered if getattr(item, 'size', 0) >= min_size]
        if max_size is not None:
            filtered = [item for item in filtered if getattr(item, 'size', float('inf')) <= max_size]
        return ItemsList(filtered)

    def filter_by_date(self, min_date: Optional[datetime] = None, max_date: Optional[datetime] = None) -> 'ItemsList':
        filtered = self._items
        if min_date is not None:
            filtered = [item for item in filtered if getattr(item, 'date', datetime.min) >= min_date]
        if max_date is not None:
            filtered = [item for item in filtered if getattr(item, 'date', datetime.max) <= max_date]
        return ItemsList(filtered)

    def filter_by_files(self) -> 'ItemsList':
        filtered = [item for item in self._items if not getattr(item, 'is_dir', True)]
        return ItemsList(filtered)

    def filter_by_folders(self) -> 'ItemsList':
        filtered = [item for item in self._items if getattr(item, 'is_dir', True)]
        return ItemsList(filtered)

    def filter(self, condition: Callable[[Union[File, Folder]], bool]) -> 'ItemsList':
        """General-purpose filter using a custom condition function."""
        filtered = [item for item in self._items if condition(item)]
        return ItemsList(filtered)

    def sort(self, attribute: str, order: str = "asc") -> 'ItemsList':
        """Sort the items by a given attribute in ascending or descending order."""
        reverse = order.lower() == "desc"
        sorted_items = sorted(self._items, key=lambda item: getattr(item, attribute, None), reverse=reverse)
        return ItemsList(sorted_items)

    def search(self, query: str) -> 'ItemsList':
        searched = []
        for item in self._items:
            if query.lower() in item.name.lower():
                searched.append(item)
        return ItemsList(searched)

    def first(self) -> Optional[Union['File', 'Folder']]:
        if not self._items:
            raise IndexError("No items available")
        return self._items[0]

    def get_as_list(self) -> list:
        return list(self._items)

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __getitem__(self, index) -> Union[File, Folder]:
        return self._items[index]

    def __contains__(self, item) -> bool:
        return item in self._items

    def __bool__(self):
        return bool(self._items)

    def __repr__(self):
        return f"{self._items}"
