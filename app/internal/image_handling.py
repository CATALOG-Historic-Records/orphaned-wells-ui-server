import os
import sys
import logging
import datetime
import json
import aiofiles
import aiohttp
from PIL import Image
from gcloud.aio.storage import Storage
from google.api_core.client_options import ClientOptions
from google.cloud import documentai, storage

from fastapi import HTTPException
import fitz
import zipfile
import mimetypes

from app.internal.bulk_upload import upload_documents_from_directory

_log = logging.getLogger(__name__)

LOCATION = os.getenv("LOCATION")
PROJECT_ID = os.getenv("PROJECT_ID")
PROCESSOR_ID = os.getenv("PROCESSOR_ID")
STORAGE_SERVICE_KEY = os.getenv("STORAGE_SERVICE_KEY")
BUCKET_NAME = os.getenv("STORAGE_BUCKET_NAME")
os.environ["GCLOUD_PROJECT"] = PROJECT_ID
DIRNAME, FILENAME = os.path.split(os.path.abspath(sys.argv[0]))

docai_client = documentai.DocumentProcessorServiceClient(
    client_options=ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com"),
    # credentials=f"{DIRNAME}/creds.json"
)


def process_zip(
    rg_id,
    user_info,
    background_tasks,
    zip_file,
    image_dir,
    zip_filename,
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
    backend_url = os.getenv("BACKEND_URL")
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
        async with aiofiles.open(original_output_path, "wb") as out_file:
            content = await file.read()
            await out_file.write(content)

        return await process_document(
            rg_id,
            user_info,
            background_tasks,
            original_output_path,
            file_ext,
            filename,
            data_manager,
            mime_type,
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
    content,
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

    ## add record to DB without attributes
    new_record = {
        "record_group_id": rg_id,
        "name": filename,
        "filename": f"{filename}{file_ext}",
        "contributor": user_info,
        "status": "processing",
        "review_status": "unreviewed",
        "image_files": [output_path.split("/")[-1] for output_path in output_paths],
    }
    new_record_id = data_manager.createRecord(new_record, user_info)

    ## fetch processor id
    processor_id, processor_attributes = data_manager.getProcessorByRecordGroupID(rg_id)

    ## upload to cloud storage
    for output_path in output_paths:
        filepath = output_path.split("/")[-1]
        background_tasks.add_task(
            upload_to_google_storage,
            file_path=output_path,
            file_name=f"{filepath}",
            folder=f"uploads/{rg_id}/{new_record_id}",
        )

    ## send to google doc AI
    background_tasks.add_task(
        process_image,
        file_name=f"{filename}{file_ext}",
        mime_type=mime_type,
        rg_id=rg_id,
        record_id=new_record_id,
        processor_id=processor_id,
        processor_attributes=processor_attributes,
        data_manager=data_manager,
        image_content=content,
    )

    ## remove file after 60 seconds to allow for the operations to finish
    ## if file was converted to PNG, remove original file as well
    files_to_delete = output_paths  # [output_path]
    if original_output_path not in output_path:
        files_to_delete.append(original_output_path)
    background_tasks.add_task(
        data_manager.deleteFiles, filepaths=files_to_delete, sleep_time=60
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
            return outfile
        except Exception as e:
            print(f"unable to save {filename}: {e}")
            return [filepath]

    except Exception as e:
        print(f"failed to convert {filename}: {e}")
        return [filepath]


def get_coordinates(entity, attribute):
    try:
        bounding_poly = entity.page_anchor.page_refs[0].bounding_poly
        coordinates = []
        for i in range(4):
            coordinate = bounding_poly.normalized_vertices[i]
            coordinates.append([coordinate.x, coordinate.y])
    except Exception as e:
        coordinates = None
        _log.info(f"unable to get coordinates of attribute {attribute}: {e}")
    return coordinates


def get_page(entity, attribute):
    try:
        page = entity.page_anchor.page_refs[0].page
    except Exception as e:
        page = None
        _log.info(f"unable to get coordinates of attribute {attribute}: {e}")
    return page


## Document AI functions
def process_image(
    file_name,
    mime_type,
    rg_id,
    record_id,
    processor_id,
    processor_attributes,
    data_manager,
    image_content,
):
    if processor_id is None:
        _log.info(
            f"processor id is none, rolling with default processor: {PROCESSOR_ID}"
        )
        processor_id = PROCESSOR_ID

    RESOURCE_NAME = docai_client.processor_path(PROJECT_ID, LOCATION, processor_id)

    raw_document = documentai.RawDocument(content=image_content, mime_type=mime_type)
    try:
        request = documentai.ProcessRequest(
            name=RESOURCE_NAME, raw_document=raw_document
        )
    except Exception as e:
        _log.error(f"error on documentai.ProcessRequest: {e}")
        record = {
            "record_group_id": rg_id,
            "filename": f"{file_name}",
            "status": "error",
            "error_message": str(e),
        }
        data_manager.updateRecord(
            record_id, record, update_type="record", forceUpdate=True
        )
        return

    ## delete raw document and image_content to free up memory
    del image_content
    del raw_document

    # Use the Document AI client to process the document
    try:
        result = docai_client.process_document(request=request)
    except Exception as e:
        _log.error(f"error on docai_client.process_document: {e}")
        record = {
            "record_group_id": rg_id,
            "filename": f"{file_name}",
            "status": "error",
            "error_message": str(e),
        }
        data_manager.updateRecord(
            record_id, record, update_type="record", forceUpdate=True
        )
        return

    _log.info(f"processed document in doc_ai")
    document_object = result.document

    # our predefined attributes will be located in the entities object
    document_entities = document_object.entities
    """
    entities has the following (useful) attributes: 
    <attribute>: <example value>
    type_: "Spud_Date"
    mention_text: "8-25-72"
    confidence: 1
    normalized_value:
        {
            date_value {
                year: 1972
                month: 8
                day: 25
            }
            text: "1972-08-25"
        }
    """
    attributesList = []
    found_attributes = {}
    for entity in document_entities:
        text_value = entity.text_anchor.content
        normalized_value = entity.normalized_value.text

        attribute = entity.type_
        confidence = entity.confidence
        raw_text = entity.mention_text
        if normalized_value:
            value = normalized_value
        else:
            value = raw_text
        coordinates = get_coordinates(entity, attribute)

        ## page numbers start at 0
        page = get_page(entity, attribute)
        # subattributes = {}
        subattributesList = []
        for prop in entity.properties:
            sub_text_value = prop.text_anchor.content
            sub_normalized_value = prop.normalized_value.text
            sub_attribute = prop.type_
            sub_confidence = prop.confidence
            sub_raw_text = prop.mention_text
            sub_coordinates = get_coordinates(prop, sub_attribute)
            sub_page = get_page(prop, sub_attribute)
            if sub_normalized_value:
                sub_value = sub_normalized_value
            else:
                sub_value = sub_raw_text
            original_sub_attribute = sub_attribute

            subattributesList.append(
                {
                    "key": original_sub_attribute,
                    "ai_confidence": confidence,
                    "confidence": sub_confidence,
                    "raw_text": sub_raw_text,
                    "text_value": sub_text_value,
                    "value": sub_value,
                    "normalized_vertices": sub_coordinates,
                    "normalized_value": sub_normalized_value,
                    "isSubattribute": True,
                    "topLevelAttribute": attribute,
                    "edited": False,
                    "page": sub_page,
                }
            )

        if len(subattributesList) == 0:
            subattributesList = None

        original_attribute = attribute
        found_attributes[original_attribute] = len(attributesList)
        attributesList.append(
            {
                "key": original_attribute,
                "ai_confidence": confidence,
                "confidence": confidence,
                "raw_text": raw_text,
                "text_value": text_value,
                "value": value,
                "normalized_vertices": coordinates,
                "normalized_value": normalized_value,
                "subattributes": subattributesList,
                "isSubattribute": False,
                "edited": False,
                "page": page,
            }
        )

    ## sort attributes and add attributes that weren't found:
    sortedAttributesList = []
    for processor_attribute in processor_attributes:
        attr = processor_attribute["name"]
        if attr in found_attributes:
            idx = found_attributes[attr]
            sortedAttributesList.append(attributesList[idx])
        else:
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

    ## gotta update the record in the db
    record = {
        "record_group_id": rg_id,
        "attributesList": sortedAttributesList,
        "filename": f"{file_name}",
        "status": "digitized",
    }
    data_manager.updateRecord(record_id, record, update_type="record", forceUpdate=True)

    ## delete objects to free up memory
    del record
    del attributesList
    del sortedAttributesList
    del document_object
    del found_attributes

    _log.info(f"updated record in db: {record_id}")

    return record_id


## Google Cloud Storage Functions
async def async_upload_to_bucket(blob_name, file_obj, folder, bucket_name=BUCKET_NAME):
    """Upload image file to bucket."""

    async with aiohttp.ClientSession() as session:
        storage = Storage(
            service_file=f"{DIRNAME}/internal/creds.json", session=session
        )
        status = await storage.upload(bucket_name, f"{folder}/{blob_name}", file_obj)
        return status["selfLink"]


async def upload_to_google_storage(file_path, file_name, folder="uploads"):
    async with aiofiles.open(file_path, "rb") as afp:
        f = await afp.read()
    url = await async_upload_to_bucket(file_name, f, folder=folder)
    _log.info(f"uploaded document to cloud storage: {url}")


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


def delete_google_storage_directory(rg_id, bucket_name=BUCKET_NAME):
    _log.info(f"deleting record group {rg_id} from google storage")
    storage_client = storage.Client.from_service_account_json(
        f"{DIRNAME}/internal/{STORAGE_SERVICE_KEY}"
    )
    bucket = storage_client.get_bucket(bucket_name)
    blobs = bucket.list_blobs(prefix=f"uploads/{rg_id}")
    for blob in blobs:
        _log.info(f"deleting {blob}")
        blob.delete()
