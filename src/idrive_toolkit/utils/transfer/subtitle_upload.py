
from pathlib import Path

import httpx

from ...models.DiscordSettings import DiscordSettings
from ...models.Subtitle import Subtitle
from ...uploader.Encryptor import Encryptor
from ...uploader.models import Crypto


def upload_subtitle(file, path: str | Path, language: str, is_forced: bool = False) -> Subtitle:
    path = Path(path)
    with path.open("rb") as source:
        raw = source.read()

    settings = DiscordSettings.fetch(session=file.session)
    webhook = next((hook for hook in settings.webhooks if not hook.is_blocked), None)
    if webhook is None:
        raise ValueError("No available Discord webhook for subtitle upload")

    crypto = Crypto.generate(file.encryption_method)
    encryptor = Encryptor(crypto.method, crypto.key, crypto.iv)
    data = encryptor.encrypt(raw) + encryptor.finalize()

    url = httpx.URL(webhook.url).copy_set_param("wait", "true")
    with httpx.Client(timeout=60.0) as client:
        response = client.post(url, files={
            "files[0]": (settings.attachment_name or path.name, data, "application/octet-stream"),
        })
        response.raise_for_status()
        message = response.json()

    attachment = message["attachments"][0]
    payload = {
        "channel_id": str(message["channel_id"]),
        "message_id": str(message["id"]),
        "attachment_id": str(attachment["id"]),
        "message_author_id": str(message["author"]["id"]),
        "language": language,
        "is_forced": is_forced,
        "size": len(data),
    }
    if crypto.key is not None:
        payload.update(key=crypto.key_b64(), iv=crypto.iv_b64())
    result = file.session.make_request(
        "POST", f"files/{file.id}/subtitles", data=payload,
        headers=file._get_password_header(),
    )
    subtitle = Subtitle(session=file.session, **result)
    subtitle.set_password(file.password)
    return subtitle
