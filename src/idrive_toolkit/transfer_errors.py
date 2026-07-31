from __future__ import annotations


class TransferFailedError(RuntimeError):
    pass


def raise_transfer_errors(transfer, label: str) -> None:
    errors = _collect_errors(transfer)
    if not errors:
        return

    lines = [f"{label} failed with {len(errors)} error(s):"]
    for index, (name, error) in enumerate(errors[:10], start=1):
        lines.append(f"{index}. {name}: {_format_error_reason(error)}")
    if len(errors) > 10:
        lines.append(f"... and {len(errors) - 10} more")
    raise TransferFailedError("\n".join(lines))


def _collect_errors(transfer) -> list[tuple[str, BaseException]]:
    errors: list[tuple[str, BaseException]] = []

    ctx = getattr(transfer, "ctx", None)
    if ctx is not None:
        for index, error in enumerate(getattr(ctx, "errors", []), start=1):
            errors.append((f"transfer error {index}", error))

    get_failed_states = getattr(transfer, "get_failed_states", None)
    if get_failed_states is None:
        return errors

    for state_id, state in get_failed_states().items():
        error = getattr(state, "error", None)
        if error is None:
            continue
        errors.append((_state_label(state_id, state), error))

    return errors


def _format_error_reason(error: BaseException) -> str:
    parts = []
    message = getattr(error, "message", None) or str(error) or repr(error)
    parts.append(f"{error.__class__.__name__}: {message}")

    status = getattr(error, "status", None)
    if status is not None and f"HTTP {status}" not in message:
        parts.append(f"HTTP {status}")

    method = getattr(error, "method", None)
    url = getattr(error, "url", None)
    if method or url:
        target = " ".join(str(part) for part in (method, url) if part)
        if target and target not in message:
            parts.append(target)

    text = getattr(error, "text", None)
    if text and text not in message:
        parts.append(str(text))

    cause = getattr(error, "cause", None) or getattr(error, "__cause__", None)
    if cause is not None:
        cause_message = str(cause)
        if cause_message and cause_message not in message:
            parts.append(f"caused by {cause.__class__.__name__}: {cause_message}")

    return " | ".join(parts)


def _state_label(state_id: str, state) -> str:
    artifacts = getattr(state, "artifacts", None)
    if artifacts is not None and getattr(artifacts, "name", None):
        return str(artifacts.name)

    file_id = getattr(state, "file_id", None)
    if file_id:
        return str(file_id)

    return str(state_id)
