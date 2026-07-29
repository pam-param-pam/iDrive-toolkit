# iDrive Toolkit

Python tools for the iDrive project.

The package can be used as a Python client library, a transfer/sync toolkit, or as a desktop remote browser GUI.

## Download GUI

Prebuilt GUI binaries are attached to the [GitHub Releases page](https://github.com/pam-param-pam/iDrive-toolkit/releases):

- Windows: `idrive-remote-browser.exe`
- Linux: `idrive-remote-browser-linux.tar.gz`

## Install

Core API bindings only:

```powershell
pip install idrive_toolkit
```

Optional transfer support:

```powershell
pip install idrive_toolkit[transfer]
```

Optional GUI support:

```powershell
pip install idrive_toolkit[gui]
```

Everything:

```powershell
pip install idrive_toolkit[all]
```

For local development:

```powershell
pip install -r dev-requirements.txt
```

## GUI

After installing the GUI extra, start the browser with:

```powershell
idrive-gui
```

The browser supports:

- login and cached auth
- remote folder browsing
- upload and download
- drag and drop upload from the file explorer
- remote rename and move to trash
- folder sync
- update prompts
- an in-app log window for stdout, stderr, and package logs

Config and auth tokens are stored through `IdriveStorage` in the app's local data directory.

## API Usage

```python
from idrive_toolkit.iDrive import Client

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

## Upload And Download

Use the client factory methods so account settings and worker limits are loaded from the backend.

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

Video metadata, thumbnails, subtitle extraction, and playback use FFmpeg tools. Make sure `ffmpeg`, `ffprobe`, and `ffplay` are available on `PATH` for the features that need them.

## Sync GUI

```python
from pathlib import Path

remote_root = client.get_folder("remote_folder_id")
syncer = client.get_syncer()
syncer.sync_gui(Path("local_folder"), remote_root)
```

The sync UI can compare local and remote folders, sync all entries, sync selected entries, resolve changed files, and handle detected renames.

## WebSocket Events

Every `Client` has a `WebsocketManager` at `client.websocket`.

```python
from idrive_toolkit.iDrive import Client

client = Client.login("https://idrive.pamparampam.dev/api", "username", "password")

def on_event(event):
    print(event.type, event)

client.websocket.register_callback(on_event)
client.websocket.start_websocket()

if client.websocket.wait_until_connected(timeout=5):
    client.websocket.send_json({"type": "hello"})

client.websocket.run_forever()
```

Stop the listener when done:

```python
client.websocket.stop_websocket()
```

If the backend sends a `FORCE_LOGOUT` event, the WebSocket listener shuts down.

## Build

Build the Python package locally:

```powershell
python -m build
```

If `python -m build` is shadowed by a local `build/` directory, run it from outside the repository and pass the project path:

```powershell
python -m build D:\Projects\iDriveApiWrapper
```

## Notes

- Folder and file passwords can be passed to `get_file()` and `get_folder()` when needed.
- `file.play()` uses `ffplay`.
- WebSocket callbacks run in daemon threads; callback code should be thread-safe.
