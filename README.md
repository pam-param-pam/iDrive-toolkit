# iDrive API Wrapper

Python client utilities for the iDrive backend. The package includes object wrappers for files/folders, high-throughput upload/download helpers, sync tooling, a Tk remote browser, and a WebSocket event client.

## Install

```powershell
pip install -e .
```

For local development:

```powershell
pip install -r dev-requirements.txt
```

## Basic Usage

```python
from src.iDriveApiWrapper.iDrive import Client

client = Client.login(
    "https://idrive.pamparampam.dev/api",
    "username",
    "password",
)

root = client.get_root()
file = client.get_file("file_id_here")
folder = client.get_folder("folder_id_here")

print(file.name)
print(folder.created_at)

file.rename("new_file_name.txt")
folder.move_to_trash()
folder.restore_from_trash()
```

## Remote Browser GUI

The remote browser provides login, browsing, upload/download, remote rename/trash actions, folder sync, cached auth, logout, and a selectable logs window.

```powershell
python remote-browser.py
```

The browser stores UI config and auth tokens through `IdriveStorage`, under the app's local data directory.

## WebSocket Events

Every `Client` owns a `WebsocketManager` at `client.websocket`. It connects to the authenticated `/user` WebSocket endpoint, sends `PONG` replies for server `PING` messages, reconnects after WebSocket errors, and dispatches parsed `WebsocketEvent` objects to registered callbacks.

```python
from src.iDriveApiWrapper.iDrive import Client

client = Client.login("https://idrive.pamparampam.dev/api", "username", "password")

def on_event(event):
    print(event.type, event)

client.websocket.register_callback(on_event)
client.websocket.start_websocket()

if client.websocket.wait_until_connected(timeout=5):
    client.websocket.send_json({"type": "hello"})

# Keep the process alive if this is your main worker.
client.websocket.run_forever()
```

Stop the listener when you no longer need it:

```python
client.websocket.stop_websocket()
```

If the backend sends a `FORCE_LOGOUT` event, the manager shuts the listener down gracefully.

## Upload and Download

Use the client factories so worker limits and account settings are loaded from the backend.

```python
from pathlib import Path

folder = client.get_folder("remote_folder_id")

uploader = client.get_uploader()
uploader.upload(Path("local_file_or_folder"), parent=folder)
uploader.join()

downloader = client.get_downloader()
downloader.download(client.get_file("remote_file_id"), target_dir=Path("downloads"))
downloader.join()
```

## Sync GUI

```python
from pathlib import Path

remote_root = client.get_folder("remote_folder_id")
syncer = client.get_syncer()
syncer.sync_gui(Path("local_folder"), remote_root)
```

## Notes

- Folder/file passwords can be passed to `get_file()` and `get_folder()` when needed.
- Video files expose `file.play()`, which uses `ffplay` and requires it to be available on `PATH`.
- WebSocket callbacks run in daemon threads; keep callback code thread-safe.
