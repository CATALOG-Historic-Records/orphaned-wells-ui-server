import os
import logging
import datetime
import json
import aiofiles
import aiohttp
from PIL import Image
from gcloud.aio.storage import Storage
from google.api_core.client_options import ClientOptions
from google.cloud import documentai, storage
from dotenv import load_dotenv

_log = logging.getLogger(__name__)

load_dotenv()
LOCATION = os.getenv("LOCATION")
PROJECT_ID = os.getenv("PROJECT_ID")
PROCESSOR_ID = os.getenv("PROCESSOR_ID")
STORAGE_SERVICE_KEY = os.getenv("STORAGE_SERVICE_KEY")
os.environ["GCLOUD_PROJECT"] = PROJECT_ID

docai_client = documentai.DocumentProcessorServiceClient(
    client_options=ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com")
)


def convert_tiff(filename, file_ext, output_directory, convert_to=".png"):
    # print(f'converting: {filename}.{file_ext} to {convert_to}')
    filepath = f"{output_directory}/{filename}{file_ext}"
    try:
        outfile = f"{output_directory}/{filename}{convert_to}"
        # print(f'outfile is {outfile}')
        try:
            im = Image.open(filepath)
            im.thumbnail(im.size)
            im.save(outfile, "PNG", quality=100)
            return outfile
        except Exception as e:
            print(f"unable to save {filename}: {e}")
            return filepath

    except Exception as e:
        print(f"failed to convert {filename}: {e}")
        return filepath


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


## Document AI functions
def process_image(
    file_path, file_name, mime_type, project_id, record_id, processor_id, data_manager
):
    with open(file_path, "rb") as image:
        image_content = image.read()

    if processor_id is None:
        _log.info(
            f"processor id is none, rolling with default processor: {PROCESSOR_ID}"
        )
        processor_id = PROCESSOR_ID

    RESOURCE_NAME = docai_client.processor_path(PROJECT_ID, LOCATION, processor_id)

    raw_document = documentai.RawDocument(content=image_content, mime_type=mime_type)
    request = documentai.ProcessRequest(name=RESOURCE_NAME, raw_document=raw_document)

    # Use the Document AI client to process the document
    result = docai_client.process_document(request=request)
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
    attributes = {}
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
        subattributes = {}
        for prop in entity.properties:
            sub_text_value = prop.text_anchor.content
            sub_normalized_value = prop.normalized_value.text
            sub_attribute = prop.type_
            sub_confidence = prop.confidence
            sub_raw_text = prop.mention_text
            sub_coordinates = get_coordinates(prop, sub_attribute)
            if sub_normalized_value:
                sub_value = sub_normalized_value
            else:
                sub_value = sub_raw_text
            counter = 2
            original_sub_attribute = sub_attribute
            while (
                sub_attribute in subattributes
            ):  ## if we make it inside this loop, then this subattribute appears multiple times
                sub_attribute = f"{original_sub_attribute}_{counter}"
                counter += 1

            subattributes[sub_attribute] = {
                "confidence": sub_confidence,
                "raw_text": sub_raw_text,
                "text_value": sub_text_value,
                "value": sub_value,
                "normalized_vertices": sub_coordinates,
                "normalized_value": sub_normalized_value,
            }
        if len(subattributes) == 0:
            subattributes = None

        counter = 2
        original_attribute = attribute
        while (
            attribute in attributes
        ):  ## if we make it inside this loop, then this attribute appears multiple times
            attribute = f"{original_attribute}_{counter}"
            counter += 1

        attributes[attribute] = {
            "confidence": confidence,
            "raw_text": raw_text,
            "text_value": text_value,
            "value": value,
            "normalized_vertices": coordinates,
            "normalized_value": normalized_value,
            "subattributes": subattributes,
        }

    ## gotta create the record in the db
    record = {
        "project_id": project_id,
        "attributes": attributes,
        "filename": f"{file_name}",
        "status": "digitized",
    }
    # new_record_id = data_manager.createRecord(record)
    data_manager.updateRecord(record_id, record, update_type="record")
    _log.info(f"updated record in db: {record_id}")
    ## TODO: Remove image from local file system. Have to make sure upload to Cloud Storage is complete as well

    return record_id


## Google Cloud Storage Functions
async def async_upload_to_bucket(
    blob_name, file_obj, folder, bucket_name="uploaded_documents_v0"
):
    """Upload image file to bucket."""
    async with aiohttp.ClientSession() as session:
        storage = Storage(service_file="./internal/creds.json", session=session)
        status = await storage.upload(bucket_name, f"{folder}/{blob_name}", file_obj)
        return status["selfLink"]


async def upload_to_google_storage(file_path, file_name, folder="uploads"):
    async with aiofiles.open(file_path, "rb") as afp:
        f = await afp.read()
    url = await async_upload_to_bucket(file_name, f, folder=folder)
    _log.info(f"uploaded document to cloud storage: {url}")


def generate_download_signed_url_v4(
    project_id, filename, bucket_name="uploaded_documents_v0"
):
    """Generates a v4 signed URL for downloading a blob.

    Note that this method requires a service account key file. You can not use
    this if you are using Application Default Credentials from Google Compute
    Engine or from the Google Cloud SDK.
    To generate STORAGE_SERVICE_KEY, follow steps here:
    https://docs.gspread.org/en/latest/oauth2.html#for-bots-using-service-account
    """

    storage_client = storage.Client.from_service_account_json(
        f"./internal/{STORAGE_SERVICE_KEY}"
    )

    # blob_name: path to file in google cloud bucket
    blob_name = f"uploads/{project_id}/{filename}"
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
