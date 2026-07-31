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