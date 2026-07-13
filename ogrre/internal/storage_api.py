"""
storage_api.py

Handles the file storage for uploaded documents through OGRRE.
Supports configurations for google cloud storage and local file storage.

Also includes utilities for rotating images stored in either backend.

Storage keys are relative paths inside the configured backend. For Google
Cloud Storage, keys are blob names in STORAGE_BUCKET_NAME. For local storage,
keys are paths under LOCAL_STORAGE_ROOT.

Record display images use this canonical layout:
    uploads/{record-group-id}/{record-id}/{image-files}

When a record is deleted, its whole record image directory is moved to:
    deleted/{record-id}/{image-files}

Keep record-image path changes centralized in this module so callers do not
need to know the storage backend or key layout.
"""

import datetime
import io
import logging
import os
import re
import sys
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote, urlparse, unquote

import aiofiles
import aiohttp
from gcloud.aio.storage import Storage
from google.api_core.exceptions import NotFound
from google.cloud import storage

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

_log = logging.getLogger(__name__)

DIRNAME, _ = os.path.split(os.path.abspath(sys.argv[0]))
STORAGE_SERVICE_KEY = os.getenv("STORAGE_SERVICE_KEY")
BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME")
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "google").lower()
LOCAL_STORAGE_ROOT = os.path.expanduser(
    os.getenv("LOCAL_STORAGE_ROOT", "~/.ogrre/uploads")
)
LOCAL_STORAGE_URL_BASE = os.getenv(
    "LOCAL_STORAGE_URL_BASE", "http://localhost:8001/local-storage"
).rstrip("/")

UPLOADS_PREFIX = "uploads"
DELETED_PREFIX = "deleted"


def _storage_path(key):
    return os.path.join(LOCAL_STORAGE_ROOT, key)


def _get_storage_client(storage_service_key=None):
    service_key = storage_service_key or STORAGE_SERVICE_KEY
    return storage.Client.from_service_account_json(f"{DIRNAME}/{service_key}")


def _get_bucket(bucket_name=None, storage_service_key=None):
    client = _get_storage_client(storage_service_key=storage_service_key)
    bucket = client.bucket(bucket_name or BUCKET_NAME)
    return client, bucket


def _ensure_local_dir(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _is_local():
    return STORAGE_BACKEND == "local"


async def upload_file(file_path, file_name, folder="uploads", on_bytes_read=None):
    key = f"{folder}/{file_name}" if folder else file_name
    if _is_local():
        destination = _storage_path(key)
        _ensure_local_dir(destination)
        file_bytes = b""
        async with aiofiles.open(file_path, "rb") as src:
            async with aiofiles.open(destination, "wb") as dst:
                while True:
                    chunk = await src.read(1024 * 1024)
                    if not chunk:
                        break
                    await dst.write(chunk)
                    file_bytes += chunk
        if on_bytes_read:
            on_bytes_read(file_bytes)
        del file_bytes
        _log.info(f"uploaded document to local storage: {destination}")
        return destination

    async with aiofiles.open(file_path, "rb") as afp:
        file_bytes = await afp.read()
    if on_bytes_read:
        on_bytes_read(file_bytes)
    url = await _async_upload_to_bucket(file_name, file_bytes, folder=folder)
    del file_bytes
    _log.info(f"uploaded document to cloud storage: {url}")
    return url


async def upload_files(
    file_paths, file_names, folder="uploads", on_all_bytes_read=None
):
    """
    Uploads multiple files and optionally calls on_all_bytes_read with a list
    of all files' bytes once every file has been read, before uploading.
    """
    all_bytes = []
    for file_path in file_paths:
        async with aiofiles.open(file_path, "rb") as afp:
            all_bytes.append(await afp.read())

    if on_all_bytes_read:
        on_all_bytes_read(all_bytes)

    urls = []
    for file_name, file_bytes in zip(file_names, all_bytes):
        if _is_local():
            key = f"{folder}/{file_name}" if folder else file_name
            destination = _storage_path(key)
            _ensure_local_dir(destination)
            async with aiofiles.open(destination, "wb") as dst:
                await dst.write(file_bytes)
            _log.info(f"uploaded document to local storage: {destination}")
            urls.append(destination)
        else:
            url = await _async_upload_to_bucket(file_name, file_bytes, folder=folder)
            _log.info(f"uploaded document to cloud storage: {url}")
            urls.append(url)

    del all_bytes
    return urls


async def _async_upload_to_bucket(
    blob_name,
    file_obj,
    folder,
    bucket_name=BUCKET_NAME,
    service_file=None,
):
    async with aiohttp.ClientSession() as session:
        storage_client = Storage(
            service_file=service_file
            or (
                f"{DIRNAME}/{STORAGE_SERVICE_KEY}"
                if STORAGE_SERVICE_KEY
                else f"{DIRNAME}/creds.json"
            ),
            session=session,
        )
        status = await storage_client.upload(
            bucket_name, f"{folder}/{blob_name}", file_obj
        )
        return status["selfLink"]


def delete_directory(prefix, bucket_name=BUCKET_NAME):
    if _is_local():
        target = _storage_path(prefix)
        if os.path.isdir(target):
            for root, _, files in os.walk(target):
                for name in files:
                    file_path = os.path.join(root, name)
                    try:
                        os.remove(file_path)
                    except OSError as e:
                        _log.info(f"unable to delete local file {file_path}: {e}")
            return True
        return False

    _log.info(f"deleting storage prefix {prefix} from google storage")
    _, bucket = _get_bucket(bucket_name=bucket_name)
    blobs = bucket.list_blobs(prefix=prefix)
    for blob in blobs:
        _log.info(f"deleting {blob}")
        blob.delete()
    return True


def move_directory(prefix, destination_prefix, bucket_name=BUCKET_NAME):
    """
    Move all files under one storage key prefix to another prefix.

    GCS does not have real directories, so this performs copy-then-delete for
    every blob under the source prefix. Local storage performs the equivalent
    filesystem move while preserving paths below the source directory.
    """
    source_prefix = prefix.strip("/")
    target_prefix = destination_prefix.strip("/")

    if not source_prefix or not target_prefix:
        raise ValueError("source and destination storage prefixes are required")

    if _is_local():
        source = _storage_path(source_prefix)
        destination = _storage_path(target_prefix)
        if not os.path.exists(source):
            _log.info(f"local storage prefix not found: {source}")
            return False

        if os.path.isfile(source):
            files_to_move = [(source, os.path.basename(source))]
            cleanup_root = os.path.dirname(source)
        else:
            files_to_move = []
            cleanup_root = source
            for root, _, files in os.walk(source):
                for name in files:
                    source_file = os.path.join(root, name)
                    relative_path = os.path.relpath(source_file, source)
                    files_to_move.append((source_file, relative_path))

        moved_files = 0
        for source_file, relative_path in files_to_move:
            destination_file = os.path.join(destination, relative_path)
            _ensure_local_dir(destination_file)
            os.replace(source_file, destination_file)
            moved_files += 1

        for root, _, _ in os.walk(cleanup_root, topdown=False):
            try:
                os.rmdir(root)
            except OSError:
                pass

        return moved_files > 0

    _log.info(
        f"moving storage prefix {source_prefix} to {target_prefix} in google storage"
    )
    _, bucket = _get_bucket(bucket_name=bucket_name)
    source_prefix = f"{source_prefix.rstrip('/')}/"
    target_prefix = f"{target_prefix.rstrip('/')}/"
    blobs = list(bucket.list_blobs(prefix=source_prefix))
    moved_files = 0

    for blob in blobs:
        relative_path = blob.name[len(source_prefix) :]
        if not relative_path:
            continue
        destination_name = f"{target_prefix}{relative_path}"
        bucket.copy_blob(blob, bucket, destination_name)
        blob.delete()
        moved_files += 1

    return moved_files > 0


def get_record_image_directory(record_group_id, record_id):
    """
    Return the storage prefix for display images generated for one record:
        uploads/{record-group-id}/{record-id}
    """
    return f"{UPLOADS_PREFIX}/{record_group_id}/{record_id}"


def get_deleted_record_image_directory(record_id):
    """
    Return the storage prefix for display images moved out of active uploads:
        deleted/{record-id}
    """
    return f"{DELETED_PREFIX}/{record_id}"


def move_record_images_to_deleted(record_group_id, record_id, bucket_name=BUCKET_NAME):
    """
    Move a record's display-image directory from active uploads to deleted
    storage. This is the record-delete archive path; callers should not build
    record image keys themselves.
    """
    source_prefix = get_record_image_directory(record_group_id, record_id)
    destination_prefix = get_deleted_record_image_directory(record_id)
    return move_directory(
        source_prefix,
        destination_prefix,
        bucket_name=bucket_name,
    )


def get_file_url(key, bucket_name=BUCKET_NAME):
    if _is_local():
        return f"{LOCAL_STORAGE_URL_BASE}/{quote(key, safe='/')}"

    _, bucket = _get_bucket(bucket_name=bucket_name)
    blob = bucket.blob(f"{key}")
    try:
        url = blob.generate_signed_url(
            version="v4",
            expiration=datetime.timedelta(minutes=15),
            method="GET",
        )
    except Exception:
        _log.info(f"unable to get GCS image for path: {key}")
        url = None
    return url


def get_document_image(rg_id, record_id, filename, bucket_name=BUCKET_NAME):
    path = f"{get_record_image_directory(rg_id, record_id)}/{filename}"
    return get_file_url(path, bucket_name=bucket_name)


def file_exists(key, bucket_name=BUCKET_NAME):
    if _is_local():
        return os.path.isfile(_storage_path(key))
    try:
        _, bucket = _get_bucket(bucket_name=bucket_name)
        blob = bucket.blob(key)
        return blob.exists()
    except Exception as e:
        _log.info(f"Error checking existence for {key}: {e}")
        return False


def iter_file_bytes(key, bucket_name=BUCKET_NAME, chunk_size=65536):
    if _is_local():
        try:
            with open(_storage_path(key), "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
        except FileNotFoundError:
            _log.info(f"local file not found: {key}")
        except Exception as e:
            _log.info(f"Error reading local file {key}: {e}")
        return

    _, bucket = _get_bucket(bucket_name=bucket_name)
    blob = bucket.blob(key)
    try:
        with blob.open("rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk
    except NotFound:
        _log.info(f"Exception, blob not found: {key}")
        return
    except Exception as e:
        _log.info(f"Error downloading {key}: {e}")
        return


def get_file_size(key, bucket_name=BUCKET_NAME):
    if _is_local():
        try:
            return os.path.getsize(_storage_path(key))
        except OSError as e:
            _log.warning(f"Failed to get size of local file {key}: {e}")
            return None
    try:
        _, bucket = _get_bucket(bucket_name=bucket_name)
        blob = bucket.blob(key)
        blob.reload()
        return blob.size
    except NotFound:
        _log.warning(f"blob not found: {key}")
        return None
    except Exception as e:
        _log.error(f"Error retrieving blob size for {key}: {e}")
        return None


def upload_sample_image(file_bytes: bytes, original_filename: str, processor_name: str):
    if _is_local():
        extension = original_filename.split(".")[-1]
        key = f"sample_images/{processor_name}"
        destination = _storage_path(key)
        _ensure_local_dir(destination)
        with open(destination, "wb") as f:
            f.write(file_bytes)
        return get_file_url(key)

    _, bucket = _get_bucket(bucket_name=BUCKET_NAME)
    extension = original_filename.split(".")[-1]
    key = f"sample_images/{processor_name}"

    blob = bucket.blob(key)
    blob.upload_from_string(file_bytes, content_type=f"image/{extension}")

    return get_file_url(key)


def list_files(prefix, bucket_name=None, storage_service_key=None):
    if _is_local():
        base = _storage_path(prefix)
        if not os.path.exists(base):
            return []
        if os.path.isfile(base):
            return [prefix]
        results = []
        for root, _, files in os.walk(base):
            for name in files:
                full_path = os.path.join(root, name)
                results.append(os.path.relpath(full_path, LOCAL_STORAGE_ROOT))
        return results

    _, bucket = _get_bucket(
        bucket_name=bucket_name, storage_service_key=storage_service_key
    )
    return [blob.name for blob in bucket.list_blobs(prefix=prefix)]


def download_file_bytes(key, bucket_name=None, storage_service_key=None):
    if _is_local():
        with open(_storage_path(key), "rb") as f:
            return f.read()

    _, bucket = _get_bucket(
        bucket_name=bucket_name, storage_service_key=storage_service_key
    )
    blob = bucket.blob(key)
    return blob.download_as_bytes()


# ------------------------------------------------------------------------------
# Image rotation
# ------------------------------------------------------------------------------


class GCSLocation(NamedTuple):
    bucket: str
    blob_path: str


def _require_pillow():
    if Image is None:  # pragma: no cover
        raise ImportError("Pillow is required for rotating images.")


def parse_gcs_url(url: str) -> GCSLocation:
    """
    Parse a GCS URL into bucket + blob_path, ignoring query params.

    Supports:
      - gs://bucket/path/to/file.jpg
      - https://storage.googleapis.com/bucket/path/to/file.jpg
      - signed URLs like:
        https://storage.googleapis.com/bucket/path/to/file.png?X-Goog-...
    """
    parsed = urlparse(url)

    # gs://bucket/path/to/object
    if parsed.scheme == "gs":
        bucket = parsed.netloc
        blob_path = unquote(parsed.path.lstrip("/"))
        return GCSLocation(bucket=bucket, blob_path=blob_path)

    # https://storage.googleapis.com/bucket/path/to/object
    if parsed.scheme in ("http", "https") and parsed.netloc == "storage.googleapis.com":
        # parsed.path is "/bucket/path/to/object"
        path = unquote(parsed.path.lstrip("/"))
        bucket, _, blob_path = path.partition("/")
        if not bucket or not blob_path:
            raise ValueError(f"Invalid GCS URL format: {url!r}")
        return GCSLocation(bucket=bucket, blob_path=blob_path)

    # https://{bucket}.storage.googleapis.com/path/to/object
    m = re.match(r"^([^.]+)\.storage\.googleapis\.com$", parsed.netloc)
    if parsed.scheme in ("http", "https") and m:
        bucket = m.group(1)
        blob_path = unquote(parsed.path.lstrip("/"))
        if not blob_path:
            raise ValueError(f"Invalid GCS URL format: {url!r}")
        return GCSLocation(bucket=bucket, blob_path=blob_path)

    raise ValueError(
        f"Unrecognised GCS URL format: {url!r}. "
        "Expected 'gs://bucket/path' or "
        "'https://storage.googleapis.com/bucket/path'."
    )


def rotate_image(image, degrees: float, expand: bool = True):
    """
    Rotate a PIL Image by the given number of degrees.
    """
    _require_pillow()
    return image.rotate(degrees, expand=expand)


def _build_destination_path(blob_path: str, suffix: str) -> str:
    """
    Insert *suffix* before the file extension of *blob_path*.

    Example:
        "photos/beach.jpg", "_rotated"  →  "photos/beach_rotated.jpg"
    """
    # Keep same behavior as the incoming rotator
    filename = blob_path.rsplit("/", 1)[-1]
    if "." in filename:
        base, ext = blob_path.rsplit(".", 1)
        return f"{base}{suffix}.{ext}"
    return f"{blob_path}{suffix}"


def _guess_format_and_content_type(key: str):
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
    pil_format_map = {
        "jpg": "JPEG",
        "jpeg": "JPEG",
        "png": "PNG",
        "gif": "GIF",
        "webp": "WEBP",
        "bmp": "BMP",
        "tif": "TIFF",
        "tiff": "TIFF",
    }
    pil_format = pil_format_map.get(ext, "JPEG")

    content_type_map = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "bmp": "image/bmp",
        "tif": "image/tiff",
        "tiff": "image/tiff",
    }
    content_type = content_type_map.get(ext, "image/jpeg")
    return pil_format, content_type


def _load_pil_image_from_bytes(image_bytes: bytes):
    _require_pillow()
    image = Image.open(io.BytesIO(image_bytes))
    image.load()  # ensure fully decoded
    return image


def _save_pil_image_to_bytes(image, pil_format: str) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format=pil_format)
    buf.seek(0)
    return buf.getvalue()


def rotate_images_in_storage(
    image_keys_or_urls: list[str],
    degrees: float,
    *,
    overwrite: bool = True,
    destination_suffix: str = "_rotated",
    expand: bool = True,
    bucket_name: str = BUCKET_NAME,
    storage_service_key: str | None = None,
) -> dict[str, str]:
    """
    Rotate images stored in either local filesystem storage or Google Cloud Storage.

    Inputs may be either:
      - Storage keys (e.g. "uploads/rg1/rec1/image.jpg")
      - GCS URLs (gs://... or https://storage.googleapis.com/... )

    Returns:
        A dict mapping each input to the destination URL (local URL for local backend,
        and signed URL for cloud backend).
    """
    results: dict[str, str] = {}

    for original in image_keys_or_urls:
        _log.info(f"[rotate_images_in_storage] Processing: {original}")

        if original.startswith("gs://") or original.startswith(
            "https://storage.googleapis.com/"
        ):
            loc = parse_gcs_url(original)
            src_bucket = loc.bucket
            src_key = loc.blob_path
        else:
            src_bucket = bucket_name
            src_key = original

        dest_key = (
            src_key
            if overwrite
            else _build_destination_path(src_key, destination_suffix)
        )

        # Download bytes using existing storage API read helper
        if _is_local():
            src_bytes = download_file_bytes(src_key)
        else:
            src_bytes = download_file_bytes(
                src_key, bucket_name=src_bucket, storage_service_key=storage_service_key
            )

        image = _load_pil_image_from_bytes(src_bytes)

        rotated = rotate_image(image, degrees, expand=expand)

        pil_format, content_type = _guess_format_and_content_type(dest_key)

        # JPEG cannot represent alpha
        if pil_format == "JPEG" and rotated.mode in ("RGBA", "LA", "P"):
            rotated = rotated.convert("RGB")

        rotated_bytes = _save_pil_image_to_bytes(rotated, pil_format=pil_format)

        # Upload back to the appropriate storage backend
        if _is_local():
            destination = _storage_path(dest_key)
            _ensure_local_dir(destination)
            with open(destination, "wb") as f:
                f.write(rotated_bytes)
        else:
            _, dest_bucket = _get_bucket(
                bucket_name=src_bucket, storage_service_key=storage_service_key
            )
            blob = dest_bucket.blob(dest_key)
            blob.upload_from_string(rotated_bytes, content_type=content_type)

        dest_url = get_file_url(dest_key, bucket_name=src_bucket)
        results[original] = dest_url

        _log.info(f"[rotate_images_in_storage]   Saved to: {dest_url}")

    return results
