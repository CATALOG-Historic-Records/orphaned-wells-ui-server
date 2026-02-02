import os
import logging
import time
import aiofiles
import multiprocessing
import tracemalloc
from PIL import Image

from fastapi import HTTPException
import fitz
import zipfile
import mimetypes

from ogrre.internal.bulk_upload import upload_documents_from_directory
from ogrre.internal import storage_api
from ogrre.internal import document_ai_api
import ogrre.internal.util as util

_log = logging.getLogger(__name__)


MEMORY_PROFILE = os.getenv("MEMORY_PROFILE", "").lower() in ("1", "true", "yes")
MEMORY_PROFILE_RATE = int(os.getenv("MEMORY_PROFILE_RATE", "1"))
MEMORY_PROFILE_TOP = int(os.getenv("MEMORY_PROFILE_TOP", "10"))
PROCESS_IMAGE_IN_SUBPROCESS = os.getenv("PROCESS_IMAGE_IN_SUBPROCESS", "").lower() in (
    "1",
    "true",
    "yes",
)
_profile_counter = 0


def _maybe_take_snapshot():
    global _profile_counter
    if not MEMORY_PROFILE:
        return None
    _profile_counter += 1
    if _profile_counter % MEMORY_PROFILE_RATE != 0:
        return None
    if not tracemalloc.is_tracing():
        tracemalloc.start()
    return tracemalloc.take_snapshot()


def _log_snapshot_diff(before, after, label, record_id=None):
    if not before or not after:
        return
    pid = os.getpid()
    prefix = f"pid={pid}"
    if record_id:
        prefix = f"{prefix} record_id={record_id}"
    stats = after.compare_to(before, "lineno")
    _log.info(f"memory profile {label} [{prefix}]: top {MEMORY_PROFILE_TOP}")
    for stat in stats[:MEMORY_PROFILE_TOP]:
        _log.info(f"{stat}")


async def _save_upload_file(upload_file, destination_path):
    async with aiofiles.open(destination_path, "wb") as out_file:
        chunk_size = 1024 * 1024
        while True:
            chunk = await upload_file.read(chunk_size)
            if not chunk:
                break
            await out_file.write(chunk)


def process_zip(
    rg_id,
    user_info,
    background_tasks,
    zip_file,
    image_dir,
    zip_filename,
    backend_url,
):
    ## read document file
    _log.info(f"processing a zip: {zip_filename}")
    output_dir = f"{image_dir}/unzipped"
    zip_path = f"{output_dir}/{zip_filename}"
    with zipfile.ZipFile(zip_file.file, "r") as zip_ref:
        zip_ref.extractall(output_dir)

    for directory, subdirectories, files in os.walk(zip_path):
        for file in files:
            unzipped_img_filepath = os.path.join(directory, file)
            mime_type = mimetypes.guess_type(file)[0]

            # if it is not a document file, remove it
            if mime_type is None:
                os.remove(unzipped_img_filepath)
    background_tasks.add_task(
        upload_documents_from_directory,
        backend_url=backend_url,
        user_email=user_info["email"],
        rg_id=rg_id,
        local_directory=zip_path,
        delete_local_files=True,
    )

    return {"success": zip_filename}


async def process_single_file(
    rg_id,
    user_info,
    background_tasks,
    file,
    original_output_path,
    file_ext,
    filename,
    data_manager,
):
    mime_type = file.content_type
    ## read document file
    try:
        await _save_upload_file(file, original_output_path)
        return await process_document(
            rg_id,
            user_info,
            background_tasks,
            original_output_path,
            file_ext,
            filename,
            data_manager,
            mime_type,
            doc_ai_input_path=original_output_path,
        )
    except Exception as e:
        _log.error(f"unable to read image file: {e}")
        raise HTTPException(400, detail=f"Unable to process image file: {e}")


def process_document(
    rg_id,
    user_info,
    background_tasks,
    original_output_path,
    file_ext,
    filename,
    data_manager,
    mime_type,
    doc_ai_input_path,
    reprocessed=False,
    run_cleaning_functions=True,
    undeployProcessor=True,
):
    if file_ext == ".tif" or file_ext == ".tiff":
        output_paths = convert_tiff(
            filename, file_ext, data_manager.app_settings.img_dir
        )
        file_ext = ".png"
    elif file_ext.lower() == ".pdf":
        output_paths = convert_pdf(
            filename, file_ext, data_manager.app_settings.img_dir
        )
        file_ext = ".png"
    else:
        output_paths = [original_output_path]

    try:
        # parse api number from filename
        api_number = filename.split("_")[0]
        api_number = int(api_number)
    except Exception as e:
        _log.info(f"unable to parse api number")
        api_number = None
    ## add record to DB without attributes
    new_record = {
        "record_group_id": rg_id,
        "name": filename,
        "filename": f"{filename}{file_ext}",
        "api_number": api_number,
        "contributor": user_info,
        "status": "processing",
        "review_status": "unreviewed",
        "original_filename": original_output_path.split("/")[-1],
        "image_files": [output_path.split("/")[-1] for output_path in output_paths],
    }
    new_record_id = data_manager.createRecord(new_record, user_info)

    ## fetch processor id
    (
        processor_id,
        model_id,
        processor_attributes,
    ) = data_manager.getProcessorByRecordGroupID(rg_id)

    ## upload to cloud storage
    for output_path in output_paths:
        filepath = output_path.split("/")[-1]
        background_tasks.add_task(
            storage_api.upload_file,
            file_path=output_path,
            file_name=f"{filepath}",
            folder=f"uploads/{rg_id}/{new_record_id}",
        )

    ## if original file was pdf, make sure to delete both image and pdf files
    files_to_delete = output_paths
    if original_output_path not in output_path:
        files_to_delete.append(original_output_path)

    ## send to google doc AI
    if PROCESS_IMAGE_IN_SUBPROCESS:
        background_tasks.add_task(
            _spawn_process_image_worker,
            file_name=f"{filename}{file_ext}",
            mime_type=mime_type,
            rg_id=rg_id,
            record_id=new_record_id,
            processor_id=processor_id,
            model_id=model_id,
            processor_attributes=processor_attributes,
            doc_ai_input_path=doc_ai_input_path,
            reprocessed=reprocessed,
            files_to_delete=files_to_delete,
            run_cleaning_functions=run_cleaning_functions,
            undeployProcessor=undeployProcessor,
        )
    else:
        background_tasks.add_task(
            process_image,
            file_name=f"{filename}{file_ext}",
            mime_type=mime_type,
            rg_id=rg_id,
            record_id=new_record_id,
            processor_id=processor_id,
            model_id=model_id,
            processor_attributes=processor_attributes,
            data_manager=data_manager,
            doc_ai_input_path=doc_ai_input_path,
            reprocessed=reprocessed,
            files_to_delete=files_to_delete,
            run_cleaning_functions=run_cleaning_functions,
            undeployProcessor=undeployProcessor,
        )
    return {"record_id": new_record_id}


def convert_pdf(filename, file_ext, output_directory, convert_to=".png"):
    filepath = f"{output_directory}/{filename}{file_ext}"
    try:
        output_paths = []
        dpi = 100  ## higher dpi will result in higher quality but longer wait time
        doc = fitz.open(filepath)
        zoom = 4
        mat = fitz.Matrix(zoom, zoom)

        i = 0
        for page in doc:
            pix = page.get_pixmap(matrix=mat, dpi=dpi)
            if i == 0:
                outfile = f"{output_directory}/{filename}{convert_to}"
            else:
                print(f"this doc has more than one page")
                outfile = f"{output_directory}/{filename}_{i+1}{convert_to}"
            pix.save(outfile)
            output_paths.append(outfile)
            i += 1
        doc.close()
        return output_paths
    except Exception as e:
        print(f"failed to convert {filename}: {e}")
        return [filepath]


def convert_tiff(filename, file_ext, output_directory, convert_to=".png"):
    filepath = f"{output_directory}/{filename}{file_ext}"
    try:
        outfile = f"{output_directory}/{filename}{convert_to}"
        try:
            im = Image.open(filepath)
            im.thumbnail(im.size)
            im.save(outfile, "PNG", quality=100)
            return [outfile]
        except Exception as e:
            print(f"unable to save {filename}: {e}")
            return [filepath]

    except Exception as e:
        print(f"failed to convert {filename}: {e}")
        return [filepath]


def _process_image_worker(**kwargs):
    from ogrre.internal.data_manager import DataManager

    data_manager = DataManager()
    process_image(data_manager=data_manager, **kwargs)


def _spawn_process_image_worker(**kwargs):
    ctx = multiprocessing.get_context("spawn")
    process = ctx.Process(target=_process_image_worker, kwargs=kwargs)
    process.daemon = True
    process.start()


## Document AI functions
def process_image(
    file_name,
    mime_type,
    rg_id,
    record_id,
    processor_id,
    model_id,
    processor_attributes,
    data_manager,
    doc_ai_input_path,
    reprocessed=False,
    files_to_delete=[],
    run_cleaning_functions=True,
    undeployProcessor=True,
):
    snapshot_start = _maybe_take_snapshot()
    try:
        with open(doc_ai_input_path, "rb") as file_handle:
            image_content = file_handle.read()
    except Exception as e:
        _log.error(f"unable to read doc ai input file: {e}")
        record = {
            "record_group_id": rg_id,
            "filename": f"{file_name}",
            "status": "error",
            "error_message": str(e),
        }
        data_manager.updateRecord(
            record_id,
            record,
            update_type="record",
            forceUpdate=True,
            calling_function="process_image",
        )
        return

    if not processor_attributes:
        _log.info(f"no processor attributes found")
        processor_attributes = []
    if run_cleaning_functions:
        prcoessor_attributes_dictionary = util.convert_processor_attributes_to_dict(
            processor_attributes
        )

    snapshot_after_prepare = _maybe_take_snapshot()
    _log_snapshot_diff(
        snapshot_start,
        snapshot_after_prepare,
        "after_prepare_request",
        record_id=record_id,
    )

    # Use the Document AI client to process the document
    try:
        attributesList = document_ai_api.process_document_content(
            image_content=image_content,
            mime_type=mime_type,
            processor_id=processor_id,
            model_id=model_id,
            using_default_processor=data_manager.using_default_processor,
        )
    except Exception as e:
        _log.error(f"error on google document ai processing: {e}")
        record = {
            "record_group_id": rg_id,
            "filename": f"{file_name}",
            "status": "error",
            "error_message": str(e),
        }
        data_manager.updateRecord(
            record_id,
            record,
            update_type="record",
            forceUpdate=True,
            calling_function="process_image",
        )
        return

    del image_content

    snapshot_after_process = _maybe_take_snapshot()
    _log_snapshot_diff(
        snapshot_after_prepare,
        snapshot_after_process,
        "after_docai_process",
        record_id=record_id,
    )
    _log.info(f"processed document in doc_ai")
    found_attributes = {}
    for idx, attribute in enumerate(attributesList):
        attribute_key = attribute["key"]
        found_attributes.setdefault(attribute_key, []).append(idx)
        if run_cleaning_functions:
            util.cleanRecordAttribute(
                processor_attributes=prcoessor_attributes_dictionary,
                attribute=attribute,
            )
            subattributes_list = attribute.get("subattributes") or []
            for subattribute in subattributes_list:
                util.cleanRecordAttribute(
                    processor_attributes=prcoessor_attributes_dictionary,
                    attribute=subattribute,
                    subattributeKey=f"{attribute_key}::{subattribute['key']}",
                )

    ## sort attributes and add attributes that weren't found:
    sortedAttributesList = []
    processor_attributes_list = []
    for processor_attribute in processor_attributes:
        attr = processor_attribute["name"]
        processor_attributes_list.append(attr)
        if attr in found_attributes:
            indexes = found_attributes[attr]
            for idx in indexes:
                sortedAttributesList.append(attributesList[idx])
        elif (
            "::" not in attr
        ):  ## :: indicates it is a subattribute. these are handled by parent attribut
            sortedAttributesList.append(
                {
                    "key": attr,
                    "ai_confidence": None,
                    "confidence": None,
                    "raw_text": "",
                    "text_value": "",
                    "value": "",
                    "normalized_vertices": None,
                    "normalized_value": None,
                    "subattributes": None,
                    "isSubattribute": False,
                    "edited": False,
                    "page": None,
                }
            )

    ## double check found attributes to see if we found anything that was NOT in the processor's attributes
    for attr in found_attributes:
        if attr not in processor_attributes_list:
            _log.info(
                f"{attr} was not in processor's attributes. adding this to the end of the sorted attributes list"
            )
            indexes = found_attributes[attr]
            for idx in indexes:
                sortedAttributesList.append(attributesList[idx])

    ## gotta update the record in the db
    record = {
        "record_group_id": rg_id,
        "attributesList": sortedAttributesList,
        "filename": f"{file_name}",
        "status": "digitized",
    }
    if reprocessed:
        record["status"] = "reprocessed"
    data_manager.updateRecord(
        record_id,
        record,
        update_type="record",
        forceUpdate=True,
        calling_function="process_image",
    )

    ## delete objects to free up memory
    del record
    del attributesList
    del sortedAttributesList
    del found_attributes

    ## delete local files
    util.deleteFiles(filepaths=files_to_delete, sleep_time=0)

    _log.info(f"updated record in db: {record_id}")

    return record_id


def deployProcessor(rg_id, data_manager):
    _log.debug(f"attempting to deploy processor for record group {rg_id}")
    start_time = time.time()
    deployment = document_ai_api.deploy_processor(rg_id, data_manager)
    if deployment != "DEPLOYED":
        finish_time = time.time()
        _log.error(
            f"we have an issue, deployment failed. took {finish_time-start_time} seconds to fail deploy"
        )
        return False
    finish_time = time.time()
    _log.debug(f"took {finish_time-start_time} seconds to DEPLOY")
    return True


def undeployProcessor(rg_id, data_manager):
    _log.debug(f"attempting to deploy processor for record group {rg_id}")
    start_time = time.time()
    document_ai_api.undeploy_processor(rg_id, data_manager)
    finish_time = time.time()
    _log.debug(f"took {finish_time-start_time} seconds to undeploy")
    return True


def check_if_processor_is_deployed(rg_id, data_manager):
    try:
        return document_ai_api.check_if_processor_is_deployed(rg_id, data_manager)
    except Exception as e:
        print(f"unable to check processor status: {e}")
        return 10
