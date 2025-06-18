import sys
import argparse
import os
import requests
import time
import shutil
from google.cloud import storage
from io import BytesIO


def upload_documents_from_directory(
    user_email,
    record_group_id,
    local_directory=None,
    cloud_bucket=None,
    cloud_directory="",
    storage_service_key=None,
    amount=30,
    preventDuplicates=True,
    db=None,
    reprocessed=False,
    backend_url=None,
):
    print(f"amount to upload: {amount}")
    count = 0
    if record_group_id is None:
        print("please provide a record group id to upload documents to")
        return
    if user_email is None:
        print("please provide a contributor's email")
        return
    if local_directory is None and (cloud_directory is None or cloud_bucket is None):
        print("please provide either a local directory or a cloud directory")
        return
    if backend_url is None:
        backend_url = os.getenv("BACKEND_URL")

    dontAdd = set()
    if preventDuplicates:
        cursor = db["records"].find({"record_group_id": record_group_id})

        for document in cursor:
            dontAdd.add(document.get("filename"))

    post_url = f"{backend_url}/upload_document/{record_group_id}/{user_email}?reprocessed={reprocessed}"
    if local_directory is not None:
        files_to_delete = []
        print(f"uploading documents from {local_directory}")
        for subdir, dirs, files in os.walk(local_directory):
            for file in files:
                file_path = os.path.join(subdir, file)
                files_to_delete.append(file_path)
                if ".pdf" in file.lower():
                    mime_type = "application/pdf"
                elif ".tif" in file.lower():
                    mime_type = "image/tiff"
                elif ".png" in file.lower():
                    mime_type = "image/png"
                elif ".jpg" in file.lower():
                    mime_type = "image/jpeg"
                elif ".jpeg" in file.lower():
                    mime_type = "image/jpeg"
                else:
                    print(f"unable to process file type {file}")
                    mime_type = None

                if mime_type is not None:
                    if file not in dontAdd:
                        print(f"uploading: {file_path} with mimetype {mime_type}")

                        opened_file = open(file_path, "rb")
                        upload_files = {
                            "file": (file, opened_file, mime_type),
                            "Content-Disposition": 'form-data; name="file"; filename="'
                            + file
                            + '"',
                            "Content-Type": mime_type,
                        }
                        requests.post(post_url, files=upload_files)
                        count += 1
                        if count == amount:
                            print(f"reached upload amount, stopping.")
                            return
                    else:
                        print(f"skipping duplicate {file}")
    if cloud_directory is not None and cloud_bucket is not None:
        if storage_service_key is None:
            print(
                "please provide a valid path to a google storage service key json file"
            )
            return
        print(f"uploading documents from {cloud_bucket}/{cloud_directory}")
        try:
            client = storage.Client.from_service_account_json(
                f"./{storage_service_key}"
            )
        except Exception as e:
            print(
                "please provide a valid path to a google storage service key json file"
            )
            return
        bucket = client.bucket(cloud_bucket)
        for blob in bucket.list_blobs(prefix=cloud_directory):
            file_name = blob.name.replace(f"{cloud_directory}/", "")
            if ".pdf" in file_name.lower():
                mime_type = "application/pdf"
            elif ".tif" in file_name.lower():
                mime_type = "image/tiff"
            elif ".png" in file_name.lower():
                mime_type = "image/png"
            elif ".jpg" in file_name.lower():
                mime_type = "image/jpeg"
            elif ".jpeg" in file_name.lower():
                mime_type = "image/jpeg"
            else:
                print(f"unable to process file type {file_name}")
                mime_type = None

            if mime_type is not None:
                if file_name not in dontAdd:
                    print(f"uploading {mime_type}: {file_name}")
                    doc = BytesIO(blob.download_as_bytes())
                    upload_files = {
                        "file": (file_name, doc, mime_type),
                        "Content-Disposition": 'form-data; name="file"; filename="'
                        + file_name
                        + '"',
                        "Content-Type": mime_type,
                    }
                    requests.post(post_url, files=upload_files)
                    count += 1
                    if count == amount:
                        print(f"reached upload amount, stopping")
                        return
                else:
                    print(f"skipping duplicate {file}")

    if preventDuplicates:
        client.close()
