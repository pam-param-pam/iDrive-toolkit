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
Saved accounts are identified by their root-folder ID and displayed using the account name returned by `user/me`. Selecting a different saved account opens another browser window, leaving the current window and its transfers bound to the current account.

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

Each client owns its configuration, HTTP connections, credentials, WebSocket connection, and transfer workers. Resources keep their originating session, so clients can run concurrently against different endpoints or accounts:

```python
with Client.login("https://first.example/api", "alice", "password-a") as first, \
     Client.login("https://second.example/api", "bob", "password-b") as second:
    first.get_downloader().set_download_folder("downloads/alice")
    second.get_downloader().set_download_folder("downloads/bob")

    alice_file = first.get_file("file-id-a")
    bob_file = second.get_file("file-id-b")
    alice_file.rename("alice-video.mp4")
    bob_file.rename("bob-video.mp4")
```

Cached credentials work with `Client(base_url, token)`. The WebSocket URL and remote folder-hash concurrency are selected automatically. Use a context manager or call `client.close()` to stop its workers and release connections.

Create resources through `client.get_file()` / `client.get_folder()`. Direct model construction now needs `session=client.session` for network operations; profile/settings fetches also require that keyword. Low-level requests use `client.session.make_request(...)`. Mixing resources from different clients in a bulk operation, move, or transfer raises `ValueError`.

Persistent `config`, `auth`, and `data` directories live directly under the application root; there is no `clients` directory. Local file CRCs are cached once in `data` and reused by every client session. Transfer files live under the shared temporary `idrive` directory: normal download fragments in `downloader/<file-id>`, and sync files in `syncer/files/<hash>` with sync download fragments in `syncer/downloader/<file-id>`. The uploader reads its source files directly. Recent transfer files survive restarts; transfer directories with no file activity for seven days are removed when storage or sync staging opens. Call `client.storage.clear_transfer_files()` while transfers are idle to remove all temporary transfer files. Completed downloads and original upload files are outside this cleanup. The CRC cache stores only derived file metadata and CRC values, never synchronization progress.

## Upload And Download

Use the client factory methods so account settings and worker limits are loaded from the backend.

```python
from pathlib import Path

folder = client.get_folder("remote_folder_id")

uploader = client.get_uploader()
uploader.upload(Path("local_file_or_folder"), parent=folder)
uploader.join()

downloader = client.get_downloader()
downloader.set_download_folder(Path("downloads"))
downloader.download(client.get_file("remote_file_id"))
downloader.join()
```

Network timeouts and rate limits have bounded automatic retries: downloads default
to 5 retries and uploads (including backend saves) to 20, after the initial attempt.
Set `max_retries` on `client.create_downloader(...)` or `client.create_uploader(...)`
to change the limit. Exhausted transient failures remain manually retryable:

```python
downloader.retry_failed_files()  # all transient failures; or pass a list of file IDs
downloader.join()
# uploader exposes the same retry_failed_files() and get_retryable_states() APIs
```

Retry after `join()` returns and before shutting down the engine. Downloads reuse
verified fragments and retain their destination and completion callback. Failed
upload transfers restart the individual file; failed backend saves reuse the
uploaded metadata. Permanent failures are excluded from these retry actions.
In both GUI File States windows, use **Retry Selected** or **Retry All Failed**.
`join()` waits for submitted attempts, including scheduled automatic retries, and
returns with failures inspectable via `get_failed_states()`. It leaves workers,
monitoring, and autoscaling running; `shutdown()` owns their final teardown.
The former `wait_for_retry` argument has been removed. GUI transfers finish with
partial results rather than waiting indefinitely for manual recovery.

Sync uses a continuous pipeline across folders, with one staging budget shared by
all transfer directions. A failed file stays in File States while other files
continue. Skipped and already-matching files appear only in the sync report.
After sync finishes, File States can retry a failed file through the complete
download/upload/verification pipeline, even though the initial engines have
closed. Removing a failed sync state also discards its retained staging data.
See [the pipeline architecture](docs/sync-pipeline.md) for lifecycle and storage rules.

For servers that return structured `ultraDownload/items/{id}` responses with
immediate `files` and `folders`, opt in to recursive folder downloads:

```python
downloader = client.create_downloader(recursive_folder_listing=True)
downloader.download(client.get_folder("remote_folder_id"))
downloader.join()
```

The default uses the legacy list response. With the flag enabled, the downloader
requests each subfolder separately and downloads its files under the selected
folder's relative paths. The structured response's aggregate `size` and
`file_count` describe the entire folder; its `files` list contains only immediate
files. Empty folders do not produce a file to download.

Video metadata, thumbnails, subtitle extraction, and playback use FFmpeg tools. Make sure `ffmpeg`, `ffprobe`, and `ffplay` are available on `PATH` for the features that need them.

Attach a local subtitle to an already uploaded video:

```python
video = client.get_file("remote_video_id")
subtitle = video.upload_subtitle("captions.vtt", language="English", is_forced=False)
```

This synchronous call uploads the subtitle directly to Discord and registers it with the video. It uses the video's encryption method and returns a `Subtitle`. The subtitle must be nonempty and fit in one Discord attachment. Its contents are uploaded as supplied, without format conversion. `create_subtitles` is an alias for `upload_subtitle`.

## Sync GUI

```python
from pathlib import Path
from idrive_toolkit.gui.SyncGui import SyncGui

remote_root = client.get_folder("remote_folder_id")
syncer = client.get_syncer()
SyncGui(syncer, Path("local_folder"), remote_root).run()
```

The same `SyncGui` supports local/local, remote/remote, and local/remote backends. Mixed pairs use Local/Remote labels; pairs of the same kind use Source/Destination labels. Optional `left_name` and `right_name` arguments identify the folders or accounts in the headings.

The sync UI can mirror either side, copy entries missing on the other side, view both copies of changed files, resolve changed files, bulk-rename conflict entries automatically, and handle detected renames. The `rename` changed-file strategy keeps both versions by giving the left copy a conflict-safe name before syncing both files.

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

## Synchronize local or remote storage

The syncer provides a common writable backend interface. Use two remote backends
for instance-to-instance copying, or replace either side with `LocalBackend()`.

```python
from idrive_toolkit.syncer.Syncer import Syncer
from idrive_toolkit.syncer.backends import RemoteBackend

syncer = Syncer.from_backends(
    RemoteBackend(first),
    RemoteBackend(second),
)
report = syncer.sync(first.get_root(), second.get_root(),
                     staging_dir="migration-staging", max_staging_gb=100)
```

It compares folder hashes, processes differing folders breadth-first in a stable
order, and reuses the existing transfer engines. Rerunning compares the remote
contents again. Matching remote files still receive a subtitle audit. Changed
files raise conflicts by default; replacement and deletion of destination-only
items require explicit `changed="replace"` and `delete_extras=True` options.
See [the design](docs/instance-copy-design.md) for scope and recovery limitations.
