import sys
import argparse
import os
import requests
import time
import shutil
from google.cloud import storage
from io import BytesIO
from dotenv import load_dotenv

load_dotenv()

import certifi
import urllib.parse
from pymongo.mongo_client import MongoClient
from pymongo.server_api import ServerApi


def connectToDatabase(DB_USERNAME, DB_PASSWORD, DB_CONNECTION):
    ca = certifi.where()
    username = urllib.parse.quote_plus(DB_USERNAME)
    password = urllib.parse.quote_plus(DB_PASSWORD)
    db_connection = urllib.parse.quote_plus(DB_CONNECTION)

    uri = f"mongodb+srv://{username}:{password}@{db_connection}.mongodb.net/?retryWrites=true&w=majority"
    print(f"trying to connect to {uri}")
    client = MongoClient(uri, server_api=ServerApi("1"), tlsCAFile=ca)
    # Send a ping to confirm a successful connection
    try:
        client.admin.command("ping")
        print("Successfully connected to MongoDB!")
    except Exception as e:
        print(f"unable to connect to db: {e}")

    return client


def upload_documents_from_directory(
    user_email=None,
    project_id=None,
    local_directory=None,
    cloud_bucket=None,
    cloud_directory="",
    delete_local_files=False,
    storage_service_key=None,
    amount=30,
    preventDuplicates=True,
    ogrre_version="uow",
    DB_USERNAME=None,
    DB_PASSWORD=None,
    DB_CONNECTION=None,
):
    WAIT_TIME_BETWEEN_UPLOADS = 120
    count = 0
    if project_id is None:
        print("please provide a project id to upload documents to")
        return
    if user_email is None:
        print("please provide a contributor's email")
        return
    if local_directory is None and (cloud_directory is None or cloud_bucket is None):
        print("please provide either a local directory or a cloud directory")
        return
    if ogrre_version == None or ogrre_version.lower() == "uow":
        backend_url = f"https://server.uow-carbon.org"
    elif ogrre_version.lower() == "isgs":
        backend_url = f"https://isgs-server.uow-carbon.org"

    if preventDuplicates:
        if DB_USERNAME is None:
            DB_USERNAME = os.getenv("DB_USERNAME")
        if DB_PASSWORD is None:
            DB_PASSWORD = os.getenv("DB_PASSWORD")
        if DB_CONNECTION is None:
            DB_CONNECTION = os.getenv("DB_CONNECTION")
        client = connectToDatabase(DB_USERNAME, DB_PASSWORD, DB_CONNECTION)
        db = client[ogrre_version]
        cursor = db["records"].find({})
        dontAdd = []
        for document in cursor:
            dontAdd.append(document.get("name"))

    post_url = f"{backend_url}/upload_document/{project_id}/{user_email}"
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
                    if file.split(".")[0] not in dontAdd:
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
                            print(
                                f"reached upload amount. waiting {WAIT_TIME_BETWEEN_UPLOADS} seconds before continuing"
                            )
                            time.sleep(WAIT_TIME_BETWEEN_UPLOADS)
                            count = 0
                    else:
                        print(f"not adding duplicate {file}")
        if delete_local_files:
            time_to_wait = len(files_to_delete) + 120
            print(f"removing {files_to_delete} in {time_to_wait} seconds")
            time.sleep(time_to_wait)
            try:
                print(f"removing {files_to_delete}")
                shutil.rmtree(local_directory)
            except Exception as e:
                print(f"unable to delete {files_to_delete}: {e}")
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
                if file_name.split(".")[0] not in dontAdd:
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
                        print(
                            f"reached upload amount. waiting {WAIT_TIME_BETWEEN_UPLOADS} seconds before continuing"
                        )
                        time.sleep(WAIT_TIME_BETWEEN_UPLOADS)
                        count = 0
                else:
                    print(f"not adding duplicate {file}")

    if preventDuplicates:
        client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--project_id", help="Project id to upload documents to")
    parser.add_argument("-e", "--email", help="Email of contributor")
    parser.add_argument(
        "-l", "--local_directory", help="Location of local directory. O"
    )
    parser.add_argument("-b", "--cloud_bucket", help="Location of cloud bucket")
    parser.add_argument("-c", "--cloud_directory", help="Location of cloud directory")
    parser.add_argument(
        "-s", "--storage_service_key", help="Location of storage service key"
    )
    parser.add_argument(
        "-a", "--amount", help="Amount of records to upload before waiting to continue"
    )
    parser.add_argument(
        "-v",
        "--OGRRE_version",
        help="Version of OGRRE that project is on (uow or isgs)",
    )
    parser.add_argument(
        "-d",
        "--prevent_duplicates",
        help="Prevent duplicates from being uploaded to OGRRE",
    )
    parser.add_argument(
        "-dbu",
        "--DB_USERNAME",
        help="Database Username. This can also be stored in .env file.",
    )
    parser.add_argument(
        "-dbp",
        "--DB_PASSWORD",
        help="Database Password. This can also be stored in .env file.",
    )
    parser.add_argument(
        "-dbc",
        "--DB_CONNECTION",
        help="Database Connection String. This can also be stored in .env file.",
    )
    args = parser.parse_args()

    cloud_directory = ""
    if args.cloud_directory is not None:
        cloud_directory = args.cloud_directory

    amount = 3
    if args.amount is not None:
        amount = args.amount
    else:
        print(f"amount not supplied. rolling with {amount}")

    preventDuplicates = True
    if args.prevent_duplicates is not None:
        preventDuplicates = args.prevent_duplicates
    upload_documents_from_directory(
        user_email=args.email,
        project_id=args.project_id,
        local_directory=args.local_directory,
        cloud_bucket=args.cloud_bucket,
        cloud_directory=cloud_directory,
        delete_local_files=False,
        storage_service_key=args.storage_service_key,
        amount=amount,
        preventDuplicates=preventDuplicates,
        ogrre_version=args.OGRRE_version,
        DB_USERNAME=args.DB_USERNAME,
        DB_PASSWORD=args.DB_PASSWORD,
        DB_CONNECTION=args.DB_CONNECTION,
    )

    ## EXAMPLES

    ## from local directory
    ## python bulk_upload_script.py -v <ogrre-version> -e <uploader-email> -p <project-id> -l <absolute-path-to-directory-containing-records> -a <amount-to-upload-between-waiting>

    ## from cloud
    ## python bulk_upload_script.py -v <ogrre-version> -e <uploader-email> -p <project-id> -b <google-cloud-bucket> -c <cloud-directory> -s <absolute-path-to-storage-service-key> -a <amount-to-upload-between-waiting>
