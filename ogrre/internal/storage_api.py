import datetime
import logging
import os
import sys
from pathlib import Path

import aiofiles
import aiohttp
from gcloud.aio.storage import Storage
from google.api_core.exceptions import NotFound
from google.cloud import storage

_log = logging.getLogger(__name__)

DIRNAME, _ = os.path.split(os.path.abspath(sys.argv[0]))
STORAGE_SERVICE_KEY = os.getenv("STORAGE_SERVICE_KEY")
BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME")
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "google").lower()
LOCAL_STORAGE_ROOT = os.path.expanduser(
    os.getenv("LOCAL_STORAGE_ROOT", "~/.ogrre/uploads")
)


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


async def upload_file(file_path, file_name, folder="uploads"):
    key = f"{folder}/{file_name}" if folder else file_name
    if _is_local():
        destination = _storage_path(key)
        _ensure_local_dir(destination)
        async with aiofiles.open(file_path, "rb") as src:
            async with aiofiles.open(destination, "wb") as dst:
                while True:
                    chunk = await src.read(1024 * 1024)
                    if not chunk:
                        break
                    await dst.write(chunk)
        _log.info(f"uploaded document to local storage: {destination}")
        return destination

    async with aiofiles.open(file_path, "rb") as afp:
        f = await afp.read()
    url = await _async_upload_to_bucket(file_name, f, folder=folder)
    _log.info(f"uploaded document to cloud storage: {url}")
    return url


async def _async_upload_to_bucket(
    blob_name,
    file_obj,
    folder,
    bucket_name=BUCKET_NAME,
    service_file=None,
):
    async with aiohttp.ClientSession() as session:
        storage_client = Storage(
            service_file=service_file or f"{DIRNAME}/creds.json", session=session
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


def get_file_url(key, bucket_name=BUCKET_NAME):
    if _is_local():
        return _storage_path(key)

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
    path = f"uploads/{rg_id}/{record_id}/{filename}"
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
