import time
import os
import logging
import datetime
import functools
import zipstream
import csv
import json
import copy

import ogrre_data_cleaning.clean as OGRRE_cleaning_functions
from ogrre.internal import storage_api

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
BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME")
ATTRIBUTE_PATH_SEPARATOR = "::"


def split_attribute_identifier(identifier):
    if identifier is None:
        return []
    return [
        part for part in str(identifier).split(ATTRIBUTE_PATH_SEPARATOR) if part != ""
    ]


def combine_attribute_identifier(parent_identifier, key):
    key = "" if key is None else str(key)
    if parent_identifier is None or parent_identifier == "":
        return key

    parent_identifier = str(parent_identifier)
    if key == "":
        return parent_identifier
    if key == parent_identifier or key.startswith(
        f"{parent_identifier}{ATTRIBUTE_PATH_SEPARATOR}"
    ):
        return key
    return f"{parent_identifier}{ATTRIBUTE_PATH_SEPARATOR}{key}"


def relative_attribute_key(identifier, parent_identifier=None):
    identifier = "" if identifier is None else str(identifier)
    if not parent_identifier:
        return identifier

    parent_identifier = str(parent_identifier)
    prefix = f"{parent_identifier}{ATTRIBUTE_PATH_SEPARATOR}"
    if identifier.startswith(prefix):
        return identifier[len(prefix) :].split(ATTRIBUTE_PATH_SEPARATOR, 1)[0]
    return identifier


def get_attribute_identifier(attribute, parent_identifier=None):
    if not isinstance(attribute, dict):
        return parent_identifier

    key = attribute.get("key", "")
    if parent_identifier:
        return combine_attribute_identifier(parent_identifier, key)

    if attribute.get("isSubattribute", False):
        ## 6/25/26: Added parentAttribute, but keeping backwards compatibility
        ## with topLevelAttribute
        parent_attribute = attribute.get("parentAttribute") or attribute.get(
            "topLevelAttribute"
        )
        if parent_attribute:
            return combine_attribute_identifier(parent_attribute, key)

    return key


def normalize_record_attribute_tree(attributes):
    if attributes is None:
        return []

    def normalize_attribute(
        attribute, top_level_attribute=None, parent_identifier=None
    ):
        if not isinstance(attribute, dict):
            return attribute

        attribute_key = attribute.get("key")
        is_subattribute = top_level_attribute is not None
        attribute["isSubattribute"] = is_subattribute
        attribute["subattributes"] = attribute.get("subattributes") or []

        if is_subattribute:
            attribute["topLevelAttribute"] = top_level_attribute
            attribute["parentAttribute"] = parent_identifier

        attribute_identifier = combine_attribute_identifier(
            parent_identifier, attribute_key
        )
        for subattribute in attribute["subattributes"]:
            normalize_attribute(
                subattribute,
                top_level_attribute=top_level_attribute or attribute_key,
                parent_identifier=attribute_identifier,
            )
        return attribute

    for attribute in attributes:
        if not isinstance(attribute, dict):
            continue
        normalize_attribute(
            attribute,
            top_level_attribute=None,
            parent_identifier=None,
        )
    return attributes


def iter_attribute_tree(attributes, parent_identifier=None):
    for attribute in attributes or []:
        if not isinstance(attribute, dict):
            continue
        attribute_identifier = get_attribute_identifier(attribute, parent_identifier)
        yield attribute, attribute_identifier
        yield from iter_attribute_tree(
            attribute.get("subattributes") or [], attribute_identifier
        )


def attribute_index_path_to_mongo_path(primary_index, subattribute_index_path=None):
    path = f"attributesList.{primary_index}"
    for sub_index in subattribute_index_path or []:
        path = f"{path}.subattributes.{sub_index}"
    return path


def normalize_subattribute_index_path(data, fallback_sub_index=None):
    if data is None:
        data = {}

    path = (
        data.get("subattributeIndexPath")
        or data.get("subAttributeIndexPath")
        or data.get("subIndexPath")
    )
    if path is None and fallback_sub_index is not None:
        path = [fallback_sub_index]
    if path is None:
        return []
    if not isinstance(path, list):
        path = [path]

    normalized_path = []
    for each in path:
        if each is None:
            continue
        normalized_path.append(int(each))
    return normalized_path


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


def sortRecordAttributes(
    attributes, processor, keep_all_attributes=False, data_fusion=None
):
    if processor is None:
        _log.info(f"no processor found")
        return normalize_record_attribute_tree(attributes), False
    if attributes is None:
        attributes = []
    processor_attributes = processor.get("attributes", None)
    if processor_attributes is None or len(processor_attributes) == 0:
        _log.info(f"no processor attributes found")
        return normalize_record_attribute_tree(attributes), False
    processor_attributes.sort(key=lambda x: x.get("page_order_sort", float("inf")))

    original_attributes = copy.deepcopy(attributes)
    attributes = normalize_record_attribute_tree(attributes)
    processor_attributes_dict = convert_processor_attributes_to_dict(
        processor_attributes
    )
    processor_attribute_tree = create_processor_attribute_tree(processor_attributes)

    def sort_subattributes(record_attribute, parent_identifier):
        subattributes = record_attribute.get("subattributes") or []
        if len(subattributes) == 0:
            record_attribute["subattributes"] = []
            return

        sorted_subattributes = []
        used_indexes = set()
        child_names = processor_attribute_tree.get(parent_identifier, [])

        for child_name in child_names:
            child_identifier = combine_attribute_identifier(
                parent_identifier, child_name
            )
            for idx, subattribute in enumerate(subattributes):
                if idx in used_indexes:
                    continue
                if (
                    get_attribute_identifier(subattribute, parent_identifier)
                    == child_identifier
                ):
                    processor_attribute_data = processor_attributes_dict.get(
                        child_identifier
                    )
                    if processor_attribute_data:
                        subattribute["alias"] = processor_attribute_data.get("alias")
                    sort_subattributes(subattribute, child_identifier)
                    sorted_subattributes.append(subattribute)
                    used_indexes.add(idx)

        for idx, subattribute in enumerate(subattributes):
            if idx in used_indexes:
                continue
            child_identifier = get_attribute_identifier(subattribute, parent_identifier)
            if child_identifier not in processor_attributes_dict:
                _log.info(
                    f"{child_identifier} was not in processor's attributes. keeping it at the end of the subattributes list"
                )
            sort_subattributes(subattribute, child_identifier)
            sorted_subattributes.append(subattribute)

        record_attribute["subattributes"] = sorted_subattributes

    ## match record attribute to each processor attribute
    sorted_attributes = []
    used_top_level_indexes = set()
    for each in processor_attributes:
        attribute_name = each["name"]

        ## if we are using data_fusion for this record group, only
        ## keep fields that are in the data_fusion list
        if data_fusion and attribute_name not in data_fusion:
            continue
        if ATTRIBUTE_PATH_SEPARATOR in attribute_name:
            continue

        found_indexes = [
            idx
            for idx, item in enumerate(attributes)
            if isinstance(item, dict) and item.get("key") == attribute_name
        ]
        for idx in found_indexes:
            attribute = attributes[idx]
            if attribute is None:
                _log.debug(f"{attribute_name} is None")
                continue

            sort_subattributes(attribute, attribute_name)
            sorted_attributes.append(attribute)
            used_top_level_indexes.add(idx)

        if len(found_indexes) == 0:
            _log.debug(
                f"{attribute_name} was not in record's attributes. adding this to the sorted attributes"
            )
            new_attr = createNewAttribute(key=attribute_name)
            sorted_attributes.append(new_attr)

    obsolete_fields_amt = 0
    ## obsolete fields will get removed automatically.
    for idx, attr in enumerate(attributes):
        if idx in used_top_level_indexes:
            continue
        attribute_name = attr["key"]
        ## if we are using data_fusion for this record group, only
        ## keep fields that are in the data_fusion list
        if data_fusion and attribute_name not in data_fusion:
            continue
        if attribute_name not in processor_attributes_dict:
            if keep_all_attributes:
                _log.info(
                    f"{attribute_name} was not in processor's attributes. adding this to the end of the sorted attributes list"
                )
                sorted_attributes.append(attr)
            else:
                obsolete_fields_amt += 1
                _log.info(
                    f"{attribute_name} was not in processor's attributes. adding this to the end of the sorted attributes list"
                )
    _log.info(f"found {obsolete_fields_amt} obsolete fields.")
    if obsolete_fields_amt >= 10:
        _log.info(f"many obsolete fields found, this is probably a mistake.")
    ## only persist when the stored list is actually different from the sorted list
    requires_db_update = sorted_attributes != original_attributes
    _log.info(f"sorted attributes. requires_db_update: {requires_db_update}")
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


def get_document_image(rg_id, record_id, filename, bucket_name=BUCKET_NAME):
    return storage_api.get_document_image(
        rg_id=rg_id, record_id=record_id, filename=filename, bucket_name=bucket_name
    )


def generate_file_url(path, bucket_name=BUCKET_NAME):
    return storage_api.get_file_url(path, bucket_name=bucket_name)


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
    for blob_name in gcs_paths or []:
        size = storage_api.get_file_size(blob_name, bucket_name=BUCKET_NAME)
        if size is not None:
            total_size += size
        else:
            _log.warning(f"blob has no size info or was not found: {blob_name}")

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
                zs.write(
                    file_path,
                    os.path.basename(file_path),
                    compress_type=zipstream.ZIP_DEFLATED,
                )
            else:
                logg(f"Local file not found, skipping: {file_path}", level="info")

    gcs_paths = generate_gcs_paths(documents)
    i = 0
    not_found_amt = 0
    for gcs_path in gcs_paths:
        i += 1
        # check if blob exists before writing to ZIP
        if not storage_api.file_exists(gcs_path, bucket_name=BUCKET_NAME):
            not_found_amt += 1
            logg(f"image #{i} not found, skipping: {gcs_path}", level="info")
            continue
        arcname = gcs_paths[gcs_path]

        def gcs_yield_chunks(arcname, gcs_path, i):
            logg(f"Starting download #{i}: {gcs_path} -> {arcname}")
            start_file = time.time()
            bytes_read = 0
            for chunk in storage_api.iter_file_bytes(
                gcs_path, bucket_name=BUCKET_NAME, chunk_size=65536
            ):
                bytes_read += len(chunk)
                yield chunk

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

        zs.write_iter(arcname, gcs_yield_chunks(arcname, gcs_path, i))

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

        for attr, _ in iter_attribute_tree(attributes):
            if attr is None:
                continue

            if attr.get("cleaning_error", False):
                hasError = True

            key_name = attr.get("key")
            if key_name in target_keys:
                found_values[key_name] = attr.get("value")

    except Exception as e:
        _log.info(
            f"unable to searchRecordForErrorsAndTargetKeys for document: {document.get('_id')}"
        )
        _log.info(f"e: {e}")

    return hasError, found_values


def convert_processor_list_to_dict(processor_list):
    processor_dict = {}
    if processor_list:
        for each in processor_list:
            key = each["Processor ID"]
            processor_dict[key] = each
    return processor_dict


def iter_processor_attribute_schema(attributes, parent_identifier=None):
    for attr in attributes or []:
        if not isinstance(attr, dict):
            continue

        name = attr.get("name")
        if not name:
            continue

        identifier = combine_attribute_identifier(parent_identifier, name)
        yield identifier, attr
        yield from iter_processor_attribute_schema(
            attr.get("subattributes") or [], identifier
        )


def convert_processor_attributes_to_dict(attributes):
    if not attributes:
        return {}
    attributes_dict = {}
    for identifier, attr in iter_processor_attribute_schema(attributes):
        attributes_dict[identifier] = attr
    return attributes_dict


def create_processor_attribute_tree(attributes):
    attribute_tree = {}
    for identifier, _ in iter_processor_attribute_schema(attributes):
        parts = split_attribute_identifier(identifier)
        if len(parts) == 0:
            continue
        parent_identifier = (
            ATTRIBUTE_PATH_SEPARATOR.join(parts[:-1]) if len(parts) > 1 else None
        )
        attribute_tree.setdefault(parent_identifier, [])
        if parts[-1] not in attribute_tree[parent_identifier]:
            attribute_tree[parent_identifier].append(parts[-1])
    return attribute_tree


def cleanRecordAttribute(processor_attributes, attribute, subattributeKey=None):
    unclean_val = attribute.get("value")
    ## regardless of whether we clean the value, we must update uncleaned value
    _log.info(f"updating uncleaned value to {unclean_val}")
    attribute["uncleaned_value"] = unclean_val
    if not processor_attributes or not isinstance(attribute, dict):
        attribute["cleaned"] = False
        return False

    attribute_key = subattributeKey or get_attribute_identifier(attribute)
    attribute_schema = processor_attributes.get(attribute_key)
    if attribute_schema:
        cleaning_function_name = attribute_schema.get("cleaning_function")
        if cleaning_function_name == "" or cleaning_function_name is None:
            _log.debug(f"cleaning_function for {attribute_key} is empty string or none")
            attribute["cleaned"] = False
            attribute["cleaning_error"] = False
        else:
            cleaning_function = CLEANING_FUNCTIONS.get(cleaning_function_name)
            if cleaning_function:
                try:
                    cleaned_val = cleaning_function(unclean_val)
                    _log.debug(f"CLEANED: {unclean_val} : {cleaned_val}")
                    attribute["value"] = cleaned_val
                    attribute["normalized_value"] = cleaned_val
                    attribute["cleaned"] = True
                    attribute["cleaning_error"] = False
                    attribute["last_cleaned"] = time.time()
                except Exception as e:
                    _log.error(f"unable to clean {attribute_key}: {e}")
                    attribute["cleaning_error"] = f"{e}"
                    attribute["cleaned"] = False
            else:
                _log.info(f"no cleaning function with name: {cleaning_function_name}")

    else:
        _log.info(f"no schema found for {attribute_key}")

    for subattribute in attribute.get("subattributes") or []:
        subattribute_key = get_attribute_identifier(subattribute, attribute_key)
        cleanRecordAttribute(
            processor_attributes, subattribute, subattributeKey=subattribute_key
        )
    return False


def summarize_attribute_for_cleaning(attribute, parent_identifier=None):
    attribute_identifier = get_attribute_identifier(attribute, parent_identifier)
    summary = {
        "key": attribute.get("key"),
        "value": attribute.get("value"),
    }
    if parent_identifier:
        summary["subattribute_identifier"] = attribute_identifier

    subattributes = attribute.get("subattributes") or []
    if subattributes:
        summary["subattributes"] = [
            summarize_attribute_for_cleaning(subattribute, attribute_identifier)
            for subattribute in subattributes
        ]
    return summary


def cleanRecords(processor_attributes, documents):
    # We want to track the before and after values of each cleaned attribute, subattribute
    # To do so, we really only need attr[key] and attr[value] (same with subattributes)
    # for record history
    attributes_list_before_and_after = {}

    for doc in documents:
        attributes_list = normalize_record_attribute_tree(doc["attributesList"])
        current_attributes_list_before_and_after = {
            "attributesList_before": [],
            "attributesList_after": [],
        }
        for attr in attributes_list:
            attribute_before_cleaning = summarize_attribute_for_cleaning(attr)
            cleanRecordAttribute(
                processor_attributes=processor_attributes, attribute=attr
            )
            attribute_after_cleaning = summarize_attribute_for_cleaning(attr)
            current_attributes_list_before_and_after["attributesList_before"].append(
                attribute_before_cleaning
            )
            current_attributes_list_before_and_after["attributesList_after"].append(
                attribute_after_cleaning
            )
        # current_attributes_list_before_and_after["attributesList_after"] = copy.deepcopy(attributes_list)
        attributes_list_before_and_after[
            str(doc.get("_id"))
        ] = current_attributes_list_before_and_after

    return attributes_list_before_and_after


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
        "subattributes": subattributes or [],
        "isSubattribute": False,
        "edited": False,
        "page": page,
    }
    return new_attribute


def defaultJSONDumpHandler(obj):
    if isinstance(obj, datetime.datetime):
        date_string = obj.date().isoformat()
        # _log.info(
        #     f"JSON Dump found datetime object, returning iso format: {date_string}"
        # )
        return date_string
    else:
        # _log.info(f"JSON Dump found Type {type(obj)}. returning string")
        return str(obj)


def generate_mongo_records_pipeline(
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
        subtattributesFields = include_attribute_fields.get("subattributes", [])
        for topLevelField in topLevelFields:
            project["$project"][topLevelField] = 1

        if len(attributesListFields) > 0:
            attributesList_include = {}
            for attributesListField in attributesListFields:
                if (
                    attributesListField == "subattributes"
                    and len(subtattributesFields) > 0
                ):
                    subattributesList_include = {}
                    for subtattributesField in subtattributesFields:
                        subattributesList_include[
                            subtattributesField
                        ] = f"$$subattr.{subtattributesField}"
                    subattributesProject = {
                        "$map": {
                            "input": {"$ifNull": ["$$attr.subattributes", []]},
                            "as": "subattr",
                            "in": subattributesList_include,
                        }
                    }
                    attributesList_include[attributesListField] = subattributesProject
                else:
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
        "Page Order Sort": "page_order_sort",
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


def csv_to_dict(upload_file):
    # upload_file.file is already a file-like object
    upload_file.file.seek(0)  # ensure start
    reader = csv.reader(upload_file.file.read().decode("utf-8").splitlines())
    headers = next(reader)

    data = []
    for row in reader:
        item = dict(zip(headers, row))
        data.append(item)
    return data


def convert_to_target_format(data):
    target_format = []
    key_map = {
        "Name": "name",
        "Database Data Type": "database_data_type",
        "Occurrence": "occurrence",
        "Grouping": "grouping",
        "Page Order Sort": "page_order_sort",
        "Cleaning Function": "cleaning_function",
        # "Data Type" OR "Google DataType" -> "data_type"
    }

    for row in data:
        target_item = {}
        for item_key in row:
            item = row[item_key]
            json_key = key_map.get(item_key, None)
            if json_key:
                target_item[json_key] = item
            else:
                ## TODO: we can add these if we want to, but it might just be a waste of space
                # target_item[item_key] = item
                _log.debug(f"we dont have a matching key for: {item_key}")
        if "Data Type" in row:
            target_item["data_type"] = row["Data Type"]
        elif "Google Data Type" in row:
            target_item["data_type"] = row["Google Data Type"]
        target_format.append(target_item)
    return target_format


def convert_csv_to_dict(csv_file):
    """
    Docstring for convert_csv_to_dict

    :param csv_file: CSV file containing schema fields
        Each field must contain:
         - "Name"
         - "Google Data type"
         - "Database Data Type"
         - "Occurrence"
         - "Grouping"
         - "Page Order Sort"
         - "Cleaning Function"
    """
    data = csv_to_dict(csv_file)
    target_format = convert_to_target_format(data)
    return target_format


def format_schema_json(json_file):
    """
    Docstring for format_schema_json

    :param json file: JSON file containing schema fields
        Each field must contain:
         - "name"
         - "data_type"
         - "database_data_type"
         - "occurrence"
         - "grouping"
         - "page_order_sort"
         - "cleaning_function"
    """
    if hasattr(json_file, "file"):
        json_file.file.seek(0)
        raw = json_file.file.read()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
    elif hasattr(json_file, "read"):
        raw = json_file.read()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
    else:
        raw = json_file

    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("schema json must be a list of objects")

    allowed_keys = {
        "name",
        "data_type",
        "database_data_type",
        "occurrence",
        "grouping",
        "page_order_sort",
        "cleaning_function",
    }
    data_type_aliases = {"google_data_type", "Google Data type"}

    formatted = []
    for item in data:
        if not isinstance(item, dict):
            continue
        new_item = {}
        for key in allowed_keys:
            if key in item:
                new_item[key] = item[key]
        if "data_type" not in new_item:
            for alias in data_type_aliases:
                if alias in item:
                    new_item["data_type"] = item[alias]
                    break
        formatted.append(new_item)
    return formatted


def upload_to_gcs(
    file_bytes: bytes, original_filename: str, processor_name: str
) -> str:
    """
    Uploads raw bytes to Google Cloud Storage and returns the public URL.
    """
    return storage_api.upload_sample_image(
        file_bytes=file_bytes,
        original_filename=original_filename,
        processor_name=processor_name,
    )


def generate_record_group_stats(rg_ids):
    ## pipeline for getting the following record group stats:
    ## total amount, amount reviewed (reviewed or defected), amount containing cleaning errors
    pipeline = [
        {"$match": {"record_group_id": {"$in": rg_ids}}},
        {
            "$group": {
                "_id": "$record_group_id",
                "total_amt": {"$sum": 1},
                "reviewed_amt": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$review_status", ["defective", "reviewed"]]},
                            1,
                            0,
                        ]
                    }
                },
                "error_amt": {
                    "$sum": {
                        "$cond": [
                            {
                                "$or": [
                                    {
                                        "$anyElementTrue": {
                                            "$map": {
                                                "input": {
                                                    "$ifNull": [
                                                        "$attributesList",
                                                        [],
                                                    ]
                                                },
                                                "as": "attr",
                                                "in": {
                                                    "$and": [
                                                        {
                                                            "$ne": [
                                                                "$$attr.cleaning_error",
                                                                False,
                                                            ]
                                                        },
                                                        {
                                                            "$ne": [
                                                                {
                                                                    "$type": "$$attr.cleaning_error"
                                                                },
                                                                "missing",
                                                            ]
                                                        },
                                                    ]
                                                },
                                            }
                                        }
                                    },
                                    {
                                        "$anyElementTrue": {
                                            "$map": {
                                                "input": {
                                                    "$reduce": {
                                                        "input": {
                                                            "$ifNull": [
                                                                "$attributesList",
                                                                [],
                                                            ]
                                                        },
                                                        "initialValue": [],
                                                        "in": {
                                                            "$concatArrays": [
                                                                "$$value",
                                                                {
                                                                    "$ifNull": [
                                                                        "$$this.subattributes",
                                                                        [],
                                                                    ]
                                                                },
                                                            ]
                                                        },
                                                    }
                                                },
                                                "as": "sub",
                                                "in": {
                                                    "$and": [
                                                        {
                                                            "$ne": [
                                                                "$$sub.cleaning_error",
                                                                False,
                                                            ]
                                                        },
                                                        {
                                                            "$ne": [
                                                                {
                                                                    "$type": "$$sub.cleaning_error"
                                                                },
                                                                "missing",
                                                            ]
                                                        },
                                                    ]
                                                },
                                            }
                                        }
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
    ]

    return pipeline


def getPreviousAttributeOrSubattributeValue(key_parts, record_doc):
    try:
        curr = record_doc
        for each in key_parts:
            if isinstance(each, str) and each.isdigit():
                _log.info(f"found int: {each}")
                each = int(each)
                _log.info(f"-> {each}")
            val = curr[each]
            curr = val
        return val
    except Exception as e:
        _log.info(f"exception: {e}")
        return None
