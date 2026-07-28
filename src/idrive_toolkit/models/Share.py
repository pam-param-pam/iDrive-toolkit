# import logging
#
# from models.Folder import Folder
# from models.baseResource import Resource
# from utils.decorators import overrides
#
# logger = logging.getLogger("iDrive")
# import logging
#
# from typing import Union, Optional, TYPE_CHECKING
#
# from models.Item import Item
#
# from models.File import File
#
# logger = logging.getLogger("iDrive")
#
# if TYPE_CHECKING:
#     from models.ItemsList import ItemsList
#
#
# class Folder(Item):
#     def __init__(self, folder_id):
#         super().__init__(folder_id)
#         self._children: Optional[ItemsList] = None
#
#         self._name: Optional[str] = None
#         self._parent_id: Optional[str] = None
#         self._created: Optional[str] = None
#         self._last_modified: Optional[str] = None
#         self._parent: Optional[Folder] = None
#         self._id: str = folder_id
#         self.isDir = True
#
#     def __str__(self):
#         return f"ShareFolder({self.name})"
#
#     def _parse_children(self, parent: Union['Folder', None], data: dict):
#         from models.ItemsList import ItemsList
#
#         children = []
#         for element in data:
#             if element['isDir']:
#                 item = Folder(element['id'])
#             else:
#                 item = File(element['id'])
#
#             item._set_data(element)
#
#             if parent and parent._password:
#                 item.set_password(parent._password)
#             children.append(item)
#
#         return ItemsList(children)
#
#     def _set_data(self, json_data: dict):
#         for key, value in json_data.items():
#             if key == "isDir":
#                 self.isDir = value
#             elif key == "id":
#                 self._id = value
#             elif key == "name":
#                 self._name = value
#             elif key == "parent_id":
#                 self._parent_id = key
#             elif key == "created":
#                 self._created = value
#             elif key == "last_modified":
#                 self._last_modified = value
#             elif key == "children":
#                 self._children = self._parse_children(self, value)
#             else:
#                 logger.warning(f"[FOLDER] Unexpected renderer: {key}")
#
#
# class ShareFolder(Folder):
#     def __init__(self, folder_id):
#         super().__init__(folder_id)
#         self._fetched = True
#
#     @overrides
#     def children(self):
#         pass
import logging
#
from typing import Optional

from overrides import overrides

from .namedTuples import VisitsNamedTuple
from ..models.Resource import Resource
from ..utils.networker import make_request

logger = logging.getLogger("iDrive")


"""Stinking pile of hot garbage"""
class Share(Resource):
    def __init__(self, token):
        super().__init__(token)
        self.token: str = token
        self._expire: Optional[str] = None
        self._id: Optional[str] = None
        self._name: Optional[str] = None
        self._resource_id: Optional[str] = None
        self._is_dir: Optional[bool] = None

    def _set_data(self, json_data: dict):
        for key, value in json_data.items():
            if key == "expire":
                self._expire = value
            elif key == "name":
                self._name = value
            elif key == "isDir":
                self._is_dir = value
            elif key == "token":
                self.token = value
            elif key == "resource_id":
                self._resource_id = value
            elif key == "id":
                self._id = value
            else:
                logger.warning(f"[Share] Unexpected key: {key}")
    #
    # @overrides
    # def _fetch_data(self):
    #     pass



    def get_visits(self) -> list[VisitsNamedTuple]:
        data = make_request("GET", f"shares/{self.token}/visits")
        visit_tuples = []
        for visit in data["accesses"]:
            visit_tuples.append(VisitsNamedTuple(**visit))
        return visit_tuples

    def delete(self):
        pass

    def get_item_inside(self):
        pass

    def __str__(self):
        return f"Share({self.name})"

