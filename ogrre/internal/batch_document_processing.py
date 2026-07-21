import io
import copy
from collections import Counter
import logging
import mimetypes
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import NamedTuple

import fitz
from google.cloud import documentai
from google.cloud.documentai_toolbox.utilities import gcs_utilities
from PIL import Image

from ogrre.internal import document_ai_api
from ogrre.internal import storage_api
from ogrre.internal import util
from ogrre.internal.whitespace_detector import detect_whitespace_from_bytes

_log = logging.getLogger(__name__)

BATCH_SIZE = 1000
MAX_CONCURRENT_BATCH_LROS = 5
BATCH_LRO_TIMEOUT = int(os.getenv("DOCUMENT_AI_BATCH_TIMEOUT_SECONDS", "7200"))
BATCH_OUTPUT_PREFIX = os.getenv(
    "DOCUMENT_AI_BATCH_OUTPUT_PREFIX", "document_ai_batch_outputs"
)
DETECT_WHITESPACE = os.getenv("DETECT_WHITESPACE", "true").lower() in (
    "1",
    "true",
    "yes",
)
MONITORED_DUPLICATE_FIELD_KEYS = {
    field.strip().lower()
    for field in os.getenv(
        "DOCUMENT_AI_BATCH_DUPLICATE_FIELD_KEYS",
        "project_name,client_name,client_address",
    ).split(",")
    if field.strip()
}
LOG_BLOB_NAME_SAMPLE_LIMIT = int(
    os.getenv("DOCUMENT_AI_BATCH_LOG_BLOB_NAME_SAMPLE_LIMIT", "20")
)

_batch_jobs = {}
_batch_jobs_lock = threading.Lock()


class PreparedDocument(NamedTuple):
    source_uri: str
    record_id: str
    file_name: str
    gcs_document: documentai.GcsDocument


def _new_summary():
    return {
        "total_submitted": 0,
        "total_succeeded": 0,
        "total_failed": 0,
        "failed_document_uris": [],
    }


def _target_attribute_counts(attributes):
    counts = Counter()
    if not MONITORED_DUPLICATE_FIELD_KEYS:
        return counts

    for attribute in attributes or []:
        if not isinstance(attribute, dict):
            continue
        key = attribute.get("key")
        normalized_key = str(key).lower()
        if normalized_key not in MONITORED_DUPLICATE_FIELD_KEYS:
            continue
        counts[normalized_key] += 1
    return counts


def _target_attribute_duplicates(attributes):
    return {
        key: count
        for key, count in _target_attribute_counts(attributes).items()
        if count > 1
    }


def _sample_values(values, limit):
    values = list(values)
    if len(values) <= limit:
        return values
    return values[:limit] + [f"... {len(values) - limit} more"]


def _sample_blob_names(blobs):
    return _sample_values((blob.name for blob in blobs), LOG_BLOB_NAME_SAMPLE_LIMIT)


def create_batch_document_job(
    rg_id,
    user_info,
    bucket_name,
    prefix="",
    output_bucket_name=None,
    output_prefix=None,
):
    job_id = uuid.uuid4().hex
    now = time.time()
    with _batch_jobs_lock:
        _batch_jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "record_group_id": rg_id,
            "bucket_name": bucket_name,
            "prefix": prefix or "",
            "output_bucket_name": output_bucket_name or bucket_name,
            "output_prefix": output_prefix,
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "batches_total": 0,
            "batches_completed": 0,
            "summary": _new_summary(),
            "error": None,
            "user_email": user_info.get("email"),
        }
    return job_id


def get_batch_document_job(job_id):
    with _batch_jobs_lock:
        job = _batch_jobs.get(job_id)
        if job is None:
            return None
        job_copy = dict(job)
        job_copy["summary"] = dict(job["summary"])
        job_copy["summary"]["failed_document_uris"] = list(
            job["summary"]["failed_document_uris"]
        )
        return job_copy


def get_gcs_path_document_summary(bucket_name, prefix=""):
    normalized_prefix = _normalize_prefix(prefix)
    batches = gcs_utilities.create_batches(
        gcs_bucket_name=bucket_name, gcs_prefix=normalized_prefix, batch_size=BATCH_SIZE
    )
    total_documents = sum(len(_get_gcs_documents(batch)) for batch in batches)
    total_batches = len(batches)
    total_lro_waves = (
        total_batches + MAX_CONCURRENT_BATCH_LROS - 1
    ) // MAX_CONCURRENT_BATCH_LROS
    return {
        "bucketName": bucket_name,
        "prefix": prefix or "",
        "normalizedPrefix": normalized_prefix,
        "totalFiles": total_documents,
        "totalBatches": total_batches,
        "totalLroWaves": total_lro_waves,
    }


def _set_job_fields(job_id, **fields):
    with _batch_jobs_lock:
        job = _batch_jobs.get(job_id)
        if job is None:
            return
        job.update(fields)
        job["updated_at"] = time.time()


def _increment_job_summary(
    job_id,
    total_submitted=0,
    total_succeeded=0,
    total_failed=0,
    failed_document_uris=None,
):
    failed_document_uris = failed_document_uris or []
    with _batch_jobs_lock:
        job = _batch_jobs.get(job_id)
        if job is None:
            return
        summary = job["summary"]
        summary["total_submitted"] += total_submitted
        summary["total_succeeded"] += total_succeeded
        summary["total_failed"] += total_failed
        summary["failed_document_uris"].extend(failed_document_uris)
        job["updated_at"] = time.time()


def _increment_batches_completed(job_id):
    with _batch_jobs_lock:
        job = _batch_jobs.get(job_id)
        if job is None:
            return
        job["batches_completed"] += 1
        job["updated_at"] = time.time()


def process_batch_document_job(
    job_id,
    rg_id,
    user_info,
    data_manager,
    bucket_name,
    prefix="",
    output_bucket_name=None,
    output_prefix=None,
    run_cleaning_functions=True,
):
    _set_job_fields(job_id, status="running", started_at=time.time(), error=None)
    try:
        _process_batch_documents(
            job_id=job_id,
            rg_id=rg_id,
            user_info=user_info,
            data_manager=data_manager,
            bucket_name=bucket_name,
            prefix=prefix,
            output_bucket_name=output_bucket_name or bucket_name,
            output_prefix=output_prefix,
            run_cleaning_functions=run_cleaning_functions,
        )
        job = get_batch_document_job(job_id)
        summary = job["summary"]
        status = "completed"
        if summary["total_failed"] > 0:
            status = "completed_with_errors"
        _set_job_fields(job_id, status=status, completed_at=time.time())
    except Exception as e:
        _log.exception("batch document processing job failed")
        _set_job_fields(job_id, status="error", completed_at=time.time(), error=str(e))


def _process_batch_documents(
    job_id,
    rg_id,
    user_info,
    data_manager,
    bucket_name,
    prefix="",
    output_bucket_name=None,
    output_prefix=None,
    run_cleaning_functions=True,
):
    if document_ai_api.DOCUMENT_AI_BACKEND != "google":
        raise ValueError("Batch Document AI processing requires the google backend")

    prefix = _normalize_prefix(prefix)
    batches = gcs_utilities.create_batches(
        gcs_bucket_name=bucket_name, gcs_prefix=prefix, batch_size=BATCH_SIZE
    )
    total_documents = sum(len(_get_gcs_documents(batch)) for batch in batches)
    _set_job_fields(job_id, batches_total=len(batches))
    _increment_job_summary(job_id, total_submitted=total_documents)
    _log.info(
        "batch document job started job_id=%s rg_id=%s bucket=%s prefix=%s "
        "output_bucket=%s output_prefix=%s batches=%s documents=%s",
        job_id,
        rg_id,
        bucket_name,
        prefix,
        output_bucket_name,
        output_prefix,
        len(batches),
        total_documents,
    )

    if total_documents == 0:
        _log.info(f"no batch documents found in gs://{bucket_name}/{prefix}")
        return

    (
        processor_id,
        model_id,
        processor_attributes,
    ) = data_manager.getProcessorByRecordGroupID(rg_id)
    if not processor_id or not model_id:
        raise ValueError(f"unable to find processor for record group {rg_id}")

    output_prefix = _normalize_output_prefix(output_prefix, rg_id, job_id)
    for wave_start in range(0, len(batches), MAX_CONCURRENT_BATCH_LROS):
        wave = batches[wave_start : wave_start + MAX_CONCURRENT_BATCH_LROS]
        with ThreadPoolExecutor(max_workers=len(wave)) as executor:
            future_to_batch = {}
            for offset, batch in enumerate(wave):
                batch_index = wave_start + offset
                future = executor.submit(
                    _process_one_batch,
                    job_id=job_id,
                    batch_index=batch_index,
                    input_config=batch,
                    rg_id=rg_id,
                    user_info=user_info,
                    data_manager=data_manager,
                    processor_id=processor_id,
                    model_id=model_id,
                    processor_attributes=processor_attributes,
                    output_bucket_name=output_bucket_name,
                    output_prefix=output_prefix,
                    run_cleaning_functions=run_cleaning_functions,
                )
                future_to_batch[future] = batch
            for future in as_completed(future_to_batch):
                try:
                    partial_summary = future.result()
                except Exception as e:
                    _log.exception(f"batch worker failed unexpectedly: {e}")
                    documents = _get_gcs_documents(future_to_batch[future])
                    partial_summary = {
                        "total_succeeded": 0,
                        "total_failed": len(documents),
                        "failed_document_uris": [
                            document.gcs_uri for document in documents
                        ],
                    }
                _increment_job_summary(job_id, **partial_summary)
                _increment_batches_completed(job_id)


def _process_one_batch(
    job_id,
    batch_index,
    input_config,
    rg_id,
    user_info,
    data_manager,
    processor_id,
    model_id,
    processor_attributes,
    output_bucket_name,
    output_prefix,
    run_cleaning_functions=True,
):
    partial_summary = _new_summary()
    partial_summary.pop("total_submitted")

    prepared_documents = []
    for gcs_document in _get_gcs_documents(input_config):
        try:
            prepared_documents.append(
                _prepare_document_for_batch(
                    gcs_document=gcs_document,
                    rg_id=rg_id,
                    user_info=user_info,
                    data_manager=data_manager,
                )
            )
        except Exception as e:
            _log.exception(
                "unable to prepare batch document job_id=%s batch_index=%s "
                "source_uri=%s",
                job_id,
                batch_index,
                gcs_document.gcs_uri,
            )
            partial_summary["total_failed"] += 1
            partial_summary["failed_document_uris"].append(gcs_document.gcs_uri)

    if not prepared_documents:
        return partial_summary

    filtered_input_config = documentai.BatchDocumentsInputConfig(
        gcs_documents=documentai.GcsDocuments(
            documents=[prepared.gcs_document for prepared in prepared_documents]
        )
    )
    output_gcs_uri = _build_output_gcs_uri(
        output_bucket_name, output_prefix, batch_index
    )

    try:
        operation = document_ai_api.batch_process_documents(
            input_documents=filtered_input_config,
            output_gcs_uri=output_gcs_uri,
            processor_id=processor_id,
            model_id=model_id,
        )
        operation.result(timeout=BATCH_LRO_TIMEOUT)
        metadata = operation.metadata
    except Exception as e:
        message = f"batch LRO failed for batch {batch_index}: {e}"
        _log.exception(message)
        for prepared in prepared_documents:
            _mark_record_error(
                data_manager,
                rg_id,
                prepared.record_id,
                prepared.file_name,
                message,
            )
            partial_summary["failed_document_uris"].append(prepared.source_uri)
        partial_summary["total_failed"] += len(prepared_documents)
        return partial_summary

    if metadata.state != documentai.BatchProcessMetadata.State.SUCCEEDED:
        message = f"batch LRO ended in state {metadata.state}: {metadata.state_message}"
        _log.error(message)
        for prepared in prepared_documents:
            _mark_record_error(
                data_manager,
                rg_id,
                prepared.record_id,
                prepared.file_name,
                message,
            )
            partial_summary["failed_document_uris"].append(prepared.source_uri)
        partial_summary["total_failed"] += len(prepared_documents)
        return partial_summary

    status_by_source = {
        status.input_gcs_source: status
        for status in metadata.individual_process_statuses
    }
    output_destination_counts = Counter(
        status.output_gcs_destination
        for status in metadata.individual_process_statuses
        if status.output_gcs_destination
    )
    duplicate_destinations = {
        destination: count
        for destination, count in output_destination_counts.items()
        if count > 1
    }
    if duplicate_destinations:
        _log.warning(
            "batch Document AI reported shared output destinations job_id=%s "
            "batch_index=%s destinations=%s",
            job_id,
            batch_index,
            duplicate_destinations,
        )

    for prepared in prepared_documents:
        process_status = status_by_source.get(prepared.source_uri)
        if process_status is None:
            message = "Document AI batch result missing individual process status"
            _log.error(f"{message}: {prepared.source_uri}")
            _mark_failed_document(
                partial_summary, prepared, data_manager, rg_id, message
            )
            continue

        if process_status.status.code != 0:
            message = process_status.status.message or "Document AI document failed"
            _log.error(f"Document AI failed for {prepared.source_uri}: {message}")
            _mark_failed_document(
                partial_summary, prepared, data_manager, rg_id, message
            )
            continue

        if not process_status.output_gcs_destination:
            message = "Document AI status missing output_gcs_destination"
            _log.error(f"{message}: {prepared.source_uri}")
            _mark_failed_document(
                partial_summary, prepared, data_manager, rg_id, message
            )
            continue

        try:
            attributes_list = _read_output_attributes(
                process_status.output_gcs_destination,
                using_default_processor=data_manager.using_default_processor,
                job_id=job_id,
                batch_index=batch_index,
                record_id=prepared.record_id,
                source_uri=prepared.source_uri,
            )
            _update_record_with_attributes(
                data_manager=data_manager,
                rg_id=rg_id,
                record_id=prepared.record_id,
                file_name=prepared.file_name,
                processor_attributes=processor_attributes,
                attributes_list=attributes_list,
                run_cleaning_functions=run_cleaning_functions,
            )
            partial_summary["total_succeeded"] += 1
        except Exception as e:
            message = f"unable to handle Document AI output: {e}"
            _log.exception(f"{message}: {prepared.source_uri}")
            _mark_failed_document(
                partial_summary, prepared, data_manager, rg_id, message
            )

    return partial_summary


def _mark_failed_document(partial_summary, prepared, data_manager, rg_id, message):
    _mark_record_error(
        data_manager,
        rg_id,
        prepared.record_id,
        prepared.file_name,
        message,
    )
    partial_summary["total_failed"] += 1
    partial_summary["failed_document_uris"].append(prepared.source_uri)


def _prepare_document_for_batch(gcs_document, rg_id, user_info, data_manager):
    source_uri = gcs_document.gcs_uri
    location = storage_api.parse_gcs_url(source_uri)
    source_filename = os.path.basename(location.blob_path)
    filename, file_ext = os.path.splitext(source_filename)
    mime_type = gcs_document.mime_type or mimetypes.guess_type(source_filename)[0]
    record_id = None

    try:
        source_bytes = storage_api.download_file_bytes(
            location.blob_path, bucket_name=location.bucket
        )
        png_files = _convert_document_to_png_files(
            source_bytes=source_bytes,
            filename=filename,
            file_ext=file_ext,
            mime_type=mime_type,
        )
        image_file_names = [file_name for file_name, _ in png_files]
        file_name = image_file_names[0]
        new_record = {
            "record_group_id": rg_id,
            "name": filename,
            "filename": file_name,
            "api_number": _parse_api_number(filename),
            "contributor": user_info,
            "status": "processing",
            "review_status": "unreviewed",
            "original_filename": source_filename,
            "image_files": image_file_names,
        }
        record_id = data_manager.createRecord(new_record, user_info)
        _upload_png_files(rg_id, record_id, png_files)
        if DETECT_WHITESPACE:
            _update_whitespace_results(data_manager, record_id, png_files)
        return PreparedDocument(
            source_uri=source_uri,
            record_id=record_id,
            file_name=file_name,
            gcs_document=gcs_document,
        )
    except Exception as e:
        if record_id is not None:
            _mark_record_error(
                data_manager,
                rg_id,
                record_id,
                f"{filename}.png",
                str(e),
            )
        raise


def _convert_document_to_png_files(source_bytes, filename, file_ext, mime_type):
    ext = file_ext.lower()
    if mime_type == "application/pdf" or ext == ".pdf":
        return _convert_pdf_bytes_to_png_files(source_bytes, filename)

    if mime_type == "image/png" or ext == ".png":
        return [(f"{filename}.png", source_bytes)]

    return [(f"{filename}.png", _convert_image_bytes_to_png(source_bytes))]


def _convert_pdf_bytes_to_png_files(source_bytes, filename):
    doc = fitz.open(stream=source_bytes, filetype="pdf")
    try:
        output_files = []
        dpi = 100
        mat = fitz.Matrix(4, 4)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=mat, dpi=dpi)
            output_filename = f"{filename}.png"
            if i > 0:
                output_filename = f"{filename}_{i + 1}.png"
            output_files.append((output_filename, pix.tobytes("png")))
        if not output_files:
            raise ValueError("PDF contains no pages")
        return output_files
    finally:
        doc.close()


def _convert_image_bytes_to_png(source_bytes):
    image = Image.open(io.BytesIO(source_bytes))
    try:
        image.load()
        output_image = image
        if image.mode not in ("RGB", "RGBA", "L"):
            output_image = image.convert("RGB")
        output = io.BytesIO()
        output_image.save(output, format="PNG")
        return output.getvalue()
    finally:
        if "output_image" in locals() and output_image is not image:
            output_image.close()
        image.close()


def _upload_png_files(rg_id, record_id, png_files):
    _, bucket = storage_api._get_bucket(bucket_name=storage_api.BUCKET_NAME)
    for file_name, file_bytes in png_files:
        blob = bucket.blob(f"uploads/{rg_id}/{record_id}/{file_name}")
        blob.upload_from_string(file_bytes, content_type="image/png")


def _update_whitespace_results(data_manager, record_id, png_files):
    whitespace_results = []
    for _, file_bytes in png_files:
        try:
            result = detect_whitespace_from_bytes(file_bytes, min_whitespace_pct=99.99)
            whitespace_results.append(
                {
                    "is_mostly_whitespace": result.get("meets_threshold"),
                    "whitespace_pct": result.get("whitespace_pct"),
                    "threshold": result.get("threshold"),
                    "total_pixels": result.get("total_pixels"),
                    "white_pixels": result.get("white_pixels"),
                    "error": None,
                }
            )
        except Exception as e:
            whitespace_results.append(
                {
                    "is_mostly_whitespace": None,
                    "whitespace_pct": None,
                    "threshold": None,
                    "total_pixels": None,
                    "white_pixels": None,
                    "error": str(e),
                }
            )
    data_manager.updateRecordInternal(record_id, "image_whitespace", whitespace_results)


def _read_output_attributes(
    output_gcs_destination,
    using_default_processor=False,
    job_id=None,
    batch_index=None,
    record_id=None,
    source_uri=None,
):
    location = storage_api.parse_gcs_url(output_gcs_destination)
    _, bucket = storage_api._get_bucket(bucket_name=location.bucket)
    attributes_list = []
    raw_json_blobs = [
        blob
        for blob in bucket.list_blobs(prefix=location.blob_path)
        if _is_json_blob(blob)
    ]
    json_blobs = [
        blob
        for blob in raw_json_blobs
        if _is_blob_inside_output_destination(blob.name, location.blob_path)
    ]
    json_blobs.sort(key=lambda blob: blob.name)
    selected_blob_names = {blob.name for blob in json_blobs}
    ignored_blobs = [
        blob for blob in raw_json_blobs if blob.name not in selected_blob_names
    ]
    if not json_blobs:
        raise ValueError(
            f"no Document AI JSON output found at {output_gcs_destination}"
        )
    if ignored_blobs:
        _log.warning(
            "ignored sibling Document AI JSON blobs outside bounded destination "
            "job_id=%s batch_index=%s record_id=%s source_uri=%s "
            "output_gcs_destination=%s destination_blob_path=%s ignored_count=%s "
            "ignored_blob_names=%s selected_blob_names=%s",
            job_id,
            batch_index,
            record_id,
            source_uri,
            output_gcs_destination,
            location.blob_path,
            len(ignored_blobs),
            _sample_blob_names(ignored_blobs),
            _sample_blob_names(json_blobs),
        )

    for blob in json_blobs:
        document_json = blob.download_as_text()
        attributes_list.extend(
            document_ai_api.process_document_json(
                document_json,
                using_default_processor=using_default_processor,
            )
        )

    aggregate_duplicates = _target_attribute_duplicates(attributes_list)
    if aggregate_duplicates:
        _log.warning(
            "Document AI output contains repeated monitored fields "
            "job_id=%s batch_index=%s record_id=%s source_uri=%s "
            "output_gcs_destination=%s selected_json_count=%s duplicate_counts=%s",
            job_id,
            batch_index,
            record_id,
            source_uri,
            output_gcs_destination,
            len(json_blobs),
            aggregate_duplicates,
        )
    return attributes_list


def _is_json_blob(blob):
    return blob.name.endswith(".json") or blob.content_type == "application/json"


def _is_blob_inside_output_destination(blob_name, destination_blob_path):
    destination_blob_path = destination_blob_path.strip("/")
    if not destination_blob_path:
        return True
    if destination_blob_path.endswith(".json"):
        return blob_name == destination_blob_path
    return (
        blob_name == destination_blob_path
        or blob_name.startswith(f"{destination_blob_path}/")
        or blob_name == f"{destination_blob_path}.json"
    )


def _update_record_with_attributes(
    data_manager,
    rg_id,
    record_id,
    file_name,
    processor_attributes,
    attributes_list,
    run_cleaning_functions=True,
):
    if not processor_attributes:
        processor_attributes = []
    processor_attributes = copy.deepcopy(processor_attributes)
    if run_cleaning_functions:
        processor_attributes_dictionary = util.convert_processor_attributes_to_dict(
            processor_attributes
        )

    attributes_list = util.normalize_record_attribute_tree(attributes_list)

    for attribute in attributes_list:
        if run_cleaning_functions:
            util.cleanRecordAttribute(
                processor_attributes=processor_attributes_dictionary,
                attribute=attribute,
            )

    sorted_attributes_list, _ = util.sortRecordAttributes(
        attributes_list,
        {"attributes": processor_attributes},
        keep_all_attributes=True,
    )
    final_duplicate_targets = _target_attribute_duplicates(sorted_attributes_list)
    if final_duplicate_targets:
        _log.warning(
            "batch record attributes contain repeated monitored fields before db "
            "update record_id=%s file_name=%s duplicate_counts=%s",
            record_id,
            file_name,
            final_duplicate_targets,
        )

    record = {
        "record_group_id": rg_id,
        "attributesList": sorted_attributes_list,
        "filename": file_name,
        "status": "digitized",
    }
    data_manager.updateRecord(
        record_id,
        record,
        update_type="record",
        forceUpdate=True,
        calling_function="batch_process_document",
    )
    _log.info(f"updated batch record in db: {record_id}")


def _mark_record_error(data_manager, rg_id, record_id, file_name, error_message):
    record = {
        "record_group_id": rg_id,
        "filename": file_name,
        "status": "error",
        "error_message": error_message,
    }
    data_manager.updateRecord(
        record_id,
        record,
        update_type="record",
        forceUpdate=True,
        calling_function="batch_process_document",
    )


def _get_gcs_documents(input_config):
    return list(input_config.gcs_documents.documents)


def _normalize_prefix(prefix):
    if not prefix:
        return ""
    normalized_prefix = prefix.strip().lstrip("/")
    if not normalized_prefix or normalized_prefix.endswith("/"):
        return normalized_prefix
    return f"{normalized_prefix}/"


def _normalize_output_prefix(output_prefix, rg_id, job_id):
    if output_prefix:
        return output_prefix.strip("/")
    return f"{BATCH_OUTPUT_PREFIX}/{rg_id}/{job_id}"


def _build_output_gcs_uri(bucket_name, output_prefix, batch_index):
    return f"gs://{bucket_name}/{output_prefix}/batch_{batch_index}/"


def _parse_api_number(filename):
    try:
        return int(filename.split("_")[0])
    except Exception:
        _log.info("unable to parse api number")
        return None
