import os
import urllib
from typing import List
from urllib.parse import unquote

import requests

from ..Config import APIConfig
from ..models.Folder import Folder
from ..models.Item import Item
from .networker import make_request

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


def _extract_ids_and_passwords(items: List[Item]) -> dict:
    ids = []
    resourcePasswords = {}
    for item in items:
        if item.is_locked and item.password:
            resourcePasswords[item.lock_from] = item.password
        ids.append(item.id)

    return {'ids': ids, 'resourcePasswords': resourcePasswords}


def move_to_trash(items: List[Item]) -> None:
    data = _extract_ids_and_passwords(items)
    make_request("PATCH", f"items/moveToTrash", data)


def delete(items: List[Item]) -> None:
    data = _extract_ids_and_passwords(items)
    make_request("POST", f"items/delete", data)


def restore_from_trash(items: List[Item]) -> None:
    data = _extract_ids_and_passwords(items)
    make_request("PATCH", f"items/restoreFromTrash", data)


def move(items: List[Item], new_parent: Folder) -> None:
    data = _extract_ids_and_passwords(items)
    data['new_parent_id'] = new_parent.id
    make_request("PATCH", f"items/move", data)


def get_zip_download_url(items: List[Item]) -> str:
    data = _extract_ids_and_passwords(items)
    data = make_request("POST", f"zip", data)
    return data['download_url']


def parse_filename(content_disposition):
    if 'filename*=' in content_disposition:
        # Extract the UTF-8 filename
        filename_encoded = content_disposition.split("filename*=")[1].split(';')[0].strip()
        encoding, _, filename_encoded = filename_encoded.split("'", 2)
        filename = urllib.parse.unquote(filename_encoded)
    elif 'filename=' in content_disposition:
        # Fallback to plain filename
        filename = content_disposition.split("filename=")[1].split(';')[0].strip().strip('"')
    else:
        # Default filename if none provided
        filename = "default_filename"
    return filename


def download_from_url(download_url: str, path: str = None) -> str:
    # Perform the download
    response = requests.get(download_url, stream=True)
    response.raise_for_status()

    # Extract filename from headers, or fall back to a default name
    content_disposition = response.headers.get('Content-Disposition')
    filename = parse_filename(content_disposition)

    if path is None:
        # Default: construct from APIConfig
        os.makedirs(APIConfig.download_folder, exist_ok=True)
        path = os.path.join(APIConfig.download_folder, filename)
    elif os.path.isdir(path):
        # If path is a directory, join with filename
        path = os.path.join(path, filename)
    else:
        # Path is assumed to be a full file path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    # Get the total file size from the Content-Length header (if available)
    total_size = int(response.headers.get('content-length', 0))

    # Open the file in binary write mode
    with open(path, 'wb') as file:
        if tqdm is None:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)
        else:
            with tqdm(total=total_size, unit='B', unit_scale=True, desc=filename) as progress_bar:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)
                    progress_bar.update(len(chunk))

    return path
