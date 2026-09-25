import time
import os
import logging
import datetime
import functools
import zipstream
import csv
import io
import json
import copy
from pathlib import Path
import re
import importlib.metadata as importlib_metadata

import fitz
from ogrre_data_cleaning import CLEANING_FUNCTIONS
from ogrre.internal import storage_api
from ogrre.internal import schema_validation

_log = logging.getLogger(__name__)
BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME")
ATTRIBUTE_PATH_SEPARATOR = "::"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_PACKAGE_NAME = "orphaned-wells-ui-server"
DATA_CLEANING_PACKAGE_NAME = "ogrre_data_cleaning"
EMBED_PACKAGE_NAME = "ogrre_embed"


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

    return combine_attribute_identifier(None, key)


def normalize_record_attribute_tree(attributes):
    if not isinstance(attributes, list):
        return []
    attributes = [
        attribute
        for attribute in attributes
        if isinstance(attribute, dict)
        and (attribute.get("deleted") or isinstance(attribute.get("key"), str))
    ]

    def normalize_attribute(
        attribute, top_level_attribute=None, parent_identifier=None
    ):
        if not isinstance(attribute, dict) or attribute.get("deleted"):
            return attribute

        attribute_key = attribute.get("key")
        is_subattribute = top_level_attribute is not None
        attribute["isSubattribute"] = is_subattribute
        children = attribute.get("subattributes")
        attribute["subattributes"] = (
            [
                child
                for child in children
                if isinstance(child, dict)
                and (child.get("deleted") or isinstance(child.get("key"), str))
            ]
            if isinstance(children, list)
            else []
        )

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


def iter_attribute_tree(attributes, parent_identifier=None, include_deleted=False):
    if not isinstance(attributes, list):
        return
    for attribute in attributes or []:
        if not isinstance(attribute, dict) or (
            attribute.get("deleted") and not include_deleted
        ):
            continue
        if not isinstance(attribute.get("key"), str):
            continue
        attribute_identifier = get_attribute_identifier(attribute, parent_identifier)
        yield attribute, attribute_identifier
        yield from iter_attribute_tree(
            attribute.get("subattributes") or [], attribute_identifier, include_deleted
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


def sanitize_filename_component(name, default=""):
    """Strip characters that are unsafe to use in a filename/path segment."""
    if not name:
        return default
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip()
    return cleaned or default


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


def active_attributes(attributes):
    """A read/export copy. Never persist this filtered list as the stored record."""
    result = []
    if not isinstance(attributes, list):
        return result
    for attribute in attributes or []:
        if not isinstance(attribute, dict) or attribute.get("deleted"):
            continue
        item = copy.deepcopy(attribute)
        item["subattributes"] = active_attributes(item.get("subattributes"))
        result.append(item)
    return result


# These stored metadata fields do not depend on the visible attribute tree.
# Unknown paths/expressions keep the conservative redact-before-filter path.
RECORD_METADATA_FIELDS = {
    "_id",
    "record_group_id",
    "dateCreated",
    "name",
    "filename",
    "original_filename",
    "record_number",
    "api_number",
    "status",
    "review_status",
    "has_errors",
    "confidence_median",
    "confidence_lowest",
    "defective_categories",
}


def is_record_metadata_filter(query):
    if not isinstance(query, dict):
        return False
    for key, value in query.items():
        if key in {"$and", "$or", "$nor"}:
            if not isinstance(value, list) or not all(
                is_record_metadata_filter(clause) for clause in value
            ):
                return False
        elif key not in RECORD_METADATA_FIELDS:
            return False
    return True


def active_records_pipeline(filter_by):
    # Apply trusted scope before traversing attributes, then all value filters to
    # the visible tree. $redact recursively prunes children of retired parents.
    scope = {
        key: filter_by[key] for key in ("record_group_id", "_id") if key in filter_by
    }
    return [
        {"$match": scope},
        {
            "$redact": {
                "$cond": [
                    {"$eq": [{"$ifNull": ["$deleted", False]}, True]},
                    "$$PRUNE",
                    "$$DESCEND",
                ]
            }
        },
        {"$match": filter_by},
    ]


def preserve_retired_attributes(previous, replacement):
    """Keep retired values through reprocessing and full-list internal writes."""
    previous = previous if isinstance(previous, list) else []
    result = copy.deepcopy(replacement if isinstance(replacement, list) else [])
    # Match each original replacement occurrence once; appended entries cannot match.
    unmatched_retired = [
        item for item in result if isinstance(item, dict) and item.get("deleted")
    ]
    for index, old in enumerate(previous or []):
        if not isinstance(old, dict):
            continue
        if old.get("deleted"):
            try:
                unmatched_retired.remove(old)
            except ValueError:
                result.append(copy.deepcopy(old))
            continue
        retired_children = preserve_retired_attributes(old.get("subattributes"), [])
        if not retired_children:
            continue
        # Pair repeated parents by occurrence, not just key.
        occurrence = sum(
            isinstance(item, dict)
            and item.get("key") == old.get("key")
            and not item.get("deleted")
            for item in (previous or [])[:index]
        )
        matches = [
            item
            for item in result
            if isinstance(item, dict)
            and item.get("key") == old.get("key")
            and not item.get("deleted")
        ]
        if occurrence < len(matches):
            target = matches[occurrence]
            target["subattributes"] = preserve_retired_attributes(
                old.get("subattributes"), target.get("subattributes")
            )
        else:
            # Retain the container too if extraction no longer supplies it.
            container = copy.deepcopy(old)
            container["deleted"] = True
            result.append(container)
    return result


def sortRecordAttributes(
    attributes, processor, keep_all_attributes=False, *, add_missing_attributes=True
):
    original = copy.deepcopy(attributes or [])
    attributes = normalize_record_attribute_tree(copy.deepcopy(original))
    if processor is None or "attributes" not in processor:
        return attributes, attributes != original
    fields = schema_validation.normalize_fields(
        processor.get("attributes") or [], strict=False
    )
    names = [field.get("name") for field in fields]
    if len(names) != len(set(names)):
        raise schema_validation.SchemaError(
            "Duplicate schema fields cannot be used to sort a record.", 409
        )
    fields.sort(key=lambda field: field.get("page_order_sort", float("inf")))
    by_name = {field["name"]: field for field in fields}
    order = {
        name: index for index, name in enumerate(field["name"] for field in fields)
    }

    def reconcile(items, parent=None):
        for item in items:
            if item.get("deleted"):
                continue
            path = get_attribute_identifier(item, parent)
            definition = by_name.get(path)
            if (definition and definition.get("deleted")) or (
                definition is None and not keep_all_attributes
            ):
                item["deleted"] = True
                continue
            if definition:
                item["alias"] = definition.get("alias")
            item["subattributes"] = reconcile(item.get("subattributes") or [], path)
        return sorted(
            items,
            key=lambda item: (
                bool(item.get("deleted")),
                order.get(get_attribute_identifier(item, parent), len(order)),
            ),
        )

    attributes = reconcile(attributes)
    existing_keys = {
        item.get("key") for item in attributes if isinstance(item.get("key"), str)
    }
    for field in fields if add_missing_attributes else []:
        name = field["name"]
        # Re-adding a definition never restores an old retired value or creates
        # an active placeholder over it. Restoration is a separate workflow.
        if "::" not in name and not field.get("deleted") and name not in existing_keys:
            attributes.append(createNewAttribute(key=name))
    attributes = reconcile(attributes)
    return attributes, attributes != original


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
        return {}
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
    gcs_sizes, missing_count = storage_api.get_file_sizes(
        gcs_paths, bucket_name=BUCKET_NAME
    )
    total_size += sum(gcs_sizes.values())
    if missing_count:
        _log.warning(
            "Skipped %s missing or unreadable blob(s) while computing download size",
            missing_count,
        )

    return total_size


def compile_embedded_pdfs(records):
    """
    Generates embedded searchable PDF bytes for each record in-memory using ogrre_embed.
    Returns a list of tuples: [(arcname, pdf_bytes), ...]
    """
    try:
        from ogrre_embed import make_pdf_searchable
    except ImportError:
        _log.error(
            "ogrre_embed package is not installed; cannot generate embedded PDFs."
        )
        return []

    embedded_pdfs = []
    for record in records or []:
        rg_id = record.get("record_group_id")
        record_id = str(record.get("_id", ""))
        if not rg_id or not record_id:
            continue
        record_name = record.get("name") or record.get("filename") or record_id
        if record_name.lower().endswith(".pdf"):
            record_name = record_name[:-4]

        image_files = record.get("image_files") or []
        if not image_files:
            continue

        doc = fitz.open()
        pages_added = 0
        for img_name in image_files:
            if not imageIsValid(img_name):
                continue
            blob_key = f"uploads/{rg_id}/{record_id}/{img_name}"
            try:
                img_bytes = storage_api.download_file_bytes(blob_key)
                if not img_bytes:
                    continue
                img_doc = fitz.open(stream=img_bytes, filetype="png")
                pdf_bytes = img_doc.convert_to_pdf()
                img_pdf = fitz.open("pdf", pdf_bytes)
                doc.insert_pdf(img_pdf)
                img_doc.close()
                img_pdf.close()
                pages_added += 1
            except Exception as e:
                _log.warning(
                    f"Unable to add image {blob_key} to PDF for embedded pdf: {e}"
                )

        if pages_added == 0:
            doc.close()
            continue

        try:
            pdf_bytes = make_pdf_searchable(doc, record)
            arcname = f"documents/{record_name}/{record_name}_searchable.pdf"
            embedded_pdfs.append((arcname, pdf_bytes))
        except Exception as e:
            _log.error(f"Failed to generate searchable PDF for record {record_id}: {e}")
            doc.close()

    return embedded_pdfs


@time_it
def zip_files_stream(
    local_file_paths, documents=[], log_to_file="zip_log.txt", embedded_pdfs=[]
):
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
    if embedded_pdfs is None:
        embedded_pdfs = []
    logg(
        f"Downloading and zipping {len(documents)} images, {len(embedded_pdfs)} embedded PDFs along with {local_file_paths}",
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

    if embedded_pdfs:
        for arcname, pdf_bytes in embedded_pdfs:
            zs.write_iter(arcname, [pdf_bytes])

    gcs_paths = generate_gcs_paths(documents)
    gcs_sizes, missing_count = storage_api.get_file_sizes(
        gcs_paths.keys(), bucket_name=BUCKET_NAME
    )
    available_gcs_paths = set(gcs_sizes)
    not_found_amt = missing_count
    if missing_count:
        logg(f"{missing_count} image(s) not found, skipping", level="info")

    last_existing_index = max(
        (
            index
            for index, gcs_path in enumerate(gcs_paths, start=1)
            if gcs_path in available_gcs_paths
        ),
        default=0,
    )
    for i, gcs_path in enumerate(gcs_paths, start=1):
        if gcs_path not in available_gcs_paths:
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
            if i == last_existing_index:
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
        if not isinstance(attr, dict) or attr.get("deleted"):
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


def cleanRecordAttribute(
    processor_attributes,
    attribute,
    subattributeKey=None,
    options=None,
):
    if options is None:
        options = {}

    if not isinstance(attribute, dict) or attribute.get("deleted"):
        return False

    attribute_key = subattributeKey or get_attribute_identifier(attribute)
    attribute_schema = (processor_attributes or {}).get(attribute_key)
    if attribute_schema and attribute_schema.get("deleted"):
        return False

    unclean_val = attribute.get("value")
    ## regardless of whether we clean the value, we must update uncleaned value
    _log.info(f"updating uncleaned value to {unclean_val}")
    attribute["uncleaned_value"] = unclean_val

    raw_key = attribute.get("key")

    target_key = None
    if raw_key in ["lease_field", "lease_operator"]:
        target_key = raw_key
    elif attribute_key in ["lease_field", "lease_operator"]:
        target_key = attribute_key

    if target_key and unclean_val is not None and unclean_val != "":
        options[target_key] = unclean_val

    if not processor_attributes:
        attribute["cleaned"] = False
        return False
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
                    options_context = dict(options)
                    options_context["attribute_key"] = target_key or attribute_key
                    cleaned_val = cleaning_function(unclean_val, options=options_context)
                    _log.debug(f"CLEANED: {unclean_val} : {cleaned_val}")
                    attribute["value"] = cleaned_val
                    attribute["normalized_value"] = cleaned_val
                    attribute["cleaned"] = True
                    attribute["cleaning_error"] = False
                    attribute["last_cleaned"] = time.time()

                    if target_key and cleaned_val is not None and cleaned_val != "":
                        options[target_key] = cleaned_val
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
            processor_attributes,
            subattribute,
            subattributeKey=subattribute_key,
            options=options,
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
            if not subattribute.get("deleted")
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
        options = {}
        for attr in attributes_list:
            if attr.get("deleted"):
                continue
            attribute_before_cleaning = summarize_attribute_for_cleaning(attr)
            cleanRecordAttribute(
                processor_attributes=processor_attributes,
                attribute=attr,
                options=options,
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
    active_pipeline = active_records_pipeline(filter_by)
    defer_redaction = (
        primary_sort[0] in RECORD_METADATA_FIELDS
        and secondary_sort is None
        and not include_attribute_fields
        and is_record_metadata_filter(filter_by)
    )
    if defer_redaction:
        # Keep match/sort/window/pagination together so Mongo can use the sort
        # index. Redacting first also makes $setWindowFields introduce a blocking
        # sort. Exclude retired root documents before ranking/paging as before.
        pipeline = [{"$match": {"$and": [filter_by, {"deleted": {"$ne": True}}]}}]
    else:
        pipeline = active_pipeline

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
                            "input": mongo_attribute_array("$$attr.subattributes"),
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
                    "input": mongo_attribute_array("$attributesList"),
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
                                                "input": mongo_attribute_array(
                                                    "$attributesList"
                                                ),
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
                                            "input": {
                                                "$convert": {
                                                    "input": "$targetValue",
                                                    "to": "string",
                                                    "onError": "",
                                                    "onNull": "",
                                                }
                                            },
                                            # "input": "$targetValue",
                                            "regex": "\\d+",
                                        }
                                    }
                                },
                                "in": {
                                    "$cond": [
                                        {"$ne": ["$$match", None]},
                                        {
                                            "$convert": {
                                                "input": "$$match.match",
                                                "to": "double",
                                                "onError": None,
                                                "onNull": None,
                                            }
                                        },
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

    if defer_redaction:
        pipeline.append(active_pipeline[1])

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
    upload_file.file.seek(0)
    reader = csv.DictReader(
        io.StringIO(upload_file.file.read().decode("utf-8-sig")), strict=True
    )
    try:
        headers = reader.fieldnames
        if not headers or any(not name.strip() for name in headers):
            raise schema_validation.SchemaError(
                "The CSV schema must contain column headers."
            )
        if len(set(headers)) != len(headers):
            raise schema_validation.SchemaError(
                "The CSV schema contains duplicate column headers."
            )
        data = list(reader)
    except csv.Error as error:
        raise schema_validation.SchemaError(f"Invalid CSV schema: {error}.") from error
    if any(None in row or None in row.values() for row in data):
        raise schema_validation.SchemaError(
            "Every CSV row must match the column headers."
        )
    return data


def convert_to_target_format(data):
    return schema_validation.normalize_fields(data)


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

    return schema_validation.normalize_fields(json.loads(raw))


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


def mongo_attribute_array(value):
    """Ignore malformed containers and entries in read-only Mongo expressions."""
    return {
        "$filter": {
            "input": {"$cond": [{"$isArray": value}, value, []]},
            "as": "entry",
            "cond": {
                "$and": [
                    {"$eq": [{"$type": "$$entry"}, "object"]},
                    {"$ne": [{"$ifNull": ["$$entry.deleted", False]}, True]},
                ]
            },
        }
    }


def record_error_expression():
    # Best-effort statistics, bounded to the same 12 levels as schema inference.
    # Stop traversing once an error is found or there are no children left.
    return {
        "$let": {
            "vars": {
                "scan": {
                    "$reduce": {
                        "input": {"$range": [0, 12]},
                        "initialValue": {
                            "attributes": mongo_attribute_array("$attributesList"),
                            "has_errors": False,
                        },
                        "in": {
                            "$cond": [
                                {
                                    "$or": [
                                        "$$value.has_errors",
                                        {"$eq": [{"$size": "$$value.attributes"}, 0]},
                                    ]
                                },
                                "$$value",
                                {
                                    "has_errors": {
                                        "$anyElementTrue": [
                                            {
                                                "$map": {
                                                    "input": "$$value.attributes",
                                                    "as": "attribute",
                                                    "in": {
                                                        "$not": [
                                                            {
                                                                "$in": [
                                                                    {
                                                                        "$ifNull": [
                                                                            "$$attribute.cleaning_error",
                                                                            False,
                                                                        ]
                                                                    },
                                                                    [
                                                                        False,
                                                                        None,
                                                                        "",
                                                                        0,
                                                                        [],
                                                                        {},
                                                                    ],
                                                                ]
                                                            }
                                                        ]
                                                    },
                                                }
                                            }
                                        ]
                                    },
                                    "attributes": {
                                        "$reduce": {
                                            "input": "$$value.attributes",
                                            "initialValue": [],
                                            "in": {
                                                "$concatArrays": [
                                                    "$$value",
                                                    mongo_attribute_array(
                                                        "$$this.subattributes"
                                                    ),
                                                ]
                                            },
                                        }
                                    },
                                },
                            ]
                        },
                    }
                }
            },
            "in": "$$scan.has_errors",
        }
    }


def generate_record_group_stats(rg_ids):
    return [
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
                "error_amt": {"$sum": {"$cond": [record_error_expression(), 1, 0]}},
            }
        },
    ]


def getPreviousAttributeOrSubattributeValue(key_parts, record_doc):
    try:
        curr = record_doc
        for each in key_parts:
            if isinstance(each, str) and each.isdigit():
                each = int(each)
                _log.info(f"-> {each}")
            val = curr[each]
            curr = val
        return val
    except Exception as e:
        _log.info(f"exception: {e}")
        return None


def _get_distribution_metadata(package_name: str) -> dict:
    package_info = {"name": package_name}
    try:
        package_info["version"] = importlib_metadata.version(package_name)
    except importlib_metadata.PackageNotFoundError:
        pass

    try:
        distribution = importlib_metadata.distribution(package_name)
        direct_url_text = distribution.read_text("direct_url.json")
    except importlib_metadata.PackageNotFoundError:
        direct_url_text = None

    if direct_url_text:
        try:
            direct_url = json.loads(direct_url_text)
            source_url = direct_url.get("url")
            if source_url:
                package_info["source_url"] = source_url

            vcs_info = direct_url.get("vcs_info") or {}
            commit = vcs_info.get("commit_id") or vcs_info.get("requested_revision")
            if commit:
                package_info["commit"] = commit
            if vcs_info.get("requested_revision"):
                package_info["requested_revision"] = vcs_info["requested_revision"]
        except (TypeError, json.JSONDecodeError) as e:
            _log.debug(
                "unable to parse direct_url metadata for %s: %s", package_name, e
            )

    return package_info


def _get_requirement_metadata(package_name: str) -> dict:
    requirement_info = {}
    requirements_path = PROJECT_ROOT / "requirements.txt"
    normalized_package_name = package_name.lower().replace("-", "_")

    try:
        requirement_lines = requirements_path.read_text().splitlines()
    except FileNotFoundError:
        return requirement_info

    for line in requirement_lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        normalized_line = line.lower().replace("-", "_")
        if not normalized_line.startswith(normalized_package_name):
            continue

        requirement_info["requirement"] = line
        if " @ git+" in line:
            requirement_url = line.split("@ git+", 1)[1]
            source_url, commit = requirement_url.rsplit("@", 1)
            requirement_info["source_url"] = source_url
            requirement_info["commit"] = commit.split("#", 1)[0]
        elif "==" in line:
            requirement_info["version"] = line.split("==", 1)[1]
        break

    return requirement_info


def _get_setup_py_metadata() -> dict:
    setup_info = {}
    setup_path = PROJECT_ROOT / "setup.py"
    try:
        setup_text = setup_path.read_text()
    except FileNotFoundError:
        return setup_info

    version_match = re.search(r"version\s*=\s*[\"']([^\"']+)[\"']", setup_text)
    if version_match:
        setup_info["version"] = version_match.group(1)

    url_match = re.search(r"url\s*=\s*[\"']([^\"']+)[\"']", setup_text)
    if url_match:
        setup_info["source_url"] = url_match.group(1)

    return setup_info


def _get_deployment_metadata() -> dict:
    deployment_info = {
        "image": os.getenv("OGRRE_BACKEND_IMAGE"),
        "deploy_run_id": os.getenv("OGRRE_BACKEND_DEPLOY_RUN_ID"),
        "deployed_at": os.getenv("OGRRE_BACKEND_DEPLOYED_AT"),
    }
    return {
        key: value
        for key, value in deployment_info.items()
        if value is not None and value != ""
    }


def build_ogrre_version_info() -> dict:
    data_cleaning_info = _get_distribution_metadata(DATA_CLEANING_PACKAGE_NAME)
    for key, value in _get_requirement_metadata(DATA_CLEANING_PACKAGE_NAME).items():
        data_cleaning_info.setdefault(key, value)

    embed_info = _get_distribution_metadata(EMBED_PACKAGE_NAME)
    for key, value in _get_requirement_metadata(EMBED_PACKAGE_NAME).items():
        embed_info.setdefault(key, value)

    backend_info = _get_distribution_metadata(BACKEND_PACKAGE_NAME)
    for key, value in _get_setup_py_metadata().items():
        backend_info.setdefault(key, value)

    return {
        "packages": [
            data_cleaning_info,
            embed_info,
            backend_info,
        ],
        "deployment": _get_deployment_metadata(),
    }
