import base64
import logging
import os
import sys

from google.api_core.client_options import ClientOptions
from google.cloud import documentai
import requests
from ogrre.internal import util

_log = logging.getLogger(__name__)

LOCATION = os.getenv("LOCATION")
PROJECT_ID = os.getenv("PROJECT_ID")
STORAGE_SERVICE_KEY = os.getenv("STORAGE_SERVICE_KEY")
DOCUMENT_AI_BACKEND = os.getenv("DOCUMENT_AI_BACKEND", "google").lower()
DOCUMENT_AI_URL = os.getenv("DOCUMENT_AI_URL")
DOCUMENT_AI_TIMEOUT = float(os.getenv("DOCUMENT_AI_TIMEOUT", "60"))


_docai_client = None


def _get_docai_client():
    global _docai_client
    if _docai_client is None:
        client_options = None
        if LOCATION:
            client_options = ClientOptions(
                api_endpoint=f"{LOCATION}-documentai.googleapis.com"
            )
        _docai_client = documentai.DocumentProcessorServiceClient(
            client_options=client_options
        )
    return _docai_client


def _get_coordinates(entity, attribute):
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


def _get_page(entity, attribute):
    try:
        page = entity.page_anchor.page_refs[0].page
    except Exception as e:
        page = None
        _log.info(f"unable to get coordinates of attribute {attribute}: {e}")
    return page


def _get_entity_value(entity):
    normalized_value = entity.normalized_value.text
    raw_text = entity.mention_text
    if normalized_value:
        return normalized_value
    return raw_text


def _get_document_entities(document_object, using_default_processor=False):
    document_entities = document_object.entities
    if using_default_processor:
        if not document_entities:
            return []
        _log.info("generic processor, diving into properties")
        return document_entities[0].properties
    return document_entities


def _entity_to_attribute(entity, top_level_attribute=None, parent_identifier=None):
    raw_attribute = entity.type_
    attribute = util.relative_attribute_key(raw_attribute, parent_identifier)
    text_value = entity.text_anchor.content
    normalized_value = entity.normalized_value.text
    confidence = entity.confidence
    raw_text = entity.mention_text
    coordinates = _get_coordinates(entity, attribute)
    page = _get_page(entity, attribute)
    is_subattribute = top_level_attribute is not None

    new_attribute = {
        "key": attribute,
        "ai_confidence": confidence,
        "confidence": confidence,
        "raw_text": raw_text,
        "text_value": text_value,
        "value": _get_entity_value(entity),
        "normalized_vertices": coordinates,
        "normalized_value": normalized_value,
        "subattributes": [],
        "isSubattribute": is_subattribute,
        "edited": False,
        "page": page,
    }
    if is_subattribute:
        new_attribute["topLevelAttribute"] = top_level_attribute
        new_attribute["parentAttribute"] = parent_identifier

    attribute_identifier = util.combine_attribute_identifier(
        parent_identifier, attribute
    )
    for prop in entity.properties:
        new_attribute["subattributes"].append(
            _entity_to_attribute(
                prop,
                top_level_attribute=top_level_attribute or attribute,
                parent_identifier=attribute_identifier,
            )
        )

    return new_attribute


def _entities_to_attributes(document_entities):
    attributes_list = []

    for entity in document_entities:
        attributes_list.append(_entity_to_attribute(entity))

    return attributes_list


def document_to_attributes(document_object, using_default_processor=False):
    document_entities = _get_document_entities(
        document_object, using_default_processor=using_default_processor
    )
    return _entities_to_attributes(document_entities)


def process_document_json(document_json, using_default_processor=False):
    if isinstance(document_json, bytes):
        document_json = document_json.decode("utf-8")
    document_object = documentai.Document.from_json(
        document_json, ignore_unknown_fields=True
    )
    return document_to_attributes(
        document_object, using_default_processor=using_default_processor
    )


def batch_process_documents(
    input_documents,
    output_gcs_uri,
    processor_id,
    model_id,
    skip_human_review=False,
):
    if DOCUMENT_AI_BACKEND != "google":
        raise ValueError("Batch Document AI processing requires the google backend")

    docai_client = _get_docai_client()
    resource_name = docai_client.processor_version_path(
        PROJECT_ID, LOCATION, processor_id, model_id
    )

    request = documentai.BatchProcessRequest(
        name=resource_name,
        input_documents=input_documents,
        document_output_config=documentai.DocumentOutputConfig(
            gcs_output_config=documentai.DocumentOutputConfig.GcsOutputConfig(
                gcs_uri=output_gcs_uri
            )
        ),
        skip_human_review=skip_human_review,
    )
    return docai_client.batch_process_documents(request=request)


def process_document_content(
    image_content,
    mime_type,
    processor_id,
    model_id,
    using_default_processor=False,
):
    if DOCUMENT_AI_BACKEND != "google":
        return _process_document_content_custom(
            image_content=image_content,
            mime_type=mime_type,
            processor_id=processor_id,
            model_id=model_id,
            using_default_processor=using_default_processor,
        )

    docai_client = _get_docai_client()
    resource_name = docai_client.processor_version_path(
        PROJECT_ID, LOCATION, processor_id, model_id
    )

    raw_document = documentai.RawDocument(content=image_content, mime_type=mime_type)
    request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document)
    result = docai_client.process_document(request=request)
    document_object = result.document

    document_entities = _get_document_entities(
        document_object, using_default_processor=using_default_processor
    )

    attributes_list = []

    for entity in document_entities:
        attributes_list.append(_entity_to_attribute(entity))

    return attributes_list


def _process_document_content_custom(
    image_content,
    mime_type,
    processor_id,
    model_id,
    using_default_processor=False,
):
    if not DOCUMENT_AI_URL:
        raise ValueError(
            "DOCUMENT_AI_URL is required when DOCUMENT_AI_BACKEND != google"
        )

    payload = {
        "image_content_base64": base64.b64encode(image_content).decode("utf-8"),
        "mime_type": mime_type,
        "processor_id": processor_id,
        "model_id": model_id,
        "using_default_processor": using_default_processor,
    }
    response = requests.post(DOCUMENT_AI_URL, json=payload, timeout=DOCUMENT_AI_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return util.normalize_record_attribute_tree(data)
    attributes_list = data.get("attributes_list")
    if attributes_list is None:
        raise ValueError("custom document ai response missing attributes_list")
    return util.normalize_record_attribute_tree(attributes_list)


def deploy_processor(rg_id, data_manager, user_info=None):
    if DOCUMENT_AI_BACKEND != "google":
        _log.info("custom document ai backend selected; skipping deploy")
        return "DEPLOYED"
    from ogrre.internal.google_processor_manager import deploy_processor_version

    processor_id, model_id, _ = data_manager.getProcessorByRecordGroupID(
        rg_id, user=user_info
    )

    docai_client = _get_docai_client()
    resource_name = docai_client.processor_version_path(
        PROJECT_ID, LOCATION, processor_id, model_id
    )

    _log.debug(f"attempting to deploy processor model: {model_id}")
    deployment = deploy_processor_version(resource_name)
    return deployment


def undeploy_processor(rg_id, data_manager, user_info=None):
    if DOCUMENT_AI_BACKEND != "google":
        _log.info("custom document ai backend selected; skipping undeploy")
        return True
    from ogrre.internal.google_processor_manager import undeploy_processor_version

    processor_id, model_id, _ = data_manager.getProcessorByRecordGroupID(
        rg_id, user=user_info
    )

    docai_client = _get_docai_client()
    resource_name = docai_client.processor_version_path(
        PROJECT_ID, LOCATION, processor_id, model_id
    )
    undeploy_processor_version(resource_name)
    return True


def check_if_processor_is_deployed(rg_id, data_manager, user_info=None):
    if DOCUMENT_AI_BACKEND != "google":
        _log.info("custom document ai backend selected; returning deployed")
        return 1
    processor_id, model_id, _ = data_manager.getProcessorByRecordGroupID(
        rg_id, user=user_info
    )
    _log.info(f"checking deployment status of {processor_id} : {model_id}")

    client = _get_docai_client()
    parent = client.processor_path(PROJECT_ID, LOCATION, processor_id)

    processor_versions = client.list_processor_versions(parent=parent)
    for processor_version in processor_versions:
        processor_version_id = client.parse_processor_version_path(
            processor_version.name
        )["processor_version"]
        if processor_version_id == model_id:
            _log.debug(f"processor state == {processor_version.state}")
            return processor_version.state
    _log.error(f"unable to find model id: {model_id}")
    return 10
