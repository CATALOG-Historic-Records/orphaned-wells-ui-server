import time
import os
import logging
import zipfile
from io import BytesIO
from google.cloud import storage
import datetime
import sys
import requests
import functools
import tempfile
import zipstream
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.cloud import storage

import ogrre_data_cleaning.clean as OGRRE_cleaning_functions

CLEANING_FUNCTIONS = {
    "clean_bool": OGRRE_cleaning_functions.clean_bool,
    "string_to_int": OGRRE_cleaning_functions.string_to_int,
    "string_to_float": OGRRE_cleaning_functions.string_to_float,
    "string_to_date": OGRRE_cleaning_functions.string_to_date,
    "clean_date": OGRRE_cleaning_functions.clean_date,
    "convert_hole_size_to_decimal": OGRRE_cleaning_functions.convert_hole_size_to_decimal,
    "llm_clean": OGRRE_cleaning_functions.llm_clean,
}

_log = logging.getLogger(__name__)
DIRNAME, FILENAME = os.path.split(os.path.abspath(sys.argv[0]))
STORAGE_SERVICE_KEY = os.getenv("STORAGE_SERVICE_KEY")
BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME")


def time_it(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        end_time = time.perf_counter()
        elapsed_time = end_time - start_time
        _log.info(f"Function '{func.__name__}' executed in {elapsed_time:.2f} seconds")
        return result

    return wrapper


def sortRecordAttributes(attributes, processor, keep_all_attributes=False):
    processor_attributes = processor["attributes"]
    processor_attributes.sort(key=lambda x: x.get("page_order_sort", float("inf")))

    ## we want to make sure that the frontend and backend are always in sync.
    ## for now, update the db with this sorted list every time before returning
    requires_db_update = len(processor_attributes) > 0

    ## match record attribute to each processor attribute
    sorted_attributes = []
    processor_attributes_dict = convert_processor_attributes_to_dict(
        processor_attributes
    )
    for each in processor_attributes:
        attribute_name = each["name"]
        if "::" not in attribute_name:
            found_ = [item for item in attributes if item["key"] == attribute_name]
            for attribute in found_:
                if attribute is None:
                    _log.debug(f"{attribute_name} is None")
                else:
                    sorted_attributes.append(attribute)
            if len(found_) == 0:
                _log.debug(
                    f"{attribute_name} was not in record's attributes. adding this to the sorted attributes"
                )
                new_attr = createNewAttribute(key=attribute_name)
                sorted_attributes.append(new_attr)
                requires_db_update = True

    if keep_all_attributes:
        ## obsolete fields will get removed automatically.
        for attr in attributes:
            attribute_name = attr["key"]
            if attribute_name not in processor_attributes_dict:
                _log.info(
                    f"{attribute_name} was not in processor's attributes. adding this to the end of the sorted attributes list"
                )
                sorted_attributes.append(attr)

    return sorted_attributes, requires_db_update


def imageIsValid(image):
    ## some broken records have letters saved where image names should be
    ## for now, just ensure that the name is at least 3 characters long
    try:
        if len(image) > 2:
            return True
        else:
            return False
    except Exception as e:
        _log.error(f"unable to check validity of image: {e}")
        return False


def deleteFiles(filepaths, sleep_time=5):
    _log.info(f"deleting files: {filepaths} in {sleep_time} seconds")
    time.sleep(sleep_time)
    for filepath in filepaths:
        if os.path.isfile(filepath):
            os.remove(filepath)
            _log.info(f"deleted {filepath}")


def validateUser(user):
    ## just make sure that this is a real user with roles
    try:
        if user.get("roles", None):
            return True
        else:
            return False
    except Exception as e:
        _log.error(f"failed attempting to validate user {user}: {e}")


def generate_gcs_paths(documents):
    if not documents or len(documents) == 0:
        return []
    gcs_paths = {}
    for record_id, document in documents.items():
        # print(f"{record_id}")
        rg_id = document["rg_id"]
        record_name = document["record_name"]
        for image_file in document.get("files", []):
            blob_path = f"uploads/{rg_id}/{record_id}/{image_file}"
            arcname = f"documents/{record_name}/{os.path.basename(image_file)}"
            gcs_paths[blob_path] = arcname
    return gcs_paths


def generate_download_signed_url_v4(
    rg_id, record_id, filename, bucket_name=BUCKET_NAME
):
    """Generates a v4 signed URL for downloading a blob.

    Note that this method requires a service account key file. You can not use
    this if you are using Application Default Credentials from Google Compute
    Engine or from the Google Cloud SDK.
    To generate STORAGE_SERVICE_KEY, follow steps here:
    https://docs.gspread.org/en/latest/oauth2.html#for-bots-using-service-account
    """

    storage_client = storage.Client.from_service_account_json(
        f"{DIRNAME}/internal/{STORAGE_SERVICE_KEY}"
    )

    # blob_name: path to file in google cloud bucket
    blob_name = f"uploads/{rg_id}/{record_id}/{filename}"
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    url = blob.generate_signed_url(
        version="v4",
        # This URL is valid for 15 minutes
        expiration=datetime.timedelta(minutes=15),
        # Allow GET requests using this URL.
        method="GET",
    )

    return url


def compileDocumentImageList(records):
    images = {}
    for record in records:
        rg_id = record.get("record_group_id", None)
        record_id = str(record["_id"])
        record_name = record.get("name", record_id)
        image_files = record.get("image_files", [])
        images[record_id] = {
            "files": image_files,
            "rg_id": rg_id,
            "record_id": record_id,
            "record_name": record_name,
        }

    return images


@time_it
def zip_files_stream(local_file_paths, documents=[]):
    """
    Streams a ZIP file directly without writing to temp files.
    Includes optional local files
    """
    start_total = time.time()
    if documents is None:
        documents = []
    _log.info(
        f"downloading and zipping {len(documents)} images along with {local_file_paths}"
    )

    zs = zipstream.ZipFile(mode="w", compression=zipstream.ZIP_STORED)

    # Add CSV and JSON first
    if local_file_paths:
        for file_path in local_file_paths:
            if os.path.isfile(file_path):
                zs.write(file_path, os.path.basename(file_path))

    client = storage.Client.from_service_account_json(
        f"{DIRNAME}/internal/{STORAGE_SERVICE_KEY}"
    )
    bucket = client.bucket(BUCKET_NAME)

    gcs_paths = generate_gcs_paths(documents)

    for gcs_path in gcs_paths:
        blob = bucket.blob(gcs_path)
        arcname = gcs_paths[gcs_path]

        def gcs_yield_chunks():
            _log.debug(f"Starting download: {gcs_path} -> {arcname}")
            start_file = time.time()
            bytes_read = 0

            with blob.open("rb") as f:
                while True:
                    chunk = f.read(65536)  # 64KB chunks
                    if not chunk:
                        break
                    bytes_read += len(chunk)
                    yield chunk

            elapsed_file = time.time() - start_file
            mb_size = bytes_read / (1024 * 1024)
            speed = (mb_size / elapsed_file) if elapsed_file > 0 else 0
            _log.debug(
                f"Finished {arcname}: {mb_size:.2f} MB in {elapsed_file:.2f} s ({speed:.2f} MB/s)"
            )

        zs.write_iter(arcname, gcs_yield_chunks())

    def streaming_generator():
        for chunk in zs:
            yield chunk
        elapsed_total = time.time() - start_total
        _log.info(f"{len(documents)} files streamed in {elapsed_total:.2f} seconds")

    return streaming_generator()


def searchRecordForErrorsAndTargetKeys(document, target_keys=None):
    """
    Checks if any attributes or subattributes have cleaning errors.
    Also retrieves values for attributes whose 'key' is in target_keys.

    Args:
        document (dict): The document to inspect.
        target_keys (list[str], optional): List of attribute keys to locate values for.

    Returns:
        tuple: (hasError, found_values)
            - hasError (bool): True if cleaning errors found.
            - found_values (dict): Mapping of target_keys to their located values.
    """
    if target_keys is None:
        target_keys = ["T", "Sec"]

    hasError = False
    found_values = {}

    try:
        attributes = document.get("attributesList") or []

        # Check attributes for errors & locate target key values
        for attr in attributes:
            if attr is None:
                continue

            if attr.get("cleaning_error", False):
                hasError = True

            key_name = attr.get("key")
            if key_name in target_keys:
                found_values[key_name] = attr.get("value")

            # Check subattributes for errors & locate target key values
            for sub in attr.get("subattributes") or []:
                if sub is None:
                    continue

                if sub.get("cleaning_error", False):
                    hasError = True

                sub_key = sub.get("key")
                if sub_key in target_keys:
                    found_values[sub_key] = sub.get("value")

    except Exception as e:
        _log.info(
            f"unable to searchRecordForErrorsAndTargetKeys for document: {document.get('_id')}"
        )
        _log.info(f"e: {e}")

    return hasError, found_values


def convert_processor_attributes_to_dict(attributes):
    attributes_dict = {}
    if attributes:
        for attr in attributes:
            key = attr["name"]
            attributes_dict[key] = attr
            subattributes = attr.get("subattributes", None)
            if subattributes:
                for subattribute in subattributes:
                    sub_key = subattribute["name"]
                    attributes_dict[f"{key}::{sub_key}"] = subattribute
    return attributes_dict


def convert_processor_list_to_dict(processor_list):
    processor_dict = {}
    if processor_list:
        for each in processor_list:
            key = each["Processor ID"]
            processor_dict[key] = each
    return processor_dict


def cleanRecordAttribute(processor_attributes, attribute, subattributeKey=None):
    if subattributeKey:
        attribute_key = subattributeKey
    else:
        attribute_key = attribute["key"]
    unclean_val = attribute["value"]

    attribute_schema = processor_attributes.get(attribute_key)
    if attribute_schema:
        cleaning_function_name = attribute_schema.get("cleaning_function")
        if cleaning_function_name == "" or cleaning_function_name is None:
            _log.debug(f"cleaning_function for {attribute_key} is empty string or none")
            attribute["cleaned"] = False
            return False
        cleaning_function = CLEANING_FUNCTIONS.get(cleaning_function_name)
        if cleaning_function:
            try:
                cleaned_val = cleaning_function(unclean_val)
                _log.debug(f"CLEANED: {unclean_val} : {cleaned_val}")
                attribute["value"] = cleaned_val
                attribute["normalized_value"] = cleaned_val
                attribute["uncleaned_value"] = unclean_val
                attribute["cleaned"] = True
                attribute["cleaning_error"] = False
                attribute["last_cleaned"] = time.time()
                return True
            except Exception as e:
                _log.error(f"unable to clean {attribute_key}: {e}")
                attribute["cleaning_error"] = f"{e}"
                attribute["cleaned"] = False
        else:
            _log.info(f"no cleaning function with name: {cleaning_function_name}")

        subattributes = attribute.get("subattributes", None)
        if subattributes:
            for subattribute in subattributes:
                subattribute_key = f"{attribute_key}::{subattribute['key']}"
                cleanRecordAttribute(
                    processor_attributes, subattribute, subattributeKey=subattribute_key
                )

    else:
        _log.info(f"no schema found for {attribute_key}")
    return False


def cleanRecords(processor_attributes, documents):
    for doc in documents:
        attributes_list = doc["attributesList"]
        for attr in attributes_list:
            cleanRecordAttribute(
                processor_attributes=processor_attributes, attribute=attr
            )
    return documents


def createNewAttribute(
    key,
    value=None,
    confidence=None,
    subattributes=None,
    page=None,
    coordinates=None,
    normalized_value=None,
    raw_text=None,
    text_value=None,
):
    new_attribute = {
        "key": key,
        "ai_confidence": confidence,
        "confidence": confidence,
        "raw_text": raw_text,
        "text_value": text_value,
        "value": value,
        "normalized_vertices": coordinates,
        "normalized_value": normalized_value,
        "subattributes": subattributes,
        "isSubattribute": False,
        "edited": False,
        "page": page,
    }
    return new_attribute


def defaultJSONDumpHandler(obj):
    if isinstance(obj, datetime.datetime):
        date_string = obj.date().isoformat()
        _log.info(
            f"JSON Dump found datetime object, returning iso format: {date_string}"
        )
        return date_string
    else:
        _log.info(f"JSON Dump found Type {type(obj)}. returning string")
        return str(obj)


def normalize_sort_direction(direction):
    """
    Ensures sort direction is an integer 1 or -1.
    Accepts 'asc', 'desc', 'ascending', 'descending', etc.
    """
    if isinstance(direction, int):
        if direction in (1, -1):
            return direction
        raise ValueError("Sort direction integer must be 1 or -1")
    if isinstance(direction, str):
        dir_lower = direction.lower()
        if dir_lower in ("asc", "ascending"):
            return 1
        elif dir_lower in ("desc", "descending"):
            return -1
    raise ValueError(f"Invalid sort direction value: {direction}")


def generate_sort_filter_pipeline(
    filter_by,
    primary_sort,
    records_per_page=None,
    page=None,
    for_ranking=False,
    secondary_sort=None,
):
    """
    Generates the part of the pipeline that applies filtering, extraction of sort values,
    and sorting (including secondary keys when desired) in a consistent way.

    filter_by : dictionary in mongo filters format
    primary_sort : list [field, direction]
    secondary_sort : list [field, direction]
    for_ranking : If True, will produce a 'sortComposite' field and $sort/$setWindowFields with a single top-level sort key.
    """
    if records_per_page is not None and page is not None:
        include_paging = True
    else:
        include_paging = False

    pipeline = [{"$match": filter_by}]

    primary_sort_key = primary_sort[0]
    primary_sort_dir = primary_sort[1]

    if secondary_sort is None:
        secondary_sort_key = None
        secondary_sort_dir = None
    else:
        secondary_sort_key = secondary_sort[0]
        secondary_sort_dir = secondary_sort[1]

    # Handle attributesList.* case
    if primary_sort_key.startswith("attributesList."):
        attr_key_name = primary_sort_key.split(".", 1)[1]

        ## If we are sorting by an attribute, we need a secondary sort
        if secondary_sort_key is None:
            secondary_sort_key, secondary_sort_dir = "dateCreated", -1

        pipeline.append(
            {
                "$addFields": {
                    "targetValue": {
                        "$first": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$attributesList",
                                        "as": "attr",
                                        "cond": {"$eq": ["$$attr.key", attr_key_name]},
                                    }
                                },
                                "as": "targetAttr",
                                "in": "$$targetAttr.value",
                            }
                        }
                    }
                }
            }
        )

        if secondary_sort_key:
            # Create a composite field to sort by
            pipeline.append(
                {
                    "$addFields": {
                        "sortComposite": ["$targetValue", f"${secondary_sort_key}"]
                    }
                }
            )
            pipeline.append({"$sort": {"sortComposite": primary_sort_dir}})
        else:
            pipeline.append({"$sort": {"targetValue": primary_sort_dir}})
            pipeline.append({"$project": {"targetValue": 0}})

    else:  # Sorting by top level field
        if secondary_sort_key:
            pipeline.append(
                {
                    "$addFields": {
                        "sortComposite": [
                            f"${primary_sort_key}",
                            f"${secondary_sort_key}",
                        ]
                    }
                }
            )
            pipeline.append({"$sort": {"sortComposite": primary_sort_dir}})
        else:
            sort_stage = {primary_sort_key: primary_sort_dir}
            pipeline.append({"$sort": sort_stage})

    if for_ranking:
        ## TODO: we're ignoring secondary sort key in here.
        # if secondary_sort_key:
        if "attributesList." in primary_sort_key:
            # Ranking with composite field
            pipeline.append(
                {
                    "$setWindowFields": {
                        "sortBy": {
                            "targetValue": primary_sort_dir,
                            # "sortComposite": primary_sort_dir,
                        },
                        "output": {
                            "rank": {"$documentNumber": {}},
                            "prevId": {"$shift": {
                                "by": -1,
                                "output": { "$toString": "$_id" }
                            }},
                            "nextId": {"$shift": {
                                "by": 1,
                                "output": { "$toString": "$_id" }
                            }},
                        },
                    }
                }
            )
        else:
            # primary-only sort
            pipeline.append(
                {
                    "$setWindowFields": {
                        "sortBy": {
                            primary_sort_key: primary_sort_dir,
                        },
                        "output": {
                            "rank": {"$documentNumber": {}},
                            "prevId": {"$shift": {"by": -1, "output": "$_id"}},
                            "nextId": {"$shift": {"by": 1, "output": "$_id"}},
                        },
                    }
                }
            )

    # Optional paging
    if include_paging and records_per_page is not None and page is not None:
        pipeline.append({"$skip": records_per_page * page})
        pipeline.append({"$limit": records_per_page})

    return pipeline
