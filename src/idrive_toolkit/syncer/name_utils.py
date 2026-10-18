from pathlib import PurePath


MAX_RESOURCE_NAME_LENGTH = 75


def remote_resource_name(file_name: str) -> str:
    if len(file_name) <= MAX_RESOURCE_NAME_LENGTH:
        return file_name

    last_dot_index = file_name.rfind(".")

    if last_dot_index > 0:
        file_extension = file_name[last_dot_index:]
        file_name_without_extension = file_name[:last_dot_index]
    else:
        file_extension = ""
        file_name_without_extension = file_name

    max_base_length = MAX_RESOURCE_NAME_LENGTH - len(file_extension)

    if max_base_length <= 0:
        return file_name[:MAX_RESOURCE_NAME_LENGTH]

    return file_name_without_extension[:max_base_length] + file_extension


def conflict_name(file_name: str, label: str, existing_names: set[str]) -> str:
    """Return a remote-safe, collision-free name carrying an origin label."""
    path = PurePath(file_name)
    suffix = path.suffix
    stem = file_name[:-len(suffix)] if suffix else file_name
    occupied = {name.casefold() for name in existing_names}

    index = 1
    while True:
        marker = f" ({label})" if index == 1 else f" ({label} {index})"
        max_suffix_length = max(0, MAX_RESOURCE_NAME_LENGTH - len(marker) - 1)
        if max_suffix_length == 0:
            safe_suffix = ""
        elif len(suffix) > max_suffix_length:
            safe_suffix = suffix[-max_suffix_length:]
        else:
            safe_suffix = suffix
        max_stem_length = MAX_RESOURCE_NAME_LENGTH - len(marker) - len(safe_suffix)
        candidate = stem[:max_stem_length] + marker + safe_suffix

        if candidate.casefold() not in occupied:
            return candidate
        index += 1


def local_conflict_name(file_name: str, existing_names: set[str]) -> str:
    return conflict_name(file_name, "local", existing_names)
