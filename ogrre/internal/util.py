import time
import os
import logging
from google.cloud import storage
import datetime
import sys
import functools
import zipstream
from google.cloud import storage
from google.api_core.exceptions import NotFound

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


def last4_before_decimal(ts=None):
    if ts is None:
        ts = time.time()
    return int(ts) % 10000


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
    if processor is None:
        _log.info(f"no processor found")
        return attributes, False
    processor_attributes = processor.get("attributes", None)
    if processor_attributes is None or len(processor_attributes) == 0:
        _log.info(f"no processor attributes found")
        return attributes, False
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
        rg_id = document["rg_id"]
        record_name = document["record_name"]
        image_files = document.get("files", [])
        for image_file in image_files:
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
        f"{DIRNAME}/{STORAGE_SERVICE_KEY}"
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
def compute_total_size(local_file_paths, gcs_paths):
    """
    Compute total bytes of local files + google cloud images.
    local_file_paths: list of paths
    gcs_paths: dict or list of google cloud storage image paths
    """
    total_size = 0

    # Local file sizes
    for file_path in local_file_paths or []:
        if os.path.isfile(file_path):
            try:
                total_size += os.path.getsize(file_path)
            except OSError as e:
                _log.warning(f"Failed to get size of local file {file_path}: {e}")
        else:
            _log.warning(f"Local file not found: {file_path}")

    # GCS blob sizes
    try:
        storage_client = storage.Client.from_service_account_json(
            f"{DIRNAME}/{STORAGE_SERVICE_KEY}"
        )
        bucket = storage_client.bucket(BUCKET_NAME)

        for blob_name in gcs_paths or []:
            try:
                blob = bucket.blob(blob_name)
                blob.reload()  # fetch metadata from GCS
                if blob.size is not None:
                    total_size += blob.size
                else:
                    _log.warning(f"blob has no size info: {blob_name}")
            except NotFound:
                _log.warning(f"blob not found: {blob_name}")
            except Exception as e:
                _log.error(f"Error retrieving blob size for {blob_name}: {e}")

    except Exception as e:
        _log.error(f"Error initializing GCS client or bucket: {e}")

    return total_size


@time_it
def zip_files_stream(local_file_paths, documents=[], log_to_file="zip_log.txt"):
    """
    Streams a ZIP file directly without writing to temp files.
    Includes optional local files (JSON and/or csv), skips missing ones gracefully.
    """
    start_total = time.time()
    log_file = None
    if log_to_file:
        log_file = open(log_to_file, "w")

        def logg(msg, level="debug"):
            log_file.write(msg + "\n")
            if level == "debug":
                _log.debug(msg)
            elif level == "info":
                _log.info(msg)

    else:
        logg = _log.info

    if documents is None:
        documents = []
    logg(
        f"Downloading and zipping {len(documents)} images along with {local_file_paths}",
        level="info",
    )

    zs = zipstream.ZipFile(mode="w", compression=zipstream.ZIP_STORED, allowZip64=True)

    # Add csv and/or json first
    if local_file_paths:
        for file_path in local_file_paths:
            if os.path.isfile(file_path):
                zs.write(file_path, os.path.basename(file_path))
            else:
                logg(f"Local file not found, skipping: {file_path}", level="info")

    client = storage.Client.from_service_account_json(
        f"{DIRNAME}/{STORAGE_SERVICE_KEY}"
    )
    bucket = client.bucket(BUCKET_NAME)

    gcs_paths = generate_gcs_paths(documents)
    i = 0
    not_found_amt = 0
    for gcs_path in gcs_paths:
        i += 1
        blob = bucket.blob(gcs_path)
        # check if blob exists before writing to ZIP
        try:
            if not blob.exists():
                not_found_amt += 1
                logg(f"image #{i} not found, skipping: {gcs_path}", level="info")
                continue
        except Exception as e:
            logg(f"Error checking existence for {gcs_path}: {e}", level="info")
            continue
        arcname = gcs_paths[gcs_path]

        def gcs_yield_chunks(blob, arcname, gcs_path, i):
            logg(f"Starting download #{i}: {gcs_path} -> {arcname}")
            start_file = time.time()
            bytes_read = 0
            try:
                with blob.open("rb") as f:
                    while True:
                        chunk = f.read(65536)  # 64KB
                        if not chunk:
                            break
                        bytes_read += len(chunk)
                        yield chunk
            except NotFound:
                logg(f"Exception, blob not found: {gcs_path}", level="info")
                return
            except Exception as e:
                logg(f"Error downloading {gcs_path}: {e}", level="info")
                return

            elapsed_file = time.time() - start_file
            mb_size = bytes_read / (1024 * 1024)
            speed = (mb_size / elapsed_file) if elapsed_file > 0 else 0
            logg(
                f"Downloaded #{i}: {mb_size:.2f} MB in {elapsed_file:.2f} s ({speed:.2f} MB/s)"
            )

            # Add log file to download on last iteration
            if i == len(gcs_paths):
                if log_to_file and os.path.isfile(log_to_file):
                    elapsed_total = time.time() - start_total
                    logg(
                        f"FINISHED: {i - not_found_amt} files streamed in {elapsed_total:.2f} seconds",
                        level="none",
                    )
                    log_file.flush()
                    zs.write(log_to_file, os.path.basename(log_to_file))
                elif log_to_file:
                    _log.info(f"Log text file is not found: {log_to_file}")

        zs.write_iter(arcname, gcs_yield_chunks(blob, arcname, gcs_path, i))

    def streaming_generator():
        for chunk in zs:
            yield chunk
        elapsed_total = time.time() - start_total
        logg(
            f"{len(documents)} files streamed in {elapsed_total:.2f} seconds",
            level="info",
        )

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


def generate_mongo_pipeline(
    filter_by,
    primary_sort,
    records_per_page=None,
    page=None,
    for_ranking=False,
    secondary_sort=None,
    convert_target_value_to_number=False,
    match_record_id=None,
    include_attribute_fields: dict = None,
    exclude_attribute_fields: dict = None,  ##TODO: add functionality for this
    forDownload: bool = False,
):
    """
    Generates pipeline that applies filtering, complex sorting, paging.

    filter_by : dictionary in mongo filters format
    primary_sort : list [field, direction]
        To sort by field inside attributesList, send parameter in as "attributesList.<field-name>"
    records_per_page : number
    page : number
    for_ranking : If True, will produce a 'sortComposite' field and $sort/$setWindowFields with a single top-level sort key.
    secondary_sort : list [field, direction]
    convert_target_value_to_number : for sorting attributesList field.
        if true, will attempt to use regex to convert field to number before applying sort
    match_record_id : ObjectID for record that we want to find
    include_attribute_fields : dict
        {
            topLevelFields: [],
            attributesList: [],
        }
    exclude_attribute_fields : dict
    """
    pipeline = [{"$match": filter_by}]

    if include_attribute_fields:
        project = {"$project": {}}
        topLevelFields = include_attribute_fields.get("topLevelFields", [])
        attributesListFields = include_attribute_fields.get("attributesList", [])
        for topLevelField in topLevelFields:
            project["$project"][topLevelField] = 1

        if len(attributesListFields) > 0:
            attributesList_include = {}
            for attributesListField in attributesListFields:
                attributesList_include[
                    attributesListField
                ] = f"$$attr.{attributesListField}"
            project["$project"]["attributesList"] = {
                "$map": {
                    "input": "$attributesList",
                    "as": "attr",
                    "in": attributesList_include,
                }
            }
        pipeline.append(project)

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
        primary_sort_key = attr_key_name

        pipeline.append(
            {
                "$addFields": {
                    "targetValue": {
                        "$ifNull": [
                            {
                                "$first": {
                                    "$map": {
                                        "input": {
                                            "$filter": {
                                                "input": "$attributesList",
                                                "as": "attr",
                                                "cond": {
                                                    "$eq": ["$$attr.key", attr_key_name]
                                                },
                                            }
                                        },
                                        "as": "targetAttr",
                                        "in": "$$targetAttr.value",
                                    }
                                }
                            },
                            "",  # replace null with empty string
                        ]
                    }
                }
            }
        )

        target = "targetValue"
        if convert_target_value_to_number:
            target = "targetNumber"

            pipeline.append(
                {
                    "$addFields": {
                        "targetNumber": {
                            "$let": {
                                "vars": {
                                    "match": {
                                        "$regexFind": {
                                            "input": {"$toString": "$targetValue"},
                                            # "input": "$targetValue",
                                            "regex": "\\d+",
                                        }
                                    }
                                },
                                "in": {
                                    "$cond": [
                                        {"$ne": ["$$match", None]},
                                        {"$toInt": "$$match.match"},
                                        None,
                                    ]
                                },
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
                        "sortComposite": [f"${target}", f"${secondary_sort_key}"]
                    }
                }
            )
            pipeline_sort = {"sortComposite": primary_sort_dir}
            pipeline.append({"$sort": pipeline_sort})
        else:
            pipeline_sort = {f"{target}": primary_sort_dir}
            pipeline.append({"$sort": pipeline_sort})

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
            pipeline_sort = {"sortComposite": primary_sort_dir}
            pipeline.append({"$sort": pipeline_sort})
        else:
            sort_stage = {primary_sort_key: primary_sort_dir}
            pipeline_sort = sort_stage
            pipeline.append({"$sort": pipeline_sort})

    if (
        for_ranking
    ):  # Adds rank (record index) and previous, next ids using setWindowFields
        ## Note: $setWindowFields appears to sort differently than the regular sort applied above, even when sorting on the
        ## same keys. Because of this, we apply $setWindowFields even when we don't need the rank.

        ## TODO: implement secondary sort key in here
        ## we'll need to use the sortComposite field
        pipeline.append(
            {
                "$setWindowFields": {
                    "sortBy": pipeline_sort,
                    "output": {
                        "rank": {"$documentNumber": {}},
                        "prevId": {
                            "$shift": {"by": -1, "output": {"$toString": "$_id"}}
                        },
                        "nextId": {
                            "$shift": {"by": 1, "output": {"$toString": "$_id"}}
                        },
                        # get first and last _id in sorted window
                        "firstId": {"$first": {"output": {"$toString": "$_id"}}},
                        "lastId": {"$last": {"output": {"$toString": "$_id"}}},
                    },
                }
            }
        )

    if match_record_id:
        pipeline.append({"$match": {"_id": match_record_id}})

        ## Wrap-around cases:
        ## If we fetched the last record, nextId will be null; use firstId in this case
        ## If we fetched the first record, prevId will be null; use lastId in this case
        pipeline.append(
            {
                "$addFields": {
                    "prevId": {"$ifNull": ["$prevId", "$lastId.output"]},
                    "nextId": {"$ifNull": ["$nextId", "$firstId.output"]},
                }
            }
        )

    # Optional paging
    if records_per_page is not None and page is not None:
        pipeline.append({"$skip": records_per_page * page})
        pipeline.append({"$limit": records_per_page})

    if forDownload:
        ## no need for sorting if we are downloading the records
        pipeline = [
            stage
            for stage in pipeline
            if not any(k in stage for k in ["$setWindowFields", "$sort"])
        ]
    return pipeline


def remap_airtable_keys(original_dict):
    key_map = {
        "Page Sort Order": "page_order_sort",
        "Name": "name",
        "Alias": "alias",
        "Google Data Type": "google_data_type",
        "Occurrence": "occurrence",
        "Database Data Type": "database_data_type",
        "Cleaning Function": "cleaning_function",
        "Model Enabled": "model_enabled",
    }

    new_dict = {}
    for old_key, value in original_dict.items():
        new_key = key_map.get(old_key, old_key)
        new_dict[new_key] = value

    return new_dict
