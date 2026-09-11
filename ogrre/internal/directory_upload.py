"""Metadata validation for browser uploads. File bytes never enter the API."""

import os
import re
import time
from pathlib import PurePosixPath

from google.cloud.documentai_toolbox import constants

MIME_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
STAGING_PREFIX = "directory_uploads"
SESSION_SECONDS = 7 * 24 * 60 * 60


def limits():
    return {
        "max_files": 1000,
        "max_file_bytes": constants.BATCH_MAX_FILE_SIZE,
        "max_total_bytes": int(
            os.getenv("DIRECTORY_UPLOAD_MAX_BYTES", str(20 * 1024**3))
        ),
    }


def validate_manifest(request):
    if not isinstance(request, dict):
        raise ValueError("Request body must be a JSON object")
    session_id = request.get("session_id", "")
    if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise ValueError("session_id must be a UUID without hyphens")
    files = request.get("files")
    constraints = limits()
    if not isinstance(files, list) or not 1 <= len(files) <= constraints["max_files"]:
        raise ValueError("Select between 1 and 1000 files")
    options = {}
    for key, default in (
        ("prevent_duplicates", True),
        ("run_cleaning_functions", True),
    ):
        value = request.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        options[key] = value
    manifest = []
    paths = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise ValueError("Each file must contain a name, relative_path, and size")
        name = item.get("name")
        path = item.get("relative_path", name)
        size = item.get("size")
        if (
            not isinstance(name, str)
            or not name
            or len(name.encode("utf-8")) > 255
            or not isinstance(path, str)
            or len(path.encode("utf-8")) > 800
            or path.startswith("/")
            or "\\" in path
            or any(part in ("", ".", "..") for part in path.split("/"))
            or PurePosixPath(path).name != name
            or any(ord(char) < 32 for char in path)
        ):
            raise ValueError("Files must have valid names and relative paths")
        if any(char in name for char in "#?%"):
            raise ValueError(
                f"Rename {name}: document filenames cannot contain #, ?, or %."
            )
        if path in paths:
            raise ValueError(f"Repeated file path: {path}")
        paths.add(path)
        content_type = MIME_TYPES.get(PurePosixPath(name).suffix.lower())
        if not content_type:
            raise ValueError(f"Unsupported file type: {name}")
        if type(size) is not int or not 0 < size <= constraints["max_file_bytes"]:
            raise ValueError(f"Invalid or oversized file: {name}")
        file_id = str(index)
        manifest.append(
            {
                "file_id": file_id,
                "name": name,
                "relative_path": path,
                "size": size,
                "content_type": content_type,
                "object_name": f"{STAGING_PREFIX}/{session_id}/{file_id}/{name}",
            }
        )
    if sum(item["size"] for item in manifest) > constraints["max_total_bytes"]:
        raise ValueError("The selected directory exceeds the upload size limit")
    return session_id, manifest, options


def require_open_session(session):
    if session["expires_at"] <= time.time():
        raise ValueError("This upload session has expired. Select the directory again.")
    if session.get("status") != "uploading":
        raise ValueError("This directory has already been submitted for processing")
