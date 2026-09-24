import logging
import time
import os
import csv
import io
import json
import re
import uuid
import copy
import hashlib
from contextlib import contextmanager
from contextvars import ContextVar
from importlib.metadata import version, PackageNotFoundError
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from bson import BSON, ObjectId
from pymongo import ASCENDING, DESCENDING, InsertOne, UpdateOne, ReturnDocument
from pymongo.errors import DuplicateKeyError, ExecutionTimeout

import ogrre_data_cleaning.processor_schemas.processor_api as processor_api
from ogrre.internal import storage_api
from ogrre.internal import directory_upload
from ogrre.internal import processing_job_history
from ogrre.internal import schema_validation as schema_rules
from ogrre.internal import schema_import
from ogrre.internal import schema_inference
from ogrre.internal.mongodb_connection import connectToDatabase
from ogrre.internal.settings import AppSettings
from ogrre.internal.util import get_document_image
import ogrre.internal.util as util
from ogrre.internal.util import time_it

_log = logging.getLogger(__name__)
_catalog_writer = ContextVar("schema_catalog_writer", default=None)
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "true").lower() in ("1", "true", "yes")

DEFAULT_UNAUTHENTICATED_TEAM = {
    "name": "default",
    "display_name": "Default",
    "users": ["anonymous"],
    "project_list": [],
}

DEFAULT_PROCESSORS = [
    {
        "Processor Type": "Extractor",
        "Processor Name": "Default Extractor",
        "Processor ID": "171289310a83c48b",
        "Model ID": "pretrained-form-parser-v2.1-2023-06-26",
    },
]

USE_DB_PROCESSORS = os.getenv("USE_DB_PROCESSORS", "false").lower() in (
    "1",
    "true",
    "yes",
)


class DataManager:
    """Manage the active data."""

    VERSION = 1

    def __init__(self, **kwargs) -> None:
        self.app_settings = AppSettings(**kwargs)
        self.db = connectToDatabase()
        self.db.records.create_index(
            [("record_group_id", 1), ("attribute_schema_revision", 1)]
        )
        self.db.record_groups.create_index("schema_id")
        self.db.records.create_index(
            [("record_group_id", 1), ("_id", 1)], name=schema_inference.INDEX
        )
        self.environment = os.getenv("ENVIRONMENT")
        self.collaborator = os.getenv("COLLABORATOR")
        _log.info(f"working in environment: {self.environment}")
        _log.info(f"collaborator is: {self.collaborator}")

        self.LOCKED = False
        ## lock_duration: amount of seconds that records remain locked if no changes are made
        self.lock_duration = 120
        self.use_airtable = False
        self.ensureDefaultUnauthenticatedTeam()

    def _createEmptyRecordAttribute(
        self,
        key,
        is_subattribute=False,
        top_level_attribute=None,
        parent_attribute=None,
    ):
        new_field = {
            "key": key,
            "ai_confidence": None,
            "confidence": None,
            "raw_text": None,
            "text_value": None,
            "value": "",
            "normalized_vertices": None,
            "normalized_value": None,
            "subattributes": [],
            "isSubattribute": is_subattribute,
            "edited": False,
            "page": None,
            "user_added": True,
        }
        if is_subattribute:
            new_field["topLevelAttribute"] = top_level_attribute
            new_field["parentAttribute"] = parent_attribute
        return new_field

    def _getFieldIndexes(self, data):
        data = data or {}
        field_id = data.get("fieldID") or {}
        index_source = {**data, **field_id} if field_id else data
        indexes = index_source.get("indexes")

        ## 6/25/26: Added indexes to fieldID. This contains all indexes for attribute and subattributes
        ## Keep the following block to ensure backwards compatibility
        if indexes is None:
            primary_index = index_source.get("primaryIndex", index_source.get("idx"))
            if primary_index is None:
                return []

            indexes = [primary_index]
            indexes.extend(
                util.normalize_subattribute_index_path(
                    index_source,
                    index_source.get("subIndex"),
                )
            )

        if not isinstance(indexes, list):
            indexes = [indexes]

        normalized_indexes = []
        for idx in indexes:
            if idx is None:
                continue
            normalized_indexes.append(int(idx))
        return normalized_indexes

    def _getAttributeAtPath(self, attributes, indexes):
        if not indexes:
            return None, None

        try:
            if indexes[0] < 0:
                return None, None

            attribute = attributes[indexes[0]]
            if attribute.get("deleted"):
                return None, None
            attribute_identifier = attribute.get("key")
            for sub_index in indexes[1:]:
                if sub_index < 0:
                    return None, None
                subattributes = attribute.get("subattributes") or []
                attribute = subattributes[sub_index]
                if attribute.get("deleted"):
                    return None, None
                attribute_identifier = util.get_attribute_identifier(
                    attribute, attribute_identifier
                )
            return attribute, attribute_identifier
        except (IndexError, TypeError):
            return None, None

    def _getAttributeParentList(self, attributes, indexes):
        if len(indexes) <= 1:
            return attributes, None, None

        parent_attribute, parent_identifier = self._getAttributeAtPath(
            attributes, indexes[:-1]
        )
        if parent_attribute is None:
            return None, None, None
        parent_list = parent_attribute.setdefault("subattributes", [])

        return parent_list, parent_attribute, parent_identifier

    def _markAttributePathEdited(self, attributes, indexes, current_time, user):
        for path_length in range(1, len(indexes) + 1):
            attribute, _ = self._getAttributeAtPath(attributes, indexes[:path_length])
            if attribute is None:
                return False
            attribute["lastUpdated"] = current_time
            attribute["lastUpdatedUser"] = user
            attribute["edited"] = True
        return True

    def _updateRecordAttributesForFieldOperation(
        self,
        _id,
        new_data,
        update_type,
        user=None,
        record_doc=None,
    ):
        record_doc = record_doc or self.db.records.find_one(
            {"_id": _id}, {"attributesList": 1}
        )
        if not record_doc:
            _log.info("record lookup returned no document for field operation")
            return False

        attributes = util.normalize_record_attribute_tree(
            record_doc.get("attributesList") or []
        )
        fieldID = new_data.get("fieldID") or {}
        key = fieldID.get("key")
        field_indexes = self._getFieldIndexes(new_data)
        if len(field_indexes) == 0:
            _log.info("field operation missing indexes")
            return False

        if update_type == "insertField":
            if field_indexes[-1] < -1:
                _log.info("insertField received an invalid negative index")
                return False

            if len(field_indexes) == 1:
                newIndex = field_indexes[0] + 1
                attributes.insert(
                    newIndex,
                    self._createEmptyRecordAttribute(key, is_subattribute=False),
                )
            else:
                parent_list, _, parent_identifier = self._getAttributeParentList(
                    attributes, field_indexes
                )
                if parent_list is None:
                    _log.info("insertField could not locate parent attribute")
                    return False

                newSubIndex = max(field_indexes[-1] + 1, 0)
                top_level_attribute = attributes[field_indexes[0]].get("key")
                parent_attribute = (
                    parent_identifier
                    or new_data.get("parentAttribute")
                    or top_level_attribute
                )
                parent_list.insert(
                    newSubIndex,
                    self._createEmptyRecordAttribute(
                        key,
                        is_subattribute=True,
                        top_level_attribute=top_level_attribute,
                        parent_attribute=parent_attribute,
                    ),
                )
        elif update_type == "deleteField":
            if field_indexes[-1] < 0:
                _log.info("deleteField received an invalid negative index")
                return False

            if len(field_indexes) == 1:
                if field_indexes[0] >= len(attributes):
                    _log.info("deleteField top-level index is out of range")
                    return False
                if not attributes[field_indexes[0]].get("user_added", False):
                    raise PermissionError(
                        "Only manually added record attributes can be deleted."
                    )
                del attributes[field_indexes[0]]
            else:
                parent_list, _, _ = self._getAttributeParentList(
                    attributes, field_indexes
                )
                if parent_list is None:
                    _log.info("deleteField could not locate parent attribute")
                    return False
                if field_indexes[-1] >= len(parent_list):
                    _log.info("deleteField subattribute index is out of range")
                    return False
                if not parent_list[field_indexes[-1]].get("user_added", False):
                    raise PermissionError(
                        "Only manually added record attributes can be deleted."
                    )
                del parent_list[field_indexes[-1]]
        elif update_type == "updateFieldCoordinates":
            current_time = time.time()
            new_coordinates = new_data.get("new_coordinates")
            pageNumber = new_data.get("pageNumber")
            if field_indexes[-1] < 0:
                _log.info("updateFieldCoordinates received an invalid negative index")
                return False

            target_attribute, _ = self._getAttributeAtPath(attributes, field_indexes)
            if target_attribute is None:
                _log.info("updateFieldCoordinates could not locate target attribute")
                return False

            target_attribute["user_provided_coordinates"] = new_coordinates
            target_attribute["page"] = pageNumber
            self._markAttributePathEdited(attributes, field_indexes, current_time, user)
        else:
            _log.info(f"unsupported field operation update type: {update_type}")
            return False

        util.normalize_record_attribute_tree(attributes)
        return {"attributesList": attributes}

    def ensureDefaultUnauthenticatedTeam(self):
        if REQUIRE_AUTH:
            return

        query = {"name": DEFAULT_UNAUTHENTICATED_TEAM["name"]}
        update = {"$setOnInsert": DEFAULT_UNAUTHENTICATED_TEAM.copy()}
        result = self.db.teams.update_one(query, update, upsert=True)
        if result.upserted_id:
            _log.info("created default unauthenticated team")

    def getDefaultTeamForUser(self, email, anonymous_team=None):
        if not REQUIRE_AUTH and email == "anonymous":
            return anonymous_team or DEFAULT_UNAUTHENTICATED_TEAM["name"]

        user_document = self.getDocument("users", ({"email": email}))
        if user_document is None:
            return None
        return user_document.get("default_team", None)

    def getMongoProcessorByID(self, google_id):
        if not google_id:
            return None
        processor = self._findUniqueProcessor(
            {"processorId": google_id}, required=False
        )
        return self._serializeSchema(processor) if processor else None

    @staticmethod
    def _serializeSchema(processor):
        result = copy.deepcopy(processor)
        if "_id" in result:
            result["schema_id"] = str(result.pop("_id"))
        result["can_process"] = bool(
            result.get("processorId") and result.get("modelId")
        )
        return result

    def _schemaDocument(self, schema_id=None, name=None):
        if schema_id is not None:
            if not isinstance(schema_id, str) or not ObjectId.is_valid(schema_id):
                raise schema_rules.SchemaLookupError("A valid schema_id is required.")
            return self._findUniqueProcessor({"_id": ObjectId(schema_id)})
        if not isinstance(name, str) or not name:
            raise schema_rules.SchemaError("A schema identifier is required.")
        return self._findUniqueProcessor({"name": name})

    @staticmethod
    def _canonicalRepoProcessor(definition):
        if not definition:
            return None
        return {
            **definition,
            "name": definition.get("name") or definition.get("Processor Name"),
            "displayName": definition.get("displayName")
            or definition.get("Processor Name"),
            "processorId": definition.get("processorId")
            or definition.get("Processor ID"),
            "modelId": definition.get("modelId") or definition.get("Model ID"),
            "documentType": definition.get("documentType")
            or definition.get("Processor Name"),
        }

    def resolveRecordGroupSchema(self, group, user=None, *, strict=False):
        """Existing data remains usable when its stored schema cannot be resolved."""
        try:
            return self._resolveRecordGroupSchema(group, user)
        except schema_rules.SchemaLookupError as error:
            if strict:
                raise
            _log.warning(
                "Record group %s is using no schema: %s", group.get("_id"), error
            )
            return None

    def _resolveRecordGroupSchema(self, group, user=None):
        """Resolve one active source. An explicit null binding never uses legacy fallback."""
        if USE_DB_PROCESSORS:
            if "schema_id" in group:
                return (
                    self._schemaDocument(group["schema_id"])
                    if group["schema_id"] is not None
                    else None
                )
            if group.get("processorId"):
                schema = self._findUniqueProcessor(
                    {"processorId": group["processorId"]}, required=False
                )
                if schema is None:
                    raise schema_rules.SchemaLookupError(
                        "The record group's schema is missing. Select a schema or detach the group.",
                        409,
                    )
                return schema
            if group.get("attributes"):
                raise schema_rules.SchemaLookupError(
                    "This record group has a legacy embedded schema. Migrate its schema binding to use the schema.",
                    409,
                )
            return None
        processor_id = group.get("processorId")
        if not processor_id:
            return None
        schema = self.getProcessorById(processor_id, user)
        if schema is None:
            raise schema_rules.SchemaLookupError(
                "The configured processor is missing from the installed package.", 409
            )
        return self._canonicalRepoProcessor(schema)

    def getRecordGroupProcessingConfig(self, rg_id, user=None):
        group = self.db.record_groups.find_one({"_id": ObjectId(rg_id)})
        if group is None:
            raise schema_rules.SchemaError("Record group not found.", 404)
        schema = self.resolveRecordGroupSchema(group, user, strict=True)
        if not schema or not all(
            isinstance(schema.get(key), str) and schema[key].strip()
            for key in ("processorId", "modelId")
        ):
            raise schema_rules.SchemaError(
                "This record group's schema has no usable processor. Add a processor ID and model ID before processing documents.",
                409,
            )
        parser_type = schema.get("parser_type") or (
            "form_parser"
            if schema["modelId"].startswith("pretrained-form-parser")
            else "custom"
        )
        return {
            "processor_id": schema["processorId"],
            "model_id": schema["modelId"],
            "processor_attributes": schema_rules.normalize_fields(
                schema.get("attributes") or [], strict=False
            ),
            "using_default_processor": parser_type == "form_parser",
        }

    def _recordGroupSchemaInfo(self, group, user=None):
        result = dict(group)
        result["schema_source"] = "database" if USE_DB_PROCESSORS else "repo"
        try:
            schema = self.resolveRecordGroupSchema(group, user, strict=True)
            result.update(
                has_schema=schema is not None
                and (USE_DB_PROCESSORS or "attributes" in schema),
                can_process=bool(
                    schema and schema.get("processorId") and schema.get("modelId")
                ),
                schema_name=(schema or {}).get("displayName")
                or (schema or {}).get("name"),
                active_schema_id=str(schema["_id"])
                if schema and "_id" in schema
                else None,
                schema_error=None,
            )
        except schema_rules.SchemaLookupError as error:
            result.update(
                has_schema=False,
                can_process=False,
                schema_name=None,
                active_schema_id=None,
                schema_error=str(error),
            )
        return result

    def _normalizeCollaborator(self, collaborator):
        if not isinstance(collaborator, str):
            return None
        collaborator = collaborator.strip()

        ## There WAS a naming convention disparity between OGRRE (rrc) and OGRRE data cleaning (texas_rrc), which is now fixed. leave this in as a fallback for now
        if collaborator == "texas_rrc":
            collaborator = "rrc"
        return collaborator or None

    def getCollaboratorForUser(self, user=None):
        collaborator = None
        email = None

        if isinstance(user, dict):
            collaborator = self._normalizeCollaborator(user.get("collaborator"))
            email = user.get("email")
        elif isinstance(user, str):
            email = user

        if collaborator:
            return collaborator

        if REQUIRE_AUTH and email and email != "anonymous":
            user_document = self.getDocument("users", {"email": email})
            if user_document is not None:
                collaborator = self._normalizeCollaborator(
                    user_document.get("collaborator")
                )
                if collaborator:
                    return collaborator

        return self._normalizeCollaborator(self.collaborator)

    def getProcessorById(self, google_id=None, user=None):
        if USE_DB_PROCESSORS:
            processor = self.getMongoProcessorByID(google_id=google_id)
        else:
            collaborator = self.getCollaboratorForUser(user)
            processor = processor_api.get_processor_by_id(collaborator, google_id)
            if not processor:
                processor = next(
                    (
                        item
                        for item in DEFAULT_PROCESSORS
                        if item["Processor ID"] == google_id
                    ),
                    None,
                )
        return processor

    def getProcessorsByIds(self, google_ids=None, user=None):
        if USE_DB_PROCESSORS:
            processors = [
                self.getMongoProcessorByID(google_id) for google_id in google_ids or []
            ]
        else:
            collaborator = self.getCollaboratorForUser(user)
            processors = []
            for google_id in google_ids:
                processor = processor_api.get_processor_by_id(collaborator, google_id)
                processors.append(processor)
        return processors

    def createProcessorsListFromDB(self):
        return [self._serializeSchema(schema) for schema in self.db.processors.find({})]

    def createProcessorsList(self, user=None):
        if USE_DB_PROCESSORS:
            _log.info(f"creating processor list using db")
            processor_list = self.createProcessorsListFromDB()
        else:
            collaborator = self.getCollaboratorForUser(user)
            _log.info(f"creating processor list using processor_api for {collaborator}")
            processor_list = processor_api.get_processor_list(collaborator)

        if not processor_list and not USE_DB_PROCESSORS:
            _log.info(f"no processors found, using default extractor")
            processor_list = DEFAULT_PROCESSORS
        return processor_list

    ## lock functions
    def lockRecord(self, record_id, user, release_previous_record=True):
        _log.info(f"{user} locking {record_id}")
        if release_previous_record:
            ## remove any record locks that this user may already have in place
            self.releaseRecord(user=user)
        query = {"record_id": record_id}
        data = {
            "user": user,
            "record_id": record_id,
            "timestamp": time.time(),
        }
        self.db.locked_records.update_one(query, {"$set": data}, upsert=True)

    def releaseRecord(self, record_id=None, user=None):
        _log.info(f"releasing record {record_id} or user {user}")
        if record_id:
            self.db.locked_records.delete_many({"record_id": record_id})
        elif user:
            self.db.locked_records.delete_many({"user": user})

    @time_it
    def tryLockingRecord(self, record_id, user):
        try:
            attained_lock = False
            locked_record_cursor = self.db.locked_records.find({"record_id": record_id})
            record_is_locked = False
            for locked_record_document in locked_record_cursor:
                record_is_locked = True
                break
            locked_record_cursor.close()
            _log.info(f"record_is_locked: {record_is_locked}")
            if record_is_locked:
                ## someone has a lock for this.
                ## (1) check who
                ## (2) check if expired
                locked_time = locked_record_document.get("timestamp", 0)
                lockholder = locked_record_document.get("user", None)
                current_time = time.time()
                if lockholder == user:
                    self.lockRecord(
                        record_id=record_id, user=user, release_previous_record=False
                    )
                    attained_lock = True
                elif locked_time + self.lock_duration < current_time:
                    ## lock is expired
                    self.lockRecord(
                        record_id=record_id, user=user, release_previous_record=True
                    )
                    attained_lock = True
                else:
                    ## lock is still valid by other user
                    attained_lock = False
            else:
                ## record is unlocked, go on ahead
                self.lockRecord(
                    record_id=record_id, user=user, release_previous_record=True
                )
                attained_lock = True
            return attained_lock
        except Exception as e:
            _log.error(f"error trying to lock record: {e}")
            return False

    def requireSchemaPermission(self, user_info, destructive=False, require_db=True):
        if require_db and not USE_DB_PROCESSORS:
            raise schema_rules.SchemaError(
                "Repo schemas are read-only. Enable DB schema mode to manage schemas.",
                409,
            )
        email = (user_info or {}).get("email")
        if not self.hasPermission(email, "manage_schema"):
            raise PermissionError("You are not authorized to manage schemas.")
        if destructive and (
            not REQUIRE_AUTH
            or (user_info or {}).get("anonymous")
            or not self.hasPermission(email, schema_rules.DESTRUCTIVE_PERMISSION)
        ):
            raise PermissionError(
                "This schema change requires manage_schema_destructive permission."
            )

    @contextmanager
    def _schemaCatalogWrite(self, import_id=None):
        """Serialize catalog/binding writes across API processes on standalone Mongo.

        No lease expiry: a slow writer must never lose ownership mid-write. After
        a process crash, operators stop writers before clearing the owner (README).
        A partial import retains pending_import until its saved steps finish.
        """
        if not USE_DB_PROCESSORS or _catalog_writer.get() is self:
            yield
            return
        guards = self.db.schema_catalog_guard
        owner = str(uuid.uuid4())
        try:
            guard = guards.find_one_and_update(
                {
                    "_id": "catalog",
                    "owner": None,
                    "pending_import": {"$in": [None, import_id]},
                },
                {
                    "$set": {"owner": owner, "started_at": time.time()},
                    "$setOnInsert": {"pending_import": None},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # A busy guard does not match; its unique _id prevents a second owner.
            guard = None
        if not guard:
            raise schema_rules.SchemaError(
                "Schema changes are busy or an import needs to be resumed. Open Import repo schemas to check its status.",
                409,
            )
        token = _catalog_writer.set(self)
        try:
            yield
        finally:
            _catalog_writer.reset(token)
            guards.update_one(
                {"_id": "catalog", "owner": owner}, {"$set": {"owner": None}}
            )

    def _installedSchemaPackage(self, user_info):
        collaborator = self.getCollaboratorForUser(user_info)
        metadata = processor_api.get_processor_list(collaborator)
        if metadata is None:
            raise schema_rules.SchemaError(
                "No schema catalog was found for this collaborator in the installed package.",
                404,
            )
        if not isinstance(metadata, list) or len(metadata) > 250:
            raise schema_rules.SchemaError(
                "The installed package schema catalog is invalid or exceeds 250 schemas."
            )
        try:
            package_version = version("ogrre_data_cleaning")
        except PackageNotFoundError:
            package_version = "unknown"
        schemas, seen = [], set()
        for item in metadata:
            name = item.get("Processor Name") if isinstance(item, dict) else None
            if not isinstance(name, str) or not name or name in seen:
                raise schema_rules.SchemaError(
                    "The installed package contains missing or duplicate schema names."
                )
            seen.add(name)
            result = {"source_id": name, "name": item.get("displayName") or name}
            try:
                definition = processor_api.get_processor_by_name(collaborator, name)
                if not definition or "attributes" not in definition:
                    raise schema_rules.SchemaError(
                        "The package schema file is missing; it cannot be imported as an empty schema."
                    )
                canonical = self._canonicalRepoProcessor(definition)
                fields = schema_rules.validate_fields(
                    canonical["attributes"], util.CLEANING_FUNCTIONS
                )
                normalized = {
                    key: canonical.get(key) for key in ("name", *schema_import.METADATA)
                }
                self._checkProcessorMetadata(normalized)
                result.update(
                    definition={**normalized, "attributes": fields},
                    field_count=len(fields),
                )
            except schema_rules.SchemaError as error:
                result["error"] = str(error)
            schemas.append(result)
        return {
            "source": {
                "package": "ogrre_data_cleaning",
                "version": package_version,
                "collaborator": collaborator,
            },
            "schemas": schemas,
        }

    def _schemaImportState(self):
        return {
            "catalog": list(self.db.processors.find({}).sort("_id", 1)),
            "groups": [
                schema_import.group_snapshot(group)
                for group in self.db.record_groups.find(
                    {}, {key: 1 for key in schema_import.GROUP_FIELDS}
                ).sort("_id", 1)
            ],
        }

    @staticmethod
    def _publicSchemaImport(job):
        return {
            key: copy.deepcopy(value)
            for key, value in job.items()
            if key
            not in {
                "_id",
                "operations",
                "snapshot",
                "package_digest",
                "request",
                "creator",
            }
        }

    def getRepoSchemaImport(self, user_info):
        self.requireSchemaPermission(user_info)
        guard = self.db.schema_catalog_guard.find_one({"_id": "catalog"}) or {}
        pending = (
            self.db.schema_imports.find_one({"_id": guard.get("pending_import")})
            if guard.get("pending_import")
            else None
        )
        # A saved import can be resumed even if the installed package changes.
        try:
            package = self._installedSchemaPackage(user_info)
        except schema_rules.SchemaError as error:
            if not pending:
                raise
            package = {"source": pending["source"], "schemas": [], "error": str(error)}
        return {
            **package,
            "schemas": [
                {key: value for key, value in item.items() if key != "definition"}
                for item in package["schemas"]
            ],
            "pending_import": self._publicSchemaImport(pending) if pending else None,
            "busy": bool(guard.get("owner")),
        }

    def previewRepoSchemaImport(self, request, user_info):
        if not isinstance(request, dict) or set(request) - {
            "mode",
            "selected",
            "decisions",
        }:
            raise schema_rules.SchemaError("Invalid schema import request.")
        self.requireSchemaPermission(
            user_info, destructive=request.get("mode") == "replace"
        )
        package = self._installedSchemaPackage(user_info)
        with self._schemaCatalogWrite():
            state = self._schemaImportState()
            now = time.time()
            creator = self._schemaCreator(user_info)
            plan = schema_import.build_plan(
                package, state["catalog"], state["groups"], request, creator, now
            )
            self.requireSchemaPermission(user_info, destructive=plan["destructive"])
            import_id = str(ObjectId())
            job = {
                **plan,
                "_id": import_id,
                "import_id": import_id,
                "status": "preview",
                "next_step": 0,
                "total_steps": len(plan["operations"]),
                "created_by": user_info.get("email"),
                "created_by_team": creator["created_by_team"],
                "created_at": now,
                "expires_at": now + 1800,
                "snapshot": self._attributeDigest(state),
                "package_digest": self._attributeDigest(package),
                "request": copy.deepcopy(request),
                "creator": creator,
            }
            if len(BSON.encode(job)) > 12 * 1024 * 1024:
                raise schema_rules.SchemaError(
                    "This import preview is too large. Import a smaller selection."
                )
            self.db.schema_imports.insert_one(job)
            return self._publicSchemaImport(job)

    def _applySchemaImportStep(self, operation, job, user_info):
        kind = operation["kind"]
        before, after = operation.get("before"), operation.get("after")
        if kind == "reconcile":
            self._ensureRecordGroupsReconciled([operation["group_id"]], user_info)
            return
        if kind == "binding":
            group = self.db.record_groups.find_one({"_id": before["_id"]})
            if group is None:
                return  # A concurrently deleted group needs no binding update.
            current = schema_import.group_snapshot(group)
            if current == after:
                return
            if current != before:
                raise schema_rules.SchemaError(
                    "A record-group binding changed during import.", 409
                )
            self.db.record_groups.update_one(
                {"_id": before["_id"]}, {"$set": {"schema_id": after["schema_id"]}}
            )
        elif kind == "schema":
            current = self.db.processors.find_one({"_id": after["_id"]})
            if current == after:
                return
            if current != before:
                raise schema_rules.SchemaError("A schema changed during import.", 409)
            if before is None:
                self.db.processors.insert_one(after)
            else:
                self.db.processors.replace_one({"_id": before["_id"]}, after)
        elif kind == "remove":
            current = self.db.processors.find_one({"_id": before["_id"]})
            if current is None:
                return
            if current != before or self._groupsUsingSchema(before):
                raise schema_rules.SchemaError(
                    "A schema or its record-group bindings changed during import.", 409
                )
            self.db.deleted_processors.replace_one(
                {"_id": before["_id"]},
                {
                    **before,
                    "deleted_by": user_info.get("email"),
                    "deleted_at": time.time(),
                    "schema_import_id": job["import_id"],
                },
                upsert=True,
            )
            self.db.processors.delete_one(before)

    def applyRepoSchemaImport(self, request, user_info):
        self.requireSchemaPermission(user_info)
        if (
            not isinstance(request, dict)
            or set(request) != {"import_id"}
            or not isinstance(request["import_id"], str)
        ):
            raise schema_rules.SchemaError("Provide the saved import preview.")
        import_id = request["import_id"]
        job = self.db.schema_imports.find_one({"_id": import_id})
        if not job:
            raise schema_rules.SchemaError("Import preview not found.", 404)
        self.requireSchemaPermission(
            user_info,
            destructive=job["destructive"]
            or job["created_by"] != user_info.get("email"),
        )
        if job["status"] == "complete":
            guard = self.db.schema_catalog_guard.find_one({"_id": "catalog"}) or {}
            if guard.get("pending_import") == import_id:
                with self._schemaCatalogWrite(import_id):
                    self.db.schema_catalog_guard.update_one(
                        {"_id": "catalog", "pending_import": import_id},
                        {"$set": {"pending_import": None}},
                    )
            return self._publicSchemaImport(job)
        with self._schemaCatalogWrite(import_id):
            job = self.db.schema_imports.find_one({"_id": import_id})
            if job["status"] == "complete":
                self.db.schema_catalog_guard.update_one(
                    {"_id": "catalog", "pending_import": import_id},
                    {"$set": {"pending_import": None}},
                )
                return self._publicSchemaImport(job)
            if not job["can_apply"]:
                raise schema_rules.SchemaError(
                    "Resolve all import conflicts before applying.", 409
                )
            if job["status"] == "preview":
                if (
                    job["expires_at"] < time.time()
                    or job["snapshot"]
                    != self._attributeDigest(self._schemaImportState())
                    or job["package_digest"]
                    != self._attributeDigest(self._installedSchemaPackage(user_info))
                ):
                    raise schema_rules.SchemaError(
                        "The catalog, groups, or installed package changed, or the preview expired. Review a new preview.",
                        409,
                    )
                now = time.time()
                for operation in job["operations"]:
                    if operation["kind"] == "schema":
                        operation["after"].update(
                            imported_at=now, updated_at=now, lastUpdated=now
                        )
                        if operation["before"] is None:
                            operation["after"]["created_at"] = now
            self.db.schema_catalog_guard.update_one(
                {"_id": "catalog"}, {"$set": {"pending_import": import_id}}
            )
            self.db.schema_imports.update_one(
                {"_id": import_id},
                {
                    "$set": {
                        "status": "applying",
                        "operations": job["operations"],
                        "last_actor": user_info.get("email"),
                        "error": None,
                    }
                },
            )
            try:
                for index in range(job["next_step"], len(job["operations"])):
                    operation = job["operations"][index]
                    self._applySchemaImportStep(operation, job, user_info)
                    self.db.history.update_one(
                        {"_id": f"schema-import:{import_id}:{index}"},
                        {
                            "$setOnInsert": {
                                "action": "importRepoSchemaStep",
                                "user": user_info.get("email"),
                                "timestamp": time.time(),
                                "schema_import_id": import_id,
                                "record_group_id": str(operation["before"]["_id"])
                                if operation["kind"] == "binding"
                                else operation.get("group_id"),
                                "source": job["source"],
                                "step": index,
                                "previous_state": operation.get("before"),
                                "query": operation.get("after"),
                                "notes": operation["kind"],
                            }
                        },
                        upsert=True,
                    )
                    self.db.schema_imports.update_one(
                        {"_id": import_id}, {"$set": {"next_step": index + 1}}
                    )
                self.recordHistory(
                    "importRepoSchemas",
                    user_info.get("email"),
                    query={"import_id": import_id, "counts": job["counts"]},
                    notes={
                        "source": job["source"],
                        "decisions": job["request"],
                        "team": job["created_by_team"],
                    },
                )
                self.db.schema_imports.update_one(
                    {"_id": import_id},
                    {
                        "$set": {
                            "status": "complete",
                            "completed_at": time.time(),
                            "error": None,
                        }
                    },
                )
                self.db.schema_catalog_guard.update_one(
                    {"_id": "catalog", "pending_import": import_id},
                    {"$set": {"pending_import": None}},
                )
            except Exception:
                _log.exception(
                    "Schema import %s paused; its saved steps can be resumed", import_id
                )
                self.db.schema_imports.update_one(
                    {"_id": import_id},
                    {
                        "$set": {
                            "status": "partial",
                            "error": "Import paused before completion. Applied changes are saved. Retry to resume the remaining steps; schema changes are held until it finishes.",
                        }
                    },
                )
            return self._publicSchemaImport(
                self.db.schema_imports.find_one({"_id": import_id})
            )

    def _findUniqueProcessor(self, query, required=True):
        matches = list(self.db.processors.find(query).limit(2))
        if len(matches) > 1:
            raise schema_rules.SchemaLookupError(
                "Multiple schemas match this identifier. Resolve the duplicate schemas first.",
                409,
            )
        if not matches:
            if required:
                raise schema_rules.SchemaLookupError("Schema not found.", 404)
            return None
        return matches[0]

    def _checkProcessorMetadata(self, metadata, existing=None):
        allowed = {
            "name",
            "displayName",
            "processorId",
            "modelId",
            "documentType",
            "img",
            "parser_type",
        }
        if not isinstance(metadata, dict) or set(metadata) - allowed:
            raise schema_rules.SchemaError("Unsupported processor metadata fields.")
        for key, value in metadata.items():
            if value is not None and not isinstance(value, str):
                raise schema_rules.SchemaError(f"{key} must be a string.")
            if key in {"processorId", "modelId"} and value and value != value.strip():
                raise schema_rules.SchemaError(
                    f"{key} cannot contain surrounding whitespace."
                )
        for key in ("name", "documentType"):
            if not isinstance(metadata.get(key), str) or not metadata[key].strip():
                raise schema_rules.SchemaError(f"{key} is required.")
        if metadata.get("parser_type") not in (None, "custom", "form_parser"):
            raise schema_rules.SchemaError("parser_type must be custom or form_parser.")

    def _schemaCreator(self, user_info):
        now = time.time()
        return {
            "created_by": user_info.get("email"),
            "created_by_team": self.getDefaultTeamForUser(
                user_info.get("email"), user_info.get("default_team")
            ),
            "created_at": now,
            "updated_at": now,
            "lastUpdated": now,
        }

    @schema_import.catalog_write
    def createSchema(self, data, user_info):
        self.requireSchemaPermission(user_info)
        if not isinstance(data, dict):
            raise schema_rules.SchemaError("Schema data must be an object.")
        metadata = {key: value for key, value in data.items() if key != "attributes"}
        self._checkProcessorMetadata(metadata)
        attributes = schema_rules.validate_fields(
            data.get("attributes", []), util.CLEANING_FUNCTIONS, require_types=False
        )
        schema = {
            **metadata,
            "attributes": attributes,
            **self._schemaCreator(user_info),
        }
        result = self.db.processors.update_one(
            {"name": metadata["name"]}, {"$setOnInsert": schema}, upsert=True
        )
        if not result.upserted_id:
            raise schema_rules.SchemaError(
                "A schema with this name already exists.", 409
            )
        self.recordHistory(
            "createSchema",
            user_info.get("email"),
            query={**schema, "schema_id": str(result.upserted_id)},
        )
        return self._serializeSchema({**schema, "_id": result.upserted_id})

    def _groupsUsingSchema(self, schema):
        clauses = [{"schema_id": str(schema["_id"])}]
        if schema.get("processorId"):
            clauses.append(
                {"schema_id": {"$exists": False}, "processorId": schema["processorId"]}
            )
        return list(self.db.record_groups.find({"$or": clauses}))

    def _schemaGenerationGroup(self, rg_id, user_info):
        self.requireSchemaPermission(user_info)
        if not isinstance(rg_id, str) or not ObjectId.is_valid(rg_id):
            raise schema_rules.SchemaError("A valid record-group ID is required.")
        if rg_id not in self.getUserRecordGroups(user_info):
            raise PermissionError("You do not have access to this record group.")
        group = self.db.record_groups.find_one({"_id": ObjectId(rg_id)})
        if group is None:
            raise schema_rules.SchemaError("Record group not found.", 404)
        return group

    def _sampleSchemaRecords(self, rg_id, limit):
        records, examined, size, skipped, capped = [], 0, 0, 0, False
        try:
            cursor = self.db.records.aggregate(
                schema_inference.sample_pipeline(rg_id, limit),
                hint=schema_inference.INDEX,
                maxTimeMS=5000,
                batchSize=10,
            )
            try:
                for record in cursor:
                    if examined == limit:
                        capped = True
                        break
                    examined += 1
                    if record["size"] > schema_inference.MAX_RECORD_BYTES:
                        skipped += 1
                        continue
                    if size + record["size"] > schema_inference.MAX_SAMPLE_BYTES:
                        capped = True
                        break
                    size += record["size"]
                    records.append(record)
            finally:
                cursor.close()
        except ExecutionTimeout:
            raise schema_rules.SchemaError(
                "Sampling timed out. Retry with a smaller record limit.", 503
            )
        return records, {
            "sampled_records": len(records),
            "examined_records": examined,
            "record_limit": limit,
            "sample_capped": capped,
            "oversized_records": skipped,
        }

    def previewRecordGroupSchema(self, rg_id, request, user_info):
        group = self._schemaGenerationGroup(rg_id, user_info)
        if not isinstance(request, dict) or set(request) - {"mode", "record_limit"}:
            raise schema_rules.SchemaError("Invalid schema generation request.")
        mode = request.get("mode", "generate")
        if not isinstance(mode, str) or mode not in {"generate", "extend"}:
            raise schema_rules.SchemaError("Choose generate or extend.")
        schema = self.resolveRecordGroupSchema(group, user_info, strict=True)
        if (mode == "generate" and schema is not None) or (
            mode == "extend" and schema is None
        ):
            raise schema_rules.SchemaError(
                "The group's schema changed. Reload the record group.", 409
            )
        maximum = max(
            1, min(10000, int(os.getenv("SCHEMA_INFERENCE_MAX_RECORDS", "1000")))
        )
        limit = request.get("record_limit", maximum)
        if type(limit) is not int or not 1 <= limit <= maximum:
            raise schema_rules.SchemaError(
                f"record_limit must be between 1 and {maximum}."
            )
        records, coverage = self._sampleSchemaRecords(rg_id, limit)
        if not coverage["examined_records"]:
            raise schema_rules.SchemaError("Add records before generating a schema.")
        fields, notes, warnings = schema_inference.infer_fields(
            records, (schema or {}).get("attributes") or []
        )
        if coverage["sample_capped"]:
            warnings.append(
                "This preview covers a bounded sample; other fields may exist outside it."
            )
        if coverage["oversized_records"]:
            warnings.append(
                "Oversized records were skipped. Their stored fields remain unchanged."
            )
        warnings.append(
            "Fields absent from stored attributes, including discarded blank CSV columns, cannot be inferred."
        )
        preview_id = str(ObjectId())
        preview = {
            "_id": preview_id,
            "preview_id": preview_id,
            "mode": mode,
            "status": "preview",
            "record_group_id": rg_id,
            "created_by": user_info.get("email"),
            "created_at": time.time(),
            "expires_at": time.time() + 1800,
            "group_snapshot": schema_import.group_snapshot(group),
            "schema_snapshot": schema,
            "sample_digest": self._attributeDigest(records),
            "schema_id": str(schema["_id"]) if schema else None,
            "schema_name": (schema or {}).get("displayName")
            or (schema or {}).get("name"),
            "name": f"{group.get('name') or 'Imported records'} schema",
            "documentType": group.get("documentType") or "Imported records",
            "fields": fields,
            "field_notes": notes,
            "warnings": warnings,
            **coverage,
        }
        if len(BSON.encode(preview)) > schema_inference.MAX_PAYLOAD_BYTES:
            raise schema_rules.SchemaError(
                "This schema preview is too large. Use a smaller record limit."
            )
        self.db.schema_generations.insert_one(preview)
        return self._publicSchemaGeneration(preview)

    @staticmethod
    def _publicSchemaGeneration(preview):
        return {
            key: value
            for key, value in preview.items()
            if key
            not in {
                "_id",
                "group_snapshot",
                "schema_snapshot",
                "sample_digest",
                "plan",
                "request_digest",
            }
        }

    @schema_import.catalog_write
    def applyRecordGroupSchema(self, rg_id, request, user_info):
        group = self._schemaGenerationGroup(rg_id, user_info)
        if not isinstance(request, dict) or set(request) - {
            "preview_id",
            "fields",
            "name",
            "documentType",
        }:
            raise schema_rules.SchemaError("Invalid schema generation request.")
        if not isinstance(request.get("preview_id"), str):
            raise schema_rules.SchemaError("Provide a saved preview.")
        preview = self.db.schema_generations.find_one(
            {"_id": request["preview_id"], "record_group_id": rg_id}
        )
        if preview is None:
            raise schema_rules.SchemaError("Schema preview not found.", 404)
        if preview["created_by"] != user_info.get("email"):
            raise PermissionError("Generate your own preview before applying changes.")
        request_digest = self._attributeDigest(request)
        if (
            preview["status"] != "preview"
            and request_digest != preview["request_digest"]
        ):
            raise schema_rules.SchemaError(
                "This preview was already submitted with different changes. Reload the group.",
                409,
            )
        if preview["status"] == "preview":
            current_schema = self.resolveRecordGroupSchema(
                group, user_info, strict=True
            )
            if (
                preview["expires_at"] < time.time()
                or schema_import.group_snapshot(group) != preview["group_snapshot"]
                or current_schema != preview["schema_snapshot"]
            ):
                raise schema_rules.SchemaError(
                    "The group or schema changed, or the preview expired. Generate a new preview.",
                    409,
                )
            records, coverage = self._sampleSchemaRecords(
                rg_id, preview["record_limit"]
            )
            if (
                self._attributeDigest(records) != preview["sample_digest"]
                or not records
            ):
                raise schema_rules.SchemaError(
                    "The sampled records changed. Generate a new preview.", 409
                )
            submitted = request.get("fields")
            if (
                not isinstance(submitted, list)
                or not 0 < len(submitted) <= schema_inference.MAX_FIELDS
            ):
                raise schema_rules.SchemaError("Provide the preview's fields to save.")
            expected = {field["name"]: field for field in preview["fields"]}
            if any(
                not isinstance(field, dict)
                or not isinstance(field.get("name"), str)
                or field.get("name") not in expected
                or set(field)
                - (schema_rules.SAFE_FIELD_UPDATES | {"name", "occurrence"})
                for field in submitted
            ):
                raise schema_rules.SchemaError(
                    "Only the suggested fields may be edited and added."
                )
            fields = schema_rules.normalize_fields(submitted)
            for field in fields:
                schema_rules.validate_field(field, util.CLEANING_FUNCTIONS)
            if {field["name"] for field in fields} != set(expected):
                raise schema_rules.SchemaError(
                    "Field names in the preview cannot be changed."
                )
            if current_schema:
                attributes = copy.deepcopy(current_schema.get("attributes") or [])
                schema_rules.validate_structure(
                    [
                        field
                        for field in schema_rules.normalize_fields(
                            attributes, strict=False
                        )
                        if not field.get("deleted")
                    ]
                    + fields
                )
                after = {
                    **current_schema,
                    "attributes": attributes + fields,
                    "updated_at": time.time(),
                    "lastUpdated": time.time(),
                }
            else:
                metadata = {
                    "name": request.get("name"),
                    "displayName": request.get("name"),
                    "documentType": request.get("documentType"),
                }
                self._checkProcessorMetadata(metadata)
                if self.db.processors.find_one({"name": metadata["name"]}):
                    raise schema_rules.SchemaError(
                        "A schema with this name already exists. Choose another name.",
                        409,
                    )
                schema_rules.validate_structure(fields)
                after = {
                    "_id": ObjectId(preview["preview_id"]),
                    **metadata,
                    "attributes": fields,
                    **self._schemaCreator(user_info),
                    "inference_source": {"record_group_id": rg_id, **coverage},
                }
            if len(BSON.encode(after)) > schema_inference.MAX_PAYLOAD_BYTES:
                raise schema_rules.SchemaError("The resulting schema is too large.")
            # Save the exact plan before either document write. Retries reuse its
            # schema ID and compare both original and already-applied states.
            preview.update(status="applying", request_digest=request_digest, plan=after)
            self.db.schema_generations.update_one(
                {"_id": preview["preview_id"]},
                {
                    "$set": {
                        "status": "applying",
                        "request_digest": request_digest,
                        "plan": after,
                    }
                },
            )
        if preview["status"] != "complete":
            after = preview["plan"]
            current_schema = self.db.processors.find_one({"_id": after["_id"]})
            before = preview["schema_snapshot"]
            before_group = preview["group_snapshot"]
            after_group = {**before_group, "schema_id": str(after["_id"])}
            if schema_import.group_snapshot(group) not in (before_group, after_group):
                raise schema_rules.SchemaError(
                    "The group's binding changed. Reload the group; any saved schema remains in the catalog.",
                    409,
                )
            if current_schema != after:
                if current_schema != before:
                    raise schema_rules.SchemaError(
                        "The schema changed. Reload the group before trying again.", 409
                    )
                if before is None:
                    if self.db.deleted_processors.find_one({"_id": after["_id"]}):
                        raise schema_rules.SchemaError(
                            "The generated schema was deleted. Generate a new preview.",
                            409,
                        )
                    if self.db.processors.find_one({"name": after["name"]}):
                        raise schema_rules.SchemaError(
                            "A schema with this name now exists. Reload the group.", 409
                        )
                    self.db.processors.insert_one(after)
                elif not self.db.processors.replace_one(before, after).matched_count:
                    raise schema_rules.SchemaError(
                        "The schema changed. Reload the group.", 409
                    )
            if preview["mode"] == "generate":
                query = {"_id": group["_id"]}
                for key in schema_import.GROUP_FIELDS:
                    query[key] = (
                        {"$eq": group[key], "$exists": True}
                        if key in group
                        else {"$exists": False}
                    )
                if not self.db.record_groups.update_one(
                    query, {"$set": {"schema_id": str(after["_id"])}}
                ).matched_count:
                    raise schema_rules.SchemaError(
                        "The record group changed. The generated schema is available in the catalog.",
                        409,
                    )
            self.db.history.update_one(
                {"_id": f"schema-generation:{preview['preview_id']}"},
                {
                    "$setOnInsert": {
                        "action": "generateSchema"
                        if preview["mode"] == "generate"
                        else "extendSchema",
                        "user": user_info.get("email"),
                        "timestamp": time.time(),
                        "record_group_id": rg_id,
                        "query": after,
                        "previous_state": before,
                        "notes": {
                            key: preview[key]
                            for key in (
                                "sampled_records",
                                "record_limit",
                                "sample_capped",
                                "oversized_records",
                            )
                        },
                    }
                },
                upsert=True,
            )
            self.db.schema_generations.update_one(
                {"_id": preview["preview_id"]}, {"$set": {"status": "complete"}}
            )
        group = self.db.record_groups.find_one({"_id": group["_id"]})
        result = self._recordGroupSchemaInfo(group, user_info)
        result["_id"] = str(result["_id"])
        result["has_records"] = True
        return result

    def _saveProcessorChanges(self, processor, changes, user_info, action):
        retirement_groups = []
        if "processorId" in changes and any(
            "schema_id" not in group for group in self._groupsUsingSchema(processor)
        ):
            raise schema_rules.SchemaError(
                "Migrate this schema's legacy record-group bindings before changing its processor ID.",
                409,
            )
        if "attributes" in changes:
            retired = {
                field["name"]
                for field in schema_rules.normalize_fields(
                    processor.get("attributes") or [], strict=False
                )
                if field.get("deleted")
            }
            active = {
                field["name"]
                for field in changes["attributes"]
                if not field.get("deleted")
            }
            affected = None
            if retired & active:
                # Materialize old retirement before reintroducing its definition.
                affected = [
                    str(group["_id"]) for group in self._groupsUsingSchema(processor)
                ]
                self._ensureRecordGroupsReconciled(affected, user_info)
            changes["attributes"] = schema_rules.retain_retired_fields(
                processor.get("attributes"), changes["attributes"]
            )
            newly_retired = {
                field["name"] for field in changes["attributes"] if field.get("deleted")
            } - retired
            if newly_retired:
                # Finish retirement in the explicit schema operation, never on
                # the next page read. The catalog guard protects the definition
                # until the conditional record writes complete.
                if affected is None:
                    affected = [
                        str(group["_id"])
                        for group in self._groupsUsingSchema(processor)
                    ]
                self._ensureRecordGroupsReconciled(
                    affected,
                    user_info,
                    schema_state=self._schemaState({**processor, **changes}),
                )
                retirement_groups = affected
        # Compare the original state so simultaneous edits cannot overwrite one another.
        result = self.db.processors.update_one(
            processor,
            {
                "$set": {
                    **changes,
                    "lastUpdated": time.time(),
                    "updated_at": time.time(),
                }
            },
        )
        if not result.matched_count:
            raise schema_rules.SchemaError(
                "The schema changed. Reload it before saving again.", 409
            )
        if retirement_groups:
            # Catch records created against the old definition during the first
            # pass. Already prepared records are excluded by the revision index.
            self._ensureRecordGroupsReconciled(retirement_groups, user_info)
        previous = {key: processor.get(key) for key in changes}
        self.recordHistory(
            user=user_info.get("email"),
            action=action,
            query={
                "schema_id": str(processor["_id"]),
                "name": processor["name"],
                **changes,
            },
            previous_state=previous,
        )

    @time_it
    def getSchema(self, user_info):
        self.requireSchemaPermission(user_info, require_db=False)
        if USE_DB_PROCESSORS:
            processors = [
                self._serializeSchema(schema) for schema in self.db.processors.find({})
            ]
        else:
            collaborator = self.getCollaboratorForUser(user_info)
            processors = []
            for metadata in processor_api.get_processor_list(collaborator) or []:
                definition = (
                    processor_api.get_processor_by_id(
                        collaborator, metadata.get("Processor ID")
                    )
                    or metadata
                )
                processors.append(
                    {
                        "name": definition.get("Processor Name"),
                        "displayName": definition.get("displayName")
                        or definition.get("Processor Name"),
                        "processorId": definition.get("Processor ID"),
                        "modelId": definition.get("Model ID"),
                        "documentType": definition.get("documentType")
                        or definition.get("Processor Name"),
                        "attributes": definition.get("attributes") or [],
                    }
                )
        for processor in processors:
            processor["attributes"] = [
                field
                for field in schema_rules.normalize_fields(
                    processor.get("attributes") or [], strict=False
                )
                if not field.get("deleted")
            ]
            processor["img"] = util.generate_file_url(
                path=f"sample_images/{processor.get('name')}"
            )
        return {
            "processors": processors,
            "source": "database" if USE_DB_PROCESSORS else "repo",
            "read_only": not USE_DB_PROCESSORS,
        }

    @schema_import.catalog_write
    def uploadProcessorSchema(self, file, schema_meta, user_info, schema_id=None):
        self.requireSchemaPermission(user_info)
        existing = (
            self._schemaDocument(schema_id)
            if schema_id
            else self._findUniqueProcessor(
                {"name": schema_meta.get("name")}, required=False
            )
        )
        if existing:
            self.requireSchemaPermission(user_info, destructive=True)
            if schema_meta.get("name") != existing["name"]:
                raise schema_rules.SchemaError(
                    "Schema names cannot be changed during replacement."
                )
        self._checkProcessorMetadata(schema_meta, existing)
        filename = (file.filename or "").lower()
        if file.content_type == "application/json" or filename.endswith(".json"):
            attributes = util.format_schema_json(file)
        elif filename.endswith(".csv") or file.content_type == "text/csv":
            attributes = util.convert_csv_to_dict(file)
        else:
            raise schema_rules.SchemaError("Upload a JSON or CSV schema file.")
        attributes = schema_rules.validate_fields(attributes, util.CLEANING_FUNCTIONS)
        if not attributes:
            raise schema_rules.SchemaError(
                "The schema file must contain at least one field."
            )
        new_processor = {**schema_meta, "attributes": attributes}
        if existing:
            self._saveProcessorChanges(
                existing, new_processor, user_info, "uploadProcessorSchema"
            )
        else:
            return self.createSchema(new_processor, user_info)
        return self._serializeSchema({**existing, **new_processor})

    @schema_import.catalog_write
    def deleteProcessorSchema(self, processorName, user_info, schema_id=None):
        self.requireSchemaPermission(user_info, destructive=True)
        processor = self._schemaDocument(schema_id, processorName)
        if self._groupsUsingSchema(processor):
            raise schema_rules.SchemaError(
                "This schema is in use. Detach its record groups before deleting it.",
                409,
            )
        archived = {
            **processor,
            "deleted_by": user_info.get("email"),
            "deleted_at": time.time(),
        }
        self.db.deleted_processors.replace_one(
            {"_id": processor["_id"]}, archived, upsert=True
        )
        result = self.db.processors.delete_one(processor)
        if not result.deleted_count:
            raise schema_rules.SchemaError(
                "The schema changed. Reload before deleting it.", 409
            )
        self.recordHistory(
            user=user_info.get("email"),
            action="deleteProcessorSchema",
            query={"name": processorName},
            previous_state={
                key: value for key, value in processor.items() if key != "_id"
            },
        )
        return {"name": processorName}

    @schema_import.catalog_write
    def updateProcessor(self, processor_data, user_info):
        self.requireSchemaPermission(user_info)
        if not isinstance(processor_data, dict):
            raise schema_rules.SchemaError(
                "Provide the schema name and metadata to update."
            )
        processor_data = dict(processor_data)
        processor = self._schemaDocument(
            processor_data.pop("schema_id", None), processor_data.get("name")
        )
        if "name" in processor_data and processor_data["name"] != processor["name"]:
            raise schema_rules.SchemaError("Schema names cannot be renamed.")
        combined = {
            key: processor.get(key)
            for key in (
                "name",
                "displayName",
                "processorId",
                "modelId",
                "documentType",
                "img",
                "parser_type",
            )
        }
        combined.update(processor_data)
        self._checkProcessorMetadata(combined, processor)
        changes = {
            key: value
            for key, value in processor_data.items()
            if processor.get(key) != value
        }
        if set(changes) & {"processorId", "modelId", "documentType", "parser_type"}:
            self.requireSchemaPermission(user_info, destructive=True)
        if changes:
            self._saveProcessorChanges(processor, changes, user_info, "updateProcessor")
        return "success"

    @time_it
    @schema_import.catalog_write
    def updateProcessorAttribute(
        self,
        processor_name,
        field_name,
        updates,
        user_info,
        operation="update",
        schema_id=None,
    ):
        self.requireSchemaPermission(user_info)
        if schema_id is None and (
            not isinstance(processor_name, str) or not processor_name
        ):
            raise schema_rules.SchemaError("processor_name is required.")
        if not isinstance(operation, str) or operation not in {
            "add",
            "update",
            "delete",
        }:
            raise schema_rules.SchemaError("operation must be add, update, or delete.")
        if not isinstance(updates, dict) or set(updates) - schema_rules.FIELD_UPDATES:
            raise schema_rules.SchemaError("Unsupported schema field updates.")
        processor = self._schemaDocument(schema_id, processor_name)
        attributes = [
            field
            for field in schema_rules.normalize_fields(
                processor.get("attributes") or [], strict=False
            )
            if not field.get("deleted")
        ]
        target = next(
            (field for field in attributes if field.get("name") == field_name), None
        )
        if operation == "add":
            new_field = {**updates, "name": updates.get("name") or field_name}
            new_field = schema_rules.normalize_fields([new_field])[0]
            schema_rules.validate_field(new_field, util.CLEANING_FUNCTIONS)
            attributes.append(new_field)
        else:
            schema_rules.field_name(field_name)
            if target is None:
                raise schema_rules.SchemaError("Schema field not found.", 404)
            if operation == "delete":
                self.requireSchemaPermission(user_info, destructive=True)
                attributes = [
                    field
                    for field in attributes
                    if field["name"] != field_name
                    and not field["name"].startswith(field_name + "::")
                ]
            else:
                if not updates:
                    raise schema_rules.SchemaError("Provide at least one field update.")
                if "name" in updates and updates["name"] != field_name:
                    raise schema_rules.SchemaError("Field renaming is disabled.")
                type_changes = any(
                    key in updates and updates[key] != target.get(key)
                    for key in ("data_type", "database_data_type")
                )
                updated = schema_rules.normalize_fields([{**target, **updates}])[0]
                schema_rules.validate_field(
                    updated, util.CLEANING_FUNCTIONS, require_types=type_changes
                )
                attributes[attributes.index(target)] = updated
        schema_rules.validate_structure(attributes)
        self._saveProcessorChanges(
            processor, {"attributes": attributes}, user_info, "updateProcessorAttribute"
        )
        return "success"

    ## user functions
    def getUser(self, email):
        cursor = self.db.users.find({"email": email})
        for document in cursor:
            user = document
            user["_id"] = str(user["_id"])
            user["permissions"] = self.getUserPermissions(user)
            user["collaborator"] = self.getCollaboratorForUser(user)
            return user
        return None

    def updateUserObject(self, user_info):
        cursor = self.db.users.find({"email": user_info["email"]})
        user = None
        for document in cursor:
            user = document
        if user == None:
            return None

        ## update name, picture, hd
        for each in ["name", "picture", "hd"]:
            new_val = user_info.get(each, False)
            if new_val and new_val != "":
                user[each] = new_val

        email = user_info.get("email", "")
        myquery = {"email": email}
        if "_id" in user:
            del user["_id"]
        newvalues = {"$set": user}
        self.db.users.update_one(myquery, newvalues)
        return user

    def ensureTeamExists(self, team):
        team_document = {
            "name": team,
            "users": [],
            "project_list": [],
        }
        result = self.db.teams.update_one(
            {"name": team},
            {"$setOnInsert": team_document},
            upsert=True,
        )
        return result.upserted_id is not None

    def changeUserTeam(self, email, new_team):
        team = new_team.strip()
        if team == "":
            raise ValueError("Please provide a new team in the request body")

        user_doc = self.getDocument("users", {"email": email})
        if user_doc is None:
            raise ValueError(f"Unable to find user {email}")

        created_team = self.ensureTeamExists(team)
        membership_result = self.addUserToTeam(email, team)
        self.db.users.update_one(
            {"email": email},
            {"$set": {"default_team": team}},
        )

        response = {
            "team": team,
            "created_team": created_team,
            "added_to_team": membership_result == "success",
        }
        self.recordHistory(
            "changeUserTeam",
            user=email,
            query=response,
        )
        return response

    def changeUserCollaborator(self, email, new_collaborator):
        collaborator = self._normalizeCollaborator(new_collaborator)
        if collaborator is None:
            raise ValueError("Please provide a new collaborator in the request body")

        user_doc = self.getDocument("users", {"email": email})
        if user_doc is None:
            raise ValueError(f"Unable to find user {email}")

        self.db.users.update_one(
            {"email": email},
            {"$set": {"collaborator": collaborator}},
        )

        response = {
            "collaborator": collaborator,
        }
        self.recordHistory(
            "changeUserCollaborator",
            user=email,
            query=response,
        )
        return response

    def addUser(self, user_info, team, team_lead=False, sys_admin=False):
        if team is None:
            _log.error(f"failed to add user {user_info}. team is required")
            return False

        ## assign roles
        roles = {"team": {}, "projects": {}, "system": []}
        if team_lead:
            roles["team"][team] = ["team_lead"]
        else:
            roles["team"][team] = ["team_member"]

        if sys_admin:
            roles["system"].append("sys_admin")
        self.ensureTeamExists(team)
        user = {
            "email": user_info.get("email", ""),
            "name": user_info.get("name", ""),
            "picture": user_info.get("picture", ""),
            "hd": user_info.get("hd", ""),
            "default_team": team,
            "roles": roles,
            "time_created": time.time(),
        }
        db_response = self.db.users.insert_one(user)
        self.addUserToTeam(user_info.get("email", ""), team)

        return db_response

    def addUserToTeam(self, email, team):
        ## CHECK IF USER IS NOT ALREADY ON THIS TEAM
        checkvalues = {"name": team, "users": email}
        found_user = self.db.teams.count_documents(checkvalues)

        # Keep user role maps in sync with team membership.
        user_doc = self.getDocument("users", {"email": email})
        if user_doc is not None:
            roles = user_doc.get("roles", {})
            team_roles = roles.get("team", {})
            if team not in team_roles:
                team_roles[team] = ["team_member"]
            roles["team"] = team_roles
            update_payload = {"roles": roles}
            if not user_doc.get("default_team"):
                update_payload["default_team"] = team
            self.db.users.update_one({"email": email}, {"$set": update_payload})

        if found_user > 0:
            _log.info(f"found {email} on {team}")
            return "already_exists"

        ## update team's users
        myquery = {"name": team}
        newvalues = {"$addToSet": {"users": email}}
        self.db.teams.update_one(myquery, newvalues)
        return "success"

    def _normalizeStringList(self, values, field_name):
        if not isinstance(values, list):
            raise ValueError(f"{field_name} must be a list")

        normalized = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError(f"{field_name} must contain only strings")
            clean_value = value.strip()
            if clean_value == "":
                raise ValueError(f"{field_name} cannot contain empty values")
            if clean_value not in normalized:
                normalized.append(clean_value)
        return normalized

    def validateRoleIdsForCategory(self, role_category, role_ids):
        normalized_role_ids = self._normalizeStringList(role_ids, "new_roles")
        if not normalized_role_ids:
            return normalized_role_ids

        cursor = self.db.roles.find(
            {"category": role_category, "id": {"$in": normalized_role_ids}},
            {"id": 1},
        )
        valid_role_ids = {document.get("id") for document in cursor}
        invalid_role_ids = [
            role_id for role_id in normalized_role_ids if role_id not in valid_role_ids
        ]
        if invalid_role_ids:
            raise ValueError(
                f"Invalid {role_category} roles: {', '.join(invalid_role_ids)}"
            )
        return normalized_role_ids

    def updateUserRole(self, email, team, role_category, new_roles, updated_by=None):
        new_roles = self.validateRoleIdsForCategory(role_category, new_roles)
        myquery = {"email": email}
        user_doc = self.getDocument("users", myquery)
        if user_doc is None:
            raise ValueError(f"Unable to find user {email}")

        user_roles = user_doc.get("roles", {})
        if role_category == "system":
            user_roles["system"] = new_roles
        elif role_category == "team":
            if not team:
                raise ValueError("team is required to update team roles")
            team_roles = user_roles.get("team", {})
            team_roles[team] = new_roles
            user_roles["team"] = team_roles
        else:
            raise ValueError("role_category must be one of: system, team")

        update = {"$set": {"roles": user_roles}}
        cursor = self.db.users.update_one(myquery, update)
        self.recordHistory(
            "updateUserRole",
            user=updated_by,
            query={
                "email": email,
                "role_category": role_category,
                "roles": new_roles,
            },
        )
        return cursor

    def hasPermission(self, email, permission):
        if permission == schema_rules.DESTRUCTIVE_PERMISSION and not REQUIRE_AUTH:
            return False
        if not REQUIRE_AUTH:
            return True
        # getUser enriches the response with roles and collaborator data. Reading
        # the stored user avoids fetching permissions twice for every check.
        user_doc = self.db.users.find_one({"email": email})
        if user_doc is None:
            return False
        return permission in self.getUserPermissions(user_doc)

    def getUserInfo(self, email):
        user_document = self.getDocument("users", {"email": email}, clean_id=True)
        return user_document

    def getUsers(self, user_info):
        user = user_info.get("email", "")
        user_document = self.getDocument("users", {"email": user})
        team_name = user_document.get("default_team", None)
        team_document = self.getDocument("teams", {"name": team_name})
        team_users = team_document.get("users", [])
        cursor = self.db.users.find()
        users = []
        for document in cursor:
            next_user = document.get("email", "")
            if next_user in team_users:
                users.append(
                    {
                        "email": document.get("email", ""),
                        "name": document.get("name", ""),
                        "hd": document.get("hd", ""),
                        "picture": document.get("picture", ""),
                        "roles": document.get("roles", {}),
                    }
                )
        return users

    def deleteUser(self, email, user_info):
        admin_email = user_info.get("email", None)
        query = {"email": email}
        self.db.users.delete_one(query)
        ## remove user form all teams that include him/her
        query = {"users": email}
        teams_cursor = self.db.teams.find(query)
        for document in teams_cursor:
            team_id = document["_id"]
            user_list = document.get("users", [])
            user_list.remove(email)
            query = {"_id": team_id}
            newvalue = {"$set": {"users": user_list}}
            self.db.teams.update_one(query, newvalue)
        self.recordHistory("deleteUser", user=admin_email)
        return email

    ## Fetch/get functions
    def getDocument(self, collection, query, clean_id=False):
        try:
            cursor = self.db[collection].find(query)
            document = cursor.next()
            if clean_id:
                document_id = document.get("_id", "")
                document["_id"] = str(document_id)
            return document
        except Exception as e:
            _log.error(f"unable to find {query} in {collection}: {e}")
            return None

    def getProjectFromRecordGroup(self, rg_id):
        project_cursor = self.db.projects.find({"record_groups": rg_id})
        project_document = project_cursor.next()
        project_document["_id"] = str(project_document["_id"])
        return project_document

    def getTeamProjectList(self, team):
        team_query = {"name": team}
        team_cursor = self.db.teams.find(team_query)
        team_document = team_cursor.next()
        projects = team_document.get("project_list", [])
        for i in range(len(projects)):
            projects[i] = ObjectId(projects[i])
        return projects

    def getUserProjectList(self, user):
        default_team = self.getDefaultTeamForUser(user)
        if default_team is None:
            _log.info(f"user {user} has no default team")
            return []
        return self.getTeamProjectList(default_team)

    @time_it
    def getUserRecordGroups(self, user):
        projects = self.fetchProjects(user)
        record_groups = []
        for project in projects:
            record_groups += project.get("record_groups", [])
        return record_groups

    def getRecordGroupProgress(self, rg_ids, user=None):
        pipeline = util.generate_record_group_stats(rg_ids)
        stats = {str(s["_id"]): s for s in self.db.records.aggregate(pipeline)}
        return stats

    def fetchTeamInfo(self, email, anonymous_team=None):
        team_name = self.getDefaultTeamForUser(email, anonymous_team)
        if team_name is None:
            _log.error(f"unable to find default team for user {email}")
            return None

        team_doc = self.getDocument("teams", {"name": team_name})
        if team_doc is None:
            _log.error(f"unable to find team {team_name}")
            return None

        if "projects" in team_doc:
            del team_doc["projects"]
        ## convert object ids to strings
        team_doc["_id"] = str(team_doc["_id"])
        team_doc["project_list"] = [
            str(project_object_id)
            for project_object_id in team_doc.get("project_list", [])
        ]
        return team_doc

    def fetchTeams(self):
        teams = []
        teams_cursor = self.db.teams.find()
        for document in teams_cursor:
            team_name = document.get("name", "")
            if team_name != "":
                teams.append(team_name)
        return sorted(teams)

    def fetchProject(self, project_id):
        cursor = self.db.projects.find({"_id": ObjectId(project_id)})
        for document in cursor:
            document["_id"] = str(document["_id"])
            return document
        return None

    @time_it
    def fetchProjects(self, user):
        projects = []
        if user.get("anonymous", False) and not REQUIRE_AUTH:
            team_name = user.get("default_team") or DEFAULT_UNAUTHENTICATED_TEAM["name"]
            _log.info(f"getting projects for anonymous user - team {team_name}")
            user_projects = self.getTeamProjectList(team_name)
        else:
            _log.info(f"user is not anonymous")
            user_email = user.get("email", None)
            user_projects = self.getUserProjectList(user_email)
        cursor = self.db.projects.find({"_id": {"$in": user_projects}})
        for document in cursor:
            document["_id"] = str(document["_id"])
            projects.append(document)
        return projects

    def getProjectRecordGroupsList(self, project_id):
        query = {"_id": ObjectId(project_id)}
        cursor = self.db.projects.find(query)
        document = cursor.next()
        record_groups_list = document.get("record_groups", [])
        return record_groups_list

    def getTeamRecordGroupsList(self, team_name):
        query = {"name": team_name}
        cursor = self.db.teams.find(query)
        document = cursor.next()
        project_list = document.get("project_list", [])
        record_groups_list = []
        for project_id in project_list:
            try:
                rgs = self.getProjectRecordGroupsList(str(project_id))
                record_groups_list += rgs
            except Exception as e:
                _log.error(f"unable to get record groups for project {project_id}: {e}")
        return record_groups_list

    @time_it
    def fetchRecords(
        self,
        sort_by=["dateCreated", 1],
        filter_by={},
        page=None,
        records_per_page=None,
        search_for_errors=True,
        include_attribute_fields=None,  ## use this to include ONLY specific fields
        exclude_attribute_fields=None,  ## use this to exclude specific fields
        forDownload=False,
        user=None,
    ):
        records = []

        pipeline = util.generate_mongo_records_pipeline(
            filter_by=filter_by,
            primary_sort=sort_by,
            records_per_page=records_per_page,
            page=page,
            for_ranking=True,
            secondary_sort=None,
            convert_target_value_to_number=True,
            include_attribute_fields=include_attribute_fields,
            exclude_attribute_fields=exclude_attribute_fields,
            forDownload=forDownload,
        )

        cursor = self.db.records.aggregate(pipeline)

        for document in cursor:
            document["_id"] = str(document["_id"])
            if search_for_errors:
                [hasErrors, found_values] = util.searchRecordForErrorsAndTargetKeys(
                    document
                )
                document["has_errors"] = hasErrors
                for each in found_values:
                    document[each] = found_values[each]
            records.append(document)
        # _log.info(records)
        counts = list(
            self.db.records.aggregate(
                util.active_records_pipeline(filter_by) + [{"$count": "count"}]
            )
        )
        record_count = counts[0]["count"] if counts else 0
        return records, record_count

    def fetchRecordsByTeam(
        self,
        user,
        page=None,
        records_per_page=None,
        sort_by=["dateCreated", 1],
        filter_by={},
        include_attribute_fields=None,
        exclude_attribute_fields=None,
        forDownload=False,
    ):
        team_info = self.fetchTeamInfo(user["email"], user.get("default_team"))
        rg_list = self.getTeamRecordGroupsList(team_info["name"])
        filter_by["record_group_id"] = {"$in": rg_list}
        return self.fetchRecords(
            sort_by,
            filter_by,
            page,
            records_per_page,
            include_attribute_fields=include_attribute_fields,
            exclude_attribute_fields=exclude_attribute_fields,
            forDownload=forDownload,
            user=user,
        )

    def fetchRecordsByRecordGroup(
        self,
        user,
        rg_id,
        page=None,
        records_per_page=None,
        sort_by=["dateCreated", 1],
        filter_by={},
        include_attribute_fields=None,
        exclude_attribute_fields=None,
        forDownload=False,
    ):
        filter_by["record_group_id"] = rg_id
        return self.fetchRecords(
            sort_by,
            filter_by,
            page,
            records_per_page,
            include_attribute_fields=include_attribute_fields,
            exclude_attribute_fields=exclude_attribute_fields,
            forDownload=forDownload,
            user=user,
        )

    def fetchRecordsByProject(
        self,
        user,
        project_id,
        page=None,
        records_per_page=None,
        sort_by=["dateCreated", 1],
        filter_by={},
        include_attribute_fields=None,
        exclude_attribute_fields=None,
        forDownload=False,
    ):
        ## if we arent filtering by record_group_id, add filter to look for ALL record_ids in given project
        if "record_group_id" not in filter_by:
            record_group_ids = self.getProjectRecordGroupsList(project_id)
            filter_by["record_group_id"] = {"$in": record_group_ids}
        return self.fetchRecords(
            sort_by,
            filter_by,
            page,
            records_per_page,
            include_attribute_fields=include_attribute_fields,
            exclude_attribute_fields=exclude_attribute_fields,
            forDownload=forDownload,
            user=user,
        )

    def fetchRecordsByProjectAndDocumentTypes(
        self,
        user,
        project_id,
        document_types,
        page=None,
        records_per_page=None,
        sort_by=["dateCreated", 1],
        filter_by=None,
        include_attribute_fields=None,
        exclude_attribute_fields=None,
        forDownload=False,
    ):
        if filter_by is None:
            filter_by = {}
        else:
            filter_by = filter_by.copy()

        # Remove documentType filter because records collection doesn't contain this field
        if "documentType" in filter_by:
            filter_by.pop("documentType")

        # 1. Get all record group IDs for the project
        project_rg_ids = self.getProjectRecordGroupsList(project_id)
        project_rg_object_ids = [ObjectId(rg) for rg in project_rg_ids]

        # 2. Filter record groups by allowed document types
        rg_query = {
            "_id": {"$in": project_rg_object_ids},
            "documentType": {"$in": document_types},
        }
        cursor = self.db.record_groups.find(rg_query)
        filtered_rg_ids = [str(doc["_id"]) for doc in cursor]

        # 3. Add or intersect record_group_id in filter_by
        if "record_group_id" in filter_by:
            frontend_rg_ids = filter_by["record_group_id"].get("$in", [])
            intersected_rg_ids = list(set(frontend_rg_ids) & set(filtered_rg_ids))
            filter_by["record_group_id"] = {"$in": intersected_rg_ids}
        else:
            filter_by["record_group_id"] = {"$in": filtered_rg_ids}

        # 4. Fetch the records
        return self.fetchRecords(
            sort_by,
            filter_by,
            page,
            records_per_page,
            include_attribute_fields=include_attribute_fields,
            exclude_attribute_fields=exclude_attribute_fields,
            forDownload=forDownload,
            user=user,
        )

    @time_it
    def fetchRecordGroups(self, project_id, user):
        project = self.fetchProject(project_id)
        if project is None:
            _log.info(f"project {project_id} not found")
            return {}

        project_record_groups = project.get("record_groups", [])

        all_stats = self.getRecordGroupProgress(project_record_groups, user)

        record_group_ids = [ObjectId(rg) for rg in project_record_groups]

        record_groups = []
        cursor = self.db.record_groups.find({"_id": {"$in": record_group_ids}})
        for document in cursor:
            document["_id"] = str(document["_id"])
            stats = all_stats.get(
                document["_id"], {"total_amt": 0, "reviewed_amt": 0, "error_amt": 0}
            )
            document.update(stats)
            record_groups.append(document)

        return {"project": project, "record_groups": record_groups}

    @time_it
    def fetchColumnData(self, location, _id, user=None, selected_record_groups=None):
        if location == "project" or location == "team" or location == "documentType":
            columns = set()
            if location == "project":
                # get project, set name and settings
                document = self.db.projects.find({"_id": ObjectId(_id)}).next()
                document["_id"] = _id
                # get all record groups
                record_groups = self.getProjectRecordGroupsList(_id)
            elif location == "team":
                document = self.db.teams.find({"name": _id}).next()
                document["_id"] = str(document["_id"])
                ##TODO: fix object ids in team project list?
                for i in range(len(document["project_list"])):
                    document["project_list"][i] = str(document["project_list"][i])
                record_groups = self.getTeamRecordGroupsList(_id)
            elif location == "documentType":
                # get project, set name and settings
                document = self.db.projects.find({"_id": ObjectId(_id)}).next()
                document["_id"] = _id
                record_groups = selected_record_groups or []

            rg_ids = []
            for rg in record_groups:
                rg_ids.append(ObjectId(rg))
            rg_documents = list(self.db.record_groups.find({"_id": {"$in": rg_ids}}))
            doc_type_columns = {}

            for doc in rg_documents:
                doc_type = doc.get("documentType") or "Unknown"
                rg_schema = self.getRecordGroupSchemaAttributes(
                    user=user, rg_document=doc
                )
                if rg_schema:
                    if location == "documentType" and doc_type not in doc_type_columns:
                        doc_type_columns[doc_type] = []
                    for attr in rg_schema:
                        attr_name = attr.get("name")
                        if attr_name:
                            columns.add(attr_name)
                            if location == "documentType":
                                if attr_name not in doc_type_columns[doc_type]:
                                    doc_type_columns[doc_type].append(attr_name)
                # Include imported fields not yet defined by the shared schema.
                derived_cols = self.deriveRecordColumnsFromRecordGroups(
                    [str(doc["_id"])], user
                )
                columns.update(derived_cols)
                if location == "documentType":
                    if doc_type not in doc_type_columns:
                        doc_type_columns[doc_type] = []
                    for col in derived_cols:
                        if col not in doc_type_columns[doc_type]:
                            doc_type_columns[doc_type].append(col)

            if "projects" in document:
                del document["projects"]

            if location == "documentType":
                return {
                    "doc_type_columns": doc_type_columns,
                    "obj": document,
                }
            else:
                columns.add("record_notes")
                return {
                    "columns": list(columns),
                    "obj": document,
                }

        elif location == "record_group":
            columns = []
            rg_document = self.db.record_groups.find({"_id": ObjectId(_id)}).next()
            rg_document["_id"] = _id
            rg_schema = self.getRecordGroupSchemaAttributes(
                user=user, rg_document=rg_document
            )
            if rg_schema:
                for attr in rg_schema:
                    attr_name = attr["name"]
                    columns.append(attr["name"])
            columns = list(
                dict.fromkeys(
                    columns + self.deriveRecordColumnsFromRecordGroups([_id], user)
                )
            )
            columns.append("record_notes")
            return {"columns": columns, "obj": rg_document}
        return None

    def _recordSchema(self, group, user=None):
        """Resolve reconciliation rules; a missing definition never retires data."""
        processor = self.resolveRecordGroupSchema(group, user)
        return self._schemaState(processor)

    def _schemaState(self, processor):
        schema = (
            processor
            if processor and (USE_DB_PROCESSORS or "attributes" in processor)
            else None
        )
        keep_unknown = USE_DB_PROCESSORS or schema is None
        if schema is not None:
            schema = {
                "attributes": schema_rules.normalize_fields(
                    schema.get("attributes") or [], strict=False
                )
            }
        fingerprint = self._attributeDigest([3, schema, keep_unknown])
        return schema, keep_unknown, fingerprint

    @staticmethod
    def _attributeDigest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, default=str).encode()
        ).hexdigest()

    def _recordAttributeState(self, attributes, schema_state):
        schema, keep_unknown, fingerprint = schema_state
        attributes, _ = util.sortRecordAttributes(
            attributes, schema, keep_all_attributes=keep_unknown
        )
        return {
            "attributesList": attributes,
            "attribute_schema_revision": fingerprint,
            "attribute_revision": self._attributeDigest([fingerprint, attributes]),
            "has_errors": util.searchRecordForErrorsAndTargetKeys(
                {"attributesList": attributes}, []
            )[0],
        }

    @staticmethod
    def _originalAttributeQuery(record):
        return {
            "_id": ObjectId(record["_id"]),
            **{
                key: record[key] if key in record else {"$exists": False}
                for key in (
                    "attributesList",
                    "attribute_revision",
                    "attribute_schema_revision",
                )
            },
        }

    def _reconcileRecord(self, record, schema_state=None, user=None):
        if schema_state is None:
            group = self.db.record_groups.find_one(
                {"_id": ObjectId(record["record_group_id"])}
            )
            schema_state = self._recordSchema(group or {}, user)
        for attempt in range(3):
            fields = self._recordAttributeState(
                record.get("attributesList"), schema_state
            )
            if all(record.get(key) == value for key, value in fields.items()):
                return record
            result = self.db.records.update_one(
                self._originalAttributeQuery(record), {"$set": fields}
            )
            if result.matched_count:
                return {**record, **fields}
            fresh = self.db.records.find_one({"_id": ObjectId(record["_id"])})
            if fresh is None:
                raise schema_rules.SchemaError("Record not found.", 404)
            record = {**record, **fresh}
        raise schema_rules.SchemaError(
            "The record changed during schema reconciliation. Reload and retry.", 409
        )

    def _ensureRecordGroupsReconciled(
        self, group_ids, user=None, schema_state=None, *, limit=None
    ):
        # Explicit schema/cleaning mutations only. Page reads must never call this.
        # Conditional writes preserve concurrent edits; cursor batches bound memory.
        group_ids = list(dict.fromkeys(group_ids))
        for group in self.db.record_groups.find(
            {"_id": {"$in": [ObjectId(value) for value in group_ids]}}
        ):
            original_state = self._recordSchema(group, user)
            target_state = schema_state or original_state
            query = {
                "record_group_id": str(group["_id"]),
                "attribute_schema_revision": {"$ne": target_state[2]},
            }
            records = self.db.records.find(query).batch_size(100)
            if limit is not None:
                records = records.limit(limit)
            for record in records:
                self._reconcileRecord(record, target_state, user)
            current_group = self.db.record_groups.find_one({"_id": group["_id"]})
            if (
                current_group is None
                or self._recordSchema(current_group, user)[2] != original_state[2]
            ):
                raise schema_rules.SchemaError(
                    "The schema changed while preparing records. Retry the request.",
                    409,
                )

    def getRecordGroupSchemaAttributes(self, rg_id=None, user=None, rg_document=None):
        group = rg_document
        if group is None and rg_id is not None:
            group = self.db.record_groups.find_one({"_id": ObjectId(rg_id)})
        schema, _, _ = self._recordSchema(group or {}, user)
        return [
            field
            for field in (schema or {}).get("attributes", [])
            if not field.get("deleted")
        ]

    def getRecordGroupSchemaMap(self, rg_id, user=None):
        return util.convert_processor_attributes_to_dict(
            self.getRecordGroupSchemaAttributes(rg_id=rg_id, user=user)
        )

    def deriveRecordColumnsFromRecordGroups(self, record_group_ids, user=None):
        columns = set()
        if not record_group_ids:
            return []

        cursor = self.db.records.find(
            {"record_group_id": {"$in": record_group_ids}},
            {"attributesList": 1},
        )
        for record in cursor:
            for _, attribute_identifier in util.iter_attribute_tree(
                record.get("attributesList") or []
            ):
                if attribute_identifier:
                    columns.add(attribute_identifier)
        return list(columns)

    def fetchProcessors(self, user):
        processor_list = self.createProcessorsList(user)
        return {
            "USE_DB_PROCESSORS": USE_DB_PROCESSORS,
            "collaborator": self.getCollaboratorForUser(user),
            "processor_list": processor_list,
        }

    def fetchRoles(self, role_categories):
        roles = []
        cursor = self.db.roles.find({"category": {"$in": role_categories}})
        for document in cursor:
            role = document.copy()
            if "_id" in role:
                del role["_id"]
            roles.append(role)
        return roles

    def fetchPermissionCatalog(self, role_categories):
        permissions = set()
        cursor = self.db.roles.find({"category": {"$in": role_categories}})
        for document in cursor:
            for permission in document.get("permissions", []):
                if isinstance(permission, str) and permission.strip() != "":
                    permissions.add(permission.strip())
        return sorted(permissions)

    def updateRolePermissions(self, role_id, category, permissions, updated_by=None):
        normalized_permissions = self._normalizeStringList(permissions, "permissions")
        if schema_rules.DESTRUCTIVE_PERMISSION in normalized_permissions and (
            category != "system" or role_id != "sys_admin"
        ):
            raise ValueError(
                "manage_schema_destructive can only be assigned to the sys_admin system role."
            )
        query = {"id": role_id, "category": category}
        role = self.getDocument("roles", query)
        if role is None:
            raise ValueError(f"Unable to find {category} role {role_id}")

        update = {"$set": {"permissions": normalized_permissions}}
        self.db.roles.update_one(query, update)
        self.recordHistory(
            "updateRolePermissions",
            user=updated_by,
            query={
                "role_id": role_id,
                "category": category,
                "permissions": normalized_permissions,
            },
        )

        updated_role = self.getDocument("roles", query)
        if updated_role and "_id" in updated_role:
            del updated_role["_id"]
        return updated_role

    def fetchRecordGroupData(self, rg_id, user):
        ## get user's projects, check if user has access to this project
        user_record_groups = self.getUserRecordGroups(user)
        if not rg_id in user_record_groups:
            return None, None

        ## get record group data
        _id = ObjectId(rg_id)
        cursor = self.db.record_groups.find({"_id": _id})
        record_group = cursor.next()
        record_group["_id"] = str(record_group["_id"])
        record_group = self._recordGroupSchemaInfo(record_group, user)
        record_group["has_records"] = (
            self.db.records.find_one({"record_group_id": rg_id}, {"_id": 1}) is not None
        )

        project_document = self.getProjectFromRecordGroup(rg_id)

        return project_document, record_group

    @time_it
    def fetchRecordData(
        self, record_id, user_info, page_state=None, background_tasks=None
    ):
        user = user_info.get("email", "")
        _id = ObjectId(record_id)
        cursor = self.db.records.find({"_id": _id})
        try:
            document = cursor.next()
        except:
            _log.error(f"record with id {record_id} does not exist")
            return None, None
        document["_id"] = str(document["_id"])
        rg_id = document.get("record_group_id", "")
        # projectId = document.get("project_id", "")
        # project_id = ObjectId(projectId)

        user_record_groups = self.getUserRecordGroups(user_info)
        if not rg_id in user_record_groups:
            return None, None

        document = self._reconcileRecord(document, user=user_info)
        document["_id"] = str(document["_id"])
        ## try to attain lock
        attained_lock = self.tryLockingRecord(record_id, user)
        image_urls = []
        image_files = document.get("image_files", [])
        for image in image_files:
            if util.imageIsValid(image):
                next_img_url = get_document_image(
                    document["record_group_id"], document["_id"], image
                )
                image_urls.append(next_img_url)
        if len(image_urls) == 0:
            if document.get("filename", False):
                next_img_url = get_document_image(
                    document["record_group_id"],
                    document["_id"],
                    document["filename"],
                )
                image_urls.append(next_img_url)
        document["img_urls"] = image_urls

        ## get record group name
        rg = self.getDocument("record_groups", {"_id": ObjectId(rg_id)})
        rg_name = rg.get("name", "")
        document["has_schema"] = self._recordGroupSchemaInfo(rg, user_info)[
            "has_schema"
        ]
        document["rg_name"] = rg_name
        document["rg_id"] = rg_id

        ## get project name
        project_document = self.getProjectFromRecordGroup(rg_id)
        project_name = project_document.get("name", "")
        project_id = str(project_document.get("_id", ""))
        document["project_name"] = project_name
        document["project_id"] = project_id

        ## For the frontend, we want to know the record index, the next record id, and the previous record id
        ## This^ helps for navigation between records.
        ## Users typically arrive at a record by clicking on one in a table.
        ## This table can be a record group, a project, or a table of all the records a team owns.
        ## We want to allow for the location of the record in the table to persist when navigating to the record.
        ## This means that we must incldue the filters and sorting that that table had when
        ## checking the index, next, and previous IDs

        ## need to get that list depending on location and group id
        if page_state:
            location = page_state.get("location")
            group_id = page_state.get("group_id")
            filterBy = page_state.get("filterBy")
            sortBy = page_state.get("sortBy")
            if not filterBy:
                filterBy = {}
            if not sortBy:
                sortBy = ["dateCreated", 1]
            group_record_group_ids = self.getRecordGroupIdsByGroup(location, group_id)
            filterBy["record_group_id"] = {"$in": group_record_group_ids}
        else:
            filterBy = {"record_group_id": rg_id}
            sortBy = ["dateCreated", 1]

        ## Get Record index, next id, and previous id
        self.getRecordIndexes(document, filterBy, tuple(sortBy), user_info)

        return document, not attained_lock

    def getRecordGroupIdsByGroup(self, location, group_id):
        ## Get the list of record group ids
        if location == "team":
            return self.getTeamRecordGroupsList(team_name=group_id)
        elif location == "project":
            return self.getProjectRecordGroupsList(group_id)
        elif location == "record_group":
            return [group_id]

    def fetchRecordNotes(self, record_id, user_info):
        # user = user_info.get("email", "")
        _id = ObjectId(record_id)
        cursor = self.db.records.find({"_id": _id})
        document = cursor.next()
        return document.get("record_notes", [])

    def fetchRecordHistory(self, record_id, user_info):
        history_cursor = self.db.history.find(
            {"record_id": record_id}, {"_id": 0}
        ).sort("timestamp", DESCENDING)
        history_items = list(history_cursor)

        for history_item in history_items:
            action = history_item.get("action")

            if action == "updateRecord":
                query = history_item.get("query")
                previous_state = history_item.get("previous_state")
                if isinstance(query, (dict, list)):
                    self._annotateHistoryPayloadNumericTypes(query)
                if isinstance(previous_state, (dict, list)):
                    self._annotateHistoryPayloadNumericTypes(previous_state)

            if action == "cleanRecord":
                before_attrs = history_item.get("attributesList_before")
                after_attrs = history_item.get("attributesList_after")
                if isinstance(before_attrs, (dict, list)):
                    self._annotateHistoryPayloadNumericTypes(before_attrs)
                if isinstance(after_attrs, (dict, list)):
                    self._annotateHistoryPayloadNumericTypes(after_attrs)

        return history_items

    @time_it
    def getRecordIndexes(self, document, filterBy, sortBy, user=None):
        target_id = (
            ObjectId(document["_id"])
            if not isinstance(document["_id"], ObjectId)
            else document["_id"]
        )

        pipeline = util.generate_mongo_records_pipeline(
            filter_by=filterBy,
            primary_sort=sortBy,
            for_ranking=True,
            convert_target_value_to_number=True,
            match_record_id=target_id,
        )

        result = list(self.db.records.aggregate(pipeline))
        if not result:
            return None

        record = result[0]
        prevId = record.get("prevId", target_id)
        nextId = record.get("nextId", target_id)

        document["rank"] = record["rank"]
        document["previous_id"] = prevId
        document["next_id"] = nextId

        return document

    def getProcessorByRecordGroupID(self, rg_id, returnNameOnly=False, user=None):
        group = self.db.record_groups.find_one({"_id": ObjectId(rg_id)})
        if group is None:
            raise schema_rules.SchemaError("Record group not found.", 404)
        schema = self.resolveRecordGroupSchema(group, user)
        if returnNameOnly:
            if schema is None:
                return group.get("documentType") or group.get("name")
            return schema.get("name")
        if schema is None:
            return None, None, []
        return (
            schema.get("processorId"),
            schema.get("modelId"),
            schema_rules.normalize_fields(schema.get("attributes") or [], strict=False),
        )

    def getProcessorByRecordID(self, record_id, user=None):
        document = self.db.records.find_one({"_id": ObjectId(record_id)})
        if document is None:
            raise schema_rules.SchemaError("Record not found.", 404)
        return self.getProcessorByRecordGroupID(document["record_group_id"], user=user)

    def userCanAccessProject(self, project_id, user_info):
        try:
            ObjectId(project_id)
        except Exception:
            return False
        return project_id in {
            project["_id"] for project in self.fetchProjects(user_info)
        }

    def _getImportPackage(self, import_request):
        if isinstance(import_request, dict):
            return (
                import_request.get("import_package")
                or import_request.get("package")
                or import_request.get("data")
                or import_request
            )
        return import_request

    def _getImportPackageRecords(self, import_package):
        if isinstance(import_package, list):
            records = import_package
        elif isinstance(import_package, dict):
            records = import_package.get("records")
        else:
            raise ValueError("JSON import must be an object or an array of records.")

        if not isinstance(records, list):
            raise ValueError("JSON import must include a records array.")
        if len(records) == 0:
            raise ValueError("JSON import must include at least one record.")
        return records

    def _getImportPackageFormat(self, import_package):
        if isinstance(import_package, dict):
            return import_package.get("format") or import_package.get("version")
        return None

    def _getImportRecordMetadataKeys(self):
        return {
            "_id",
            "id",
            "record_id",
            "record_group_id",
            "rg_id",
            "project_id",
            "name",
            "file",
            "filename",
            "original_filename",
            "api_number",
            "contributor",
            "status",
            "review_status",
            "verification_status",
            "URL",
            "url",
            "image_files",
            "img_urls",
            "image_whitespace",
            "source_type",
            "dateCreated",
            "lastUpdated",
            "lastUpdatedBy",
            "record_notes",
            "notes",
            "previous_id",
            "next_id",
            "rank",
            "record_number",
        }

    def _looksLikeImportedAttribute(self, value):
        if not isinstance(value, dict):
            return False
        return any(
            key in value
            for key in (
                "key",
                "name",
                "value",
                "raw_text",
                "text_value",
                "normalized_value",
                "normalized_vertices",
                "coordinates",
                "subattributes",
                "properties",
                "page",
                "confidence",
                "ai_confidence",
            )
        )

    def _getImportPackageSchemaFields(self, import_package):
        if not isinstance(import_package, dict):
            return []

        schema = import_package.get("schema")
        if isinstance(schema, dict):
            fields = schema.get("fields") or schema.get("attributes") or []
        else:
            fields = import_package.get("schema_fields") or []

        if not isinstance(fields, list):
            raise schema_rules.SchemaError("Import schema fields must be an array.")
        fields = [
            dict(field, name=field.get("name") or field.get("key"))
            if isinstance(field, dict)
            else field
            for field in fields
        ]
        return schema_rules.validate_fields(
            fields, util.CLEANING_FUNCTIONS, require_types=False
        )

    def _getImportPackageDocumentType(self, import_package):
        if not isinstance(import_package, dict):
            return None
        schema = import_package.get("schema")
        if isinstance(schema, dict):
            return schema.get("documentType") or schema.get("document_type")
        return import_package.get("documentType") or import_package.get("document_type")

    def _coerceImportedValue(self, value):
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return json.dumps(value, default=util.defaultJSONDumpHandler)

    def _simpleFieldsToAttributes(self, fields):
        attributes = []
        for key, value in fields.items():
            if isinstance(value, dict) and not any(
                field_key in value
                for field_key in (
                    "key",
                    "name",
                    "value",
                    "raw_text",
                    "normalized_value",
                    "subattributes",
                    "properties",
                )
            ):
                subattributes = self._simpleFieldsToAttributes(value)
                attributes.append(
                    {
                        "key": key,
                        "value": None,
                        "raw_text": None,
                        "normalized_value": None,
                        "subattributes": subattributes,
                    }
                )
            else:
                attributes.append(
                    {
                        "key": key,
                        "value": value,
                        "raw_text": value,
                        "normalized_value": value,
                        "subattributes": [],
                    }
                )
        return attributes

    def _attributeMapToAttributes(self, record):
        attributes = []
        metadata_keys = self._getImportRecordMetadataKeys()
        for key, value in record.items():
            if key in metadata_keys:
                continue
            if self._looksLikeImportedAttribute(value):
                attribute = value.copy()
                if not attribute.get("key") and not attribute.get("name"):
                    attribute["key"] = key
                attributes.append(attribute)
            else:
                attributes.append(
                    {
                        "key": key,
                        "value": value,
                        "raw_text": value,
                        "normalized_value": value,
                        "subattributes": [],
                    }
                )
        return attributes

    def _normalizeImportedAttribute(self, attribute, record_idx):
        if not isinstance(attribute, dict):
            raise ValueError(f"Record {record_idx + 1} has a non-object attribute.")

        key = attribute.get("key") or attribute.get("name")
        if not key:
            raise ValueError(f"Record {record_idx + 1} has an attribute without a key.")

        subattributes = (
            attribute.get("subattributes")
            if "subattributes" in attribute
            else attribute.get("properties", [])
        )
        if subattributes is None:
            subattributes = []
        if not isinstance(subattributes, list):
            raise ValueError(
                f"Record {record_idx + 1} attribute {key} has non-array subattributes."
            )

        value = attribute.get("value")
        if "value" not in attribute:
            value = attribute.get("normalized_value", attribute.get("raw_text"))

        raw_text = attribute.get("raw_text", attribute.get("text_value", value))
        normalized_value = attribute.get("normalized_value", value)
        normalized_attribute = {
            "key": str(key),
            "ai_confidence": attribute.get(
                "ai_confidence", attribute.get("confidence")
            ),
            "confidence": attribute.get("confidence"),
            "raw_text": self._coerceImportedValue(raw_text),
            "text_value": self._coerceImportedValue(attribute.get("text_value")),
            "value": self._coerceImportedValue(value),
            "normalized_vertices": attribute.get(
                "normalized_vertices", attribute.get("coordinates")
            ),
            "normalized_value": self._coerceImportedValue(normalized_value),
            "subattributes": [
                self._normalizeImportedAttribute(subattribute, record_idx)
                for subattribute in subattributes
            ],
            "edited": bool(attribute.get("edited", False)),
            "page": attribute.get("page"),
        }
        if attribute.get("deleted") is True:
            normalized_attribute["deleted"] = True
        if "user_added" in attribute:
            normalized_attribute["user_added"] = bool(attribute.get("user_added"))
        return normalized_attribute

    def _normalizeImportedRecordAttributes(self, record, record_idx):
        attributes = record.get("attributesList")
        if attributes is None:
            attributes = record.get("attributes")
        if attributes is None and isinstance(record.get("fields"), dict):
            attributes = self._simpleFieldsToAttributes(record.get("fields"))
        if attributes is None:
            attributes = self._attributeMapToAttributes(record)

        if not isinstance(attributes, list):
            raise ValueError(
                f"Record {record_idx + 1} must include attributesList, attributes, fields, or exported attribute columns."
            )
        if len(attributes) == 0:
            raise ValueError(f"Record {record_idx + 1} does not contain attributes.")

        normalized_attributes = [
            self._normalizeImportedAttribute(attribute, record_idx)
            for attribute in attributes
        ]
        return util.normalize_record_attribute_tree(normalized_attributes)

    def _buildImportedRecords(self, rg_id, import_package, user_info):
        records = self._getImportPackageRecords(import_package)
        normalized_records = []
        for idx, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(f"Record {idx + 1} must be an object.")

            source_filename = (
                record.get("filename")
                or record.get("file")
                or record.get("original_filename")
            )
            name = record.get("name") or source_filename or f"record-{idx + 1}"
            name = str(name).strip() or f"record-{idx + 1}"
            filename = str(source_filename or f"{name}.json").strip()
            if not filename:
                filename = f"{name}.json"
            if name == filename:
                name = os.path.splitext(os.path.basename(filename))[0] or name

            normalized_records.append(
                {
                    "record_group_id": rg_id,
                    "name": name,
                    "filename": filename,
                    "api_number": record.get("api_number"),
                    "contributor": user_info,
                    "status": record.get("status") or "digitized",
                    "review_status": record.get("review_status") or "unreviewed",
                    "verification_status": record.get("verification_status"),
                    "original_filename": record.get("original_filename")
                    or record.get("file")
                    or filename,
                    "image_files": record.get("image_files") or [],
                    "attributesList": self._normalizeImportedRecordAttributes(
                        record, idx
                    ),
                    "source_type": record.get("source_type") or "json_import",
                }
            )
        return normalized_records

    def analyzeImportedRecordDuplicates(
        self, rg_id, normalized_records, prevent_duplicates=True
    ):
        filenames = [record.get("filename") for record in normalized_records]
        existing_duplicate_bases = set(
            self.checkIfRecordsExist(filenames, rg_id) if rg_id else []
        )
        duplicate_filename_bases = {
            filename_base: count
            for filename_base, count in Counter(
                self.getFilenameBase(filename) for filename in filenames
            ).items()
            if filename_base and count > 1
        }

        seen_import_bases = set()
        existing_duplicates = []
        internal_duplicates = []
        importable_records = []

        for idx, record in enumerate(normalized_records):
            filename = record.get("filename")
            filename_base = self.getFilenameBase(filename)
            duplicate_item = {
                "index": idx,
                "filename": filename,
                "filename_base": filename_base,
                "name": record.get("name"),
            }

            is_existing_duplicate = (
                bool(filename_base) and filename_base in existing_duplicate_bases
            )
            is_internal_duplicate = (
                bool(filename_base) and filename_base in seen_import_bases
            )

            if is_existing_duplicate:
                existing_duplicates.append(duplicate_item)
            elif is_internal_duplicate:
                internal_duplicates.append(duplicate_item)

            if prevent_duplicates and (is_existing_duplicate or is_internal_duplicate):
                continue

            importable_records.append(record)
            if filename_base:
                seen_import_bases.add(filename_base)

        skipped_duplicate_count = (
            len(existing_duplicates) + len(internal_duplicates)
            if prevent_duplicates
            else 0
        )
        return {
            "record_count": len(normalized_records),
            "requested_count": len(normalized_records),
            "importable_records": importable_records,
            "importable_count": len(importable_records),
            "existing_duplicates": existing_duplicates,
            "existing_duplicate_count": len(existing_duplicates),
            "internal_duplicates": internal_duplicates,
            "internal_duplicate_count": len(internal_duplicates),
            "skipped_duplicates": [
                duplicate["filename"]
                for duplicate in existing_duplicates + internal_duplicates
            ]
            if prevent_duplicates
            else [],
            "skipped_duplicate_count": skipped_duplicate_count,
            "duplicate_filename_bases_in_file": duplicate_filename_bases,
            "duplicate_filename_base_count_in_file": len(duplicate_filename_bases),
            "prevent_duplicates": prevent_duplicates,
        }

    def previewJsonRecords(self, rg_id, import_request, prevent_duplicates=True):
        import_package = self._getImportPackage(import_request)
        normalized_records = self._buildImportedRecords(rg_id, import_package, {})
        preview = self.analyzeImportedRecordDuplicates(
            rg_id, normalized_records, prevent_duplicates=prevent_duplicates
        )
        preview.pop("importable_records", None)
        _log.info(
            "record import preview rg_id=%s requested_count=%s importable_count=%s skipped_duplicate_count=%s existing_duplicate_count=%s internal_duplicate_count=%s prevent_duplicates=%s",
            rg_id,
            preview["requested_count"],
            preview["importable_count"],
            preview["skipped_duplicate_count"],
            preview["existing_duplicate_count"],
            preview["internal_duplicate_count"],
            prevent_duplicates,
        )
        return preview

    def importJsonRecords(
        self, rg_id, import_request, user_info, prevent_duplicates=True
    ):
        import_package = self._getImportPackage(import_request)
        normalized_records = self._buildImportedRecords(
            rg_id, import_package, user_info
        )
        duplicate_preview = self.analyzeImportedRecordDuplicates(
            rg_id, normalized_records, prevent_duplicates=prevent_duplicates
        )
        records_to_create = duplicate_preview.pop("importable_records", [])

        schema_fields = self._getImportPackageSchemaFields(import_package)
        if schema_fields:
            rg_document = self.getDocument("record_groups", {"_id": ObjectId(rg_id)})
            current_fields = self.getRecordGroupSchemaAttributes(
                user=user_info, rg_document=rg_document
            )
            if schema_fields != current_fields:
                self.requireSchemaPermission(
                    user_info, destructive=bool(current_fields)
                )
                raise schema_rules.SchemaError(
                    "Appending records cannot replace a schema. Create or edit the shared schema separately.",
                    409,
                )

        created_record_ids = []
        for record in records_to_create:
            created_record_ids.append(self.createRecord(record, user_info))

        _log.info(
            "record import complete rg_id=%s requested_count=%s created_count=%s skipped_duplicate_count=%s existing_duplicate_count=%s internal_duplicate_count=%s prevent_duplicates=%s duplicate_filename_base_count_in_file=%s",
            rg_id,
            duplicate_preview["requested_count"],
            len(created_record_ids),
            duplicate_preview["skipped_duplicate_count"],
            duplicate_preview["existing_duplicate_count"],
            duplicate_preview["internal_duplicate_count"],
            prevent_duplicates,
            duplicate_preview["duplicate_filename_base_count_in_file"],
        )

        self.recordHistory(
            "importJsonRecords",
            user_info.get("email", None),
            rg_id=rg_id,
            notes={
                "requested_count": duplicate_preview["requested_count"],
                "created_count": len(created_record_ids),
                "skipped_duplicate_count": duplicate_preview["skipped_duplicate_count"],
                "format": self._getImportPackageFormat(import_package),
                "duplicate_filename_base_count_in_file": duplicate_preview[
                    "duplicate_filename_base_count_in_file"
                ],
            },
        )
        return {
            "record_group_id": rg_id,
            "created_record_ids": created_record_ids,
            "requested_count": duplicate_preview["requested_count"],
            "created_count": len(created_record_ids),
            "skipped_duplicates": duplicate_preview["skipped_duplicates"],
            "skipped_duplicate_count": duplicate_preview["skipped_duplicate_count"],
            "existing_duplicate_count": duplicate_preview["existing_duplicate_count"],
            "internal_duplicate_count": duplicate_preview["internal_duplicate_count"],
            "duplicate_filename_bases_in_file": duplicate_preview[
                "duplicate_filename_bases_in_file"
            ],
        }

    def _parseCsvAttributePath(self, column_name):
        parts = []
        current = ""
        for char in str(column_name):
            if char == "[":
                if current:
                    parts.append(current.strip())
                current = ""
            elif char == "]":
                if current:
                    parts.append(current.strip())
                current = ""
            else:
                current += char
        if current.strip():
            parts.append(current.strip())
        return [part for part in parts if part]

    def _addCsvAttributeValue(self, attributes, path, value):
        if not path:
            return
        key = path[0]
        attribute = next(
            (existing for existing in attributes if existing.get("key") == key),
            None,
        )
        if attribute is None:
            attribute = {
                "key": key,
                "value": None if len(path) > 1 else value,
                "raw_text": None if len(path) > 1 else value,
                "normalized_value": None if len(path) > 1 else value,
                "subattributes": [],
            }
            attributes.append(attribute)
        elif len(path) == 1:
            attribute["value"] = value
            attribute["raw_text"] = value
            attribute["normalized_value"] = value

        if len(path) > 1:
            self._addCsvAttributeValue(attribute["subattributes"], path[1:], value)

    def _csvRowsToImportPackage(self, rows):
        records = []
        metadata_keys = self._getImportRecordMetadataKeys()
        for idx, row in enumerate(rows):
            attributes = []
            for column, value in row.items():
                if column is None or column in metadata_keys:
                    continue
                if value is None or str(value).strip() == "":
                    continue
                self._addCsvAttributeValue(
                    attributes,
                    self._parseCsvAttributePath(column),
                    value,
                )

            filename = row.get("filename") or row.get("file") or f"record-{idx + 1}.csv"
            records.append(
                {
                    "name": row.get("name")
                    or os.path.splitext(os.path.basename(filename))[0]
                    or f"record-{idx + 1}",
                    "filename": filename,
                    "original_filename": row.get("original_filename") or filename,
                    "api_number": row.get("api_number"),
                    "status": row.get("status"),
                    "review_status": row.get("review_status"),
                    "verification_status": row.get("verification_status"),
                    "attributesList": attributes,
                    "source_type": "csv_import",
                }
            )
        return {"format": "ogrre-csv-records-v1", "records": records}

    def parseImportFile(self, filename, file_bytes):
        size_bytes = len(file_bytes) if isinstance(file_bytes, bytes) else None
        if isinstance(file_bytes, bytes):
            text = file_bytes.decode("utf-8-sig")
        else:
            text = str(file_bytes)
        extension = os.path.splitext(filename or "")[1].lower()

        if extension == ".csv":
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            _log.info(
                "record import parsed file filename=%s extension=%s size_bytes=%s detected_format=csv row_count=%s header_count=%s",
                filename,
                extension,
                size_bytes,
                len(rows),
                len(reader.fieldnames or []),
            )
            return self._csvRowsToImportPackage(rows)

        try:
            parsed = json.loads(text)
            try:
                record_count = len(self._getImportPackageRecords(parsed))
            except Exception:
                record_count = "unknown"
            _log.info(
                "record import parsed file filename=%s extension=%s size_bytes=%s detected_format=json package_format=%s record_count=%s",
                filename,
                extension,
                size_bytes,
                self._getImportPackageFormat(parsed),
                record_count,
            )
            return parsed
        except json.JSONDecodeError as json_error:
            if extension == ".json":
                raise ValueError(f"Invalid JSON import file: {json_error}")
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            if rows:
                _log.info(
                    "record import parsed file filename=%s extension=%s size_bytes=%s detected_format=csv row_count=%s header_count=%s",
                    filename,
                    extension,
                    size_bytes,
                    len(rows),
                    len(reader.fieldnames or []),
                )
                return self._csvRowsToImportPackage(rows)
            raise ValueError("Import file must be valid JSON or CSV.")

    def createRecordGroupFromJsonImport(self, project_id, import_request, user_info):
        if not self.userCanAccessProject(project_id, user_info):
            raise PermissionError("User does not have access to this project.")

        import_package = self._getImportPackage(import_request)
        self._getImportPackageRecords(import_package)

        record_group_info = (
            import_request.get("record_group")
            if isinstance(import_request, dict)
            else {}
        )
        if not isinstance(record_group_info, dict):
            record_group_info = {}

        name = str(record_group_info.get("name") or "").strip()
        if not name:
            raise ValueError("Record group name is required.")

        document_type = (
            record_group_info.get("documentType")
            or record_group_info.get("document_type")
            or self._getImportPackageDocumentType(import_package)
            or "JSON Import"
        )
        rg_info = {
            "name": name,
            "description": record_group_info.get("description", ""),
            "history": [],
            "documentType": document_type,
            "processorId": None,
            "processor_id": None,
            "project_id": project_id,
            "source_type": "json_import",
            "import_format": self._getImportPackageFormat(import_package),
            "attributes": self._getImportPackageSchemaFields(import_package),
        }
        rg_id = self.createRecordGroup(rg_info, user_info)
        prevent_duplicates = True
        if isinstance(import_request, dict):
            prevent_duplicates = import_request.get(
                "preventDuplicates", import_request.get("prevent_duplicates", True)
            )
        import_summary = self.importJsonRecords(
            rg_id,
            import_request,
            user_info,
            prevent_duplicates=prevent_duplicates,
        )
        import_summary["record_group_id"] = rg_id
        return import_summary

    ## create/add functions
    def createProject(self, project_info, user_info):
        ## get user's default team
        user_email = user_info.get("email", "")
        default_team = self.getDefaultTeamForUser(
            user_email, user_info.get("default_team")
        )
        if default_team is None:
            _log.info(f"user {user_email} has no default team")
            return False

        ## add default data to project
        project_info["creator"] = user_info
        project_info["team"] = default_team
        project_info["dateCreated"] = time.time()
        project_info["record_groups"] = []
        project_info["history"] = []
        project_info["tags"] = []
        project_info["settings"] = {}

        ## create new project entry
        db_response = self.db.projects.insert_one(project_info)
        new_project_id = db_response.inserted_id

        ## add project to team's project list:
        team_query = {"name": default_team}
        team_document = self.getDocument("teams", team_query)
        team_projects = team_document.get("project_list", [])
        team_projects.append(new_project_id)
        newvalues = {"$set": {"project_list": team_projects}}
        self.db.teams.update_one(team_query, newvalues)

        self.recordHistory("createProject", user_email, str(new_project_id))

        return str(new_project_id)

    @schema_import.catalog_write
    def createRecordGroup(self, rg_info, user_info):
        if not isinstance(rg_info, dict):
            raise schema_rules.SchemaError("Record group data must be an object.")
        if not isinstance(rg_info.get("project_id"), str) or not ObjectId.is_valid(
            rg_info["project_id"]
        ):
            raise schema_rules.SchemaError("A valid project_id is required.")
        if "attributes" in rg_info and not isinstance(rg_info["attributes"], list):
            raise schema_rules.SchemaError(
                "Schema attributes must be an array of objects."
            )
        if rg_info.get("processorId") is not None and not isinstance(
            rg_info["processorId"], str
        ):
            raise schema_rules.SchemaError("processorId must be a string.")
        if not self.userCanAccessProject(rg_info.get("project_id"), user_info):
            raise PermissionError("You do not have access to this project.")
        rg_info = copy.deepcopy(rg_info)
        if rg_info.get("attributes"):
            self.requireSchemaPermission(user_info)
            rg_info["attributes"] = schema_rules.validate_fields(
                rg_info["attributes"], util.CLEANING_FUNCTIONS, require_types=False
            )
        ## get user's default team
        user_email = user_info.get("email", "")
        default_team = self.getDefaultTeamForUser(
            user_email, user_info.get("default_team")
        )
        if default_team is None:
            _log.info(f"user {user_email} has no default team")
            return False

        group_id = ObjectId()
        if USE_DB_PROCESSORS:
            if "schema_id" in rg_info:
                if rg_info.get("attributes") or rg_info.get("processorId"):
                    raise schema_rules.SchemaError(
                        "Select a schema without also supplying fields or a processor ID."
                    )
                if rg_info["schema_id"] is not None:
                    schema = self._schemaDocument(rg_info["schema_id"])
                    rg_info["schema_id"] = str(schema["_id"])
            elif rg_info.get("processorId"):
                schema = self.resolveRecordGroupSchema(rg_info, user_info, strict=True)
                rg_info["schema_id"] = str(schema["_id"])
            elif rg_info.get("attributes"):
                schema = self.createSchema(
                    {
                        "name": f"record-group-{group_id}",
                        "displayName": rg_info.get("name"),
                        "documentType": rg_info.get("documentType")
                        or "Imported records",
                        "attributes": rg_info["attributes"],
                    },
                    user_info,
                )
                rg_info["schema_id"] = schema["schema_id"]
            else:
                rg_info["schema_id"] = None
            rg_info.pop("attributes", None)
        else:
            if rg_info.get("schema_id"):
                raise schema_rules.SchemaError(
                    "Mongo schemas are inactive in repo mode.", 409
                )
            self.resolveRecordGroupSchema(rg_info, user_info, strict=True)

        ## add user and timestamp to record group
        rg_info["creator"] = user_info
        rg_info["_id"] = group_id
        rg_info["team"] = default_team
        rg_info["dateCreated"] = time.time()
        rg_info["settings"] = {}

        ## add record group to db collection
        db_response = self.db.record_groups.insert_one(rg_info)
        new_rg_id = db_response.inserted_id

        ## add record group to project's rg list:
        project_query = {"_id": ObjectId(rg_info.get("project_id", None))}
        _log.info(f"project_query: {project_query}")
        project_update = {"$push": {"record_groups": str(new_rg_id)}}

        _log.info(f"project_update: {project_update}")
        self.db.projects.update_one(project_query, project_update)

        self.recordHistory("createRecordGroup", user_email, rg_id=str(new_rg_id))

        return str(new_rg_id)

    def createRecord(self, record, user_info={}):
        user = user_info.get("email", None)
        schema_state = None
        if record.get("attributesList") and record.get("record_group_id"):
            group = self.db.record_groups.find_one(
                {"_id": ObjectId(record["record_group_id"])}
            )
            schema_state = self._recordSchema(group or {}, user_info)
            record.update(
                self._recordAttributeState(record["attributesList"], schema_state)
            )
        ## add timestamp to project
        record["dateCreated"] = time.time()

        # Atomically increment the counter and get the next number
        next_doc = self.db.counters.find_one_and_update(
            {"_id": "records"},
            {"$inc": {"record_number": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        record["record_number"] = next_doc["record_number"]

        ## add record to db collection
        db_response = self.db.records.insert_one(record)
        new_id = db_response.inserted_id
        if schema_state is not None:
            group = self.db.record_groups.find_one(
                {"_id": ObjectId(record["record_group_id"])}
            )
            current_state = self._recordSchema(group or {}, user_info)
            if current_state[2] != schema_state[2]:
                self._reconcileRecord(record, current_state, user_info)
        self.recordHistory("createRecord", user, record_id=str(new_id))
        return str(new_id)

    def createDirectoryUpload(self, rg_id, user_info, request):
        session_id, files, options = directory_upload.validate_manifest(request)
        self.getRecordGroupProcessingConfig(rg_id, user_info)
        existing_job = self.getProcessingJob(session_id)
        if existing_job and (
            existing_job["record_group_id"] != rg_id
            or existing_job["request_user"]["email"] != user_info["email"]
            or existing_job["input"].get("upload_session_id") != session_id
        ):
            raise ValueError("Upload session ID is already in use")
        session = {
            "_id": session_id,
            "record_group_id": rg_id,
            "user_email": user_info["email"],
            "files": files,
            "options": options,
            "bucket_name": storage_api.BUCKET_NAME,
            "status": "uploading",
            "created_at": time.time(),
            "expires_at": time.time() + directory_upload.SESSION_SECONDS,
        }
        result = self.db.directory_uploads.update_one(
            {"_id": session_id}, {"$setOnInsert": session}, upsert=True
        )
        existing = self.db.directory_uploads.find_one({"_id": session_id})
        if any(
            existing.get(key) != session[key]
            for key in (
                "record_group_id",
                "user_email",
                "files",
                "options",
                "bucket_name",
            )
        ):
            raise ValueError("Upload session ID is already in use")
        if result.upserted_id:
            self.recordHistory(
                "createDirectoryUpload",
                user_info["email"],
                rg_id=rg_id,
                notes={"session_id": session_id, "files": len(files)},
            )
        return existing

    def prepareProcessingRecord(self, record_id, record, user_info):
        existing = self.db.records.find_one({"_id": ObjectId(record_id)})
        if existing:
            self.db.records.update_one(
                {"_id": ObjectId(record_id)},
                {
                    "$set": {
                        "status": "processing",
                        "filename": record["filename"],
                        "image_files": record["image_files"],
                        "processing_attempt": record["processing_attempt"],
                    },
                    "$unset": {"error_message": ""},
                },
            )
            self.recordHistory(
                "prepareProcessingRecord"
                if existing.get("status") == "queued"
                else "retryProcessingRecord",
                user_info.get("email"),
                record_id=record_id,
                notes={"job_id": record["processing_job_id"]},
            )
        else:
            record["_id"] = ObjectId(record_id)
            self.createRecord(record, user_info)
        return record_id

    def getDirectoryUpload(self, session_id, rg_id, user_info):
        return self.db.directory_uploads.find_one(
            {
                "_id": session_id,
                "record_group_id": rg_id,
                "user_email": user_info["email"],
            }
        )

    def getDirectoryUploadFile(self, session, file_id, origin):
        directory_upload.require_open_session(session)
        item = next(
            (item for item in session["files"] if item["file_id"] == file_id), None
        )
        if item is None:
            raise ValueError("File is not part of this upload session")
        if storage_api.verify_directory_upload(
            session["bucket_name"], item, required=False
        ):
            return {"uploaded": True, "file_id": file_id}
        return {
            "uploaded": False,
            "file_id": file_id,
            "upload_url": storage_api.create_directory_upload_url(
                session["bucket_name"], item, origin
            ),
        }

    def finalizeDirectoryUpload(self, session, user_info):
        existing_job = self.getProcessingJob(session["_id"])
        if existing_job:
            if (
                existing_job["record_group_id"] != session["record_group_id"]
                or existing_job["request_user"]["email"] != user_info["email"]
                or existing_job["input"].get("upload_session_id") != session["_id"]
            ):
                raise ValueError("Upload session ID is already in use")
            return self.ensureDirectoryUploadRecords(existing_job)
        directory_upload.require_open_session(session)
        with ThreadPoolExecutor(max_workers=4) as pool:
            documents = list(
                pool.map(
                    lambda item: storage_api.verify_directory_upload(
                        session["bucket_name"], item
                    ),
                    session["files"],
                )
            )
        job = self.createBatchProcessingJob(
            session["record_group_id"],
            user_info,
            session["bucket_name"],
            prefix=f"{directory_upload.STAGING_PREFIX}/{session['_id']}/",
            job_id=session["_id"],
            documents=documents,
            output_prefix=f"directory_upload_outputs/{session['_id']}/",
            upload_expires_at=session["expires_at"],
            **session["options"],
        )
        self.db.directory_uploads.update_one(
            {"_id": session["_id"]}, {"$set": {"status": "submitted"}}
        )
        return self.ensureDirectoryUploadRecords(job)

    def ensureDirectoryUploadRecords(self, job):
        """Create bounded, metadata-only record placeholders before dispatch.

        Persist duplicate decisions before inserting placeholders so a repeated
        finalization cannot mistake this upload's own records for duplicates.
        Every insert is idempotent; interrupted initialization resumes on dispatch.
        """
        input_data = job["input"]
        if (
            not input_data.get("upload_session_id")
            or input_data.get("records_initialized")
            or job["status"] not in ("queued", "dispatched")
        ):
            return job
        job_id = job["job_id"]
        if not input_data.get("records_planned"):
            documents = [dict(item) for item in input_data.get("documents", [])]
            names = [os.path.splitext(item["name"])[0] for item in documents]
            seen = set()
            if job["options"]["prevent_duplicates"]:
                seen = {
                    record["name"]
                    for record in self.db.records.find(
                        {
                            "record_group_id": job["record_group_id"],
                            "processing_job_id": {"$ne": job_id},
                            "name": {"$in": names},
                        },
                        {"name": 1},
                    )
                }
            for item, name in zip(documents, names):
                item["skip_duplicate"] = bool(
                    job["options"]["prevent_duplicates"] and name in seen
                )
                seen.add(name)
            self.db.processing_jobs.update_one(
                {"_id": job_id, "input.records_planned": {"$ne": True}},
                {"$set": {"input.documents": documents, "input.records_planned": True}},
            )
            job = self.getProcessingJob(job_id)
            input_data = job["input"]

        records = []
        for item in input_data.get("documents", []):
            if item.get("skip_duplicate"):
                continue
            source_uri = f"gs://{input_data['bucket_name']}/{item['object_name']}"
            name = os.path.splitext(item["name"])[0]
            try:
                api_number = int(name.split("_")[0])
            except ValueError:
                api_number = None
            records.append(
                {
                    "_id": ObjectId(
                        directory_upload.processing_record_id(job_id, source_uri)
                    ),
                    "record_group_id": job["record_group_id"],
                    "name": name,
                    "filename": "",
                    "original_filename": item["name"],
                    "api_number": api_number,
                    "contributor": job["request_user"],
                    "status": "queued",
                    "review_status": "unreviewed",
                    "image_files": [],
                    "attributesList": [],
                    "processing_job_id": job_id,
                    "processing_attempt": job.get("attempt", 0),
                    "processing_source_uri": source_uri,
                    "dateCreated": job["created_at"],
                }
            )
        existing_ids = {
            record["_id"]
            for record in self.db.records.find(
                {"_id": {"$in": [record["_id"] for record in records]}}, {"_id": 1}
            )
        }
        missing = [record for record in records if record["_id"] not in existing_ids]
        if missing:
            counter = self.db.counters.find_one_and_update(
                {"_id": "records"},
                {"$inc": {"record_number": len(missing)}},
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            first_number = counter["record_number"] - len(missing) + 1
            operations = []
            for offset, record in enumerate(missing):
                record["record_number"] = first_number + offset
                operations.append(
                    UpdateOne(
                        {"_id": record["_id"]}, {"$setOnInsert": record}, upsert=True
                    )
                )
            self.db.records.bulk_write(operations, ordered=False)
        # Stable audit IDs repair a crash between record and history writes.
        # Initialization is complete only after both collections are written.
        history_ops = []
        for record in records:
            event_id = ObjectId(
                directory_upload.processing_record_id(
                    job_id, f"record-created:{record['_id']}"
                )
            )
            history_ops.append(
                UpdateOne(
                    {"_id": event_id},
                    {
                        "$setOnInsert": self._buildHistoryItem(
                            action="createRecord",
                            user=job["request_user"].get("email"),
                            rg_id=job["record_group_id"],
                            record_id=str(record["_id"]),
                            notes={"job_id": job_id, "status": "queued"},
                            timestamp=job["created_at"],
                        )
                    },
                    upsert=True,
                )
            )
        if history_ops:
            self.db.history.bulk_write(history_ops, ordered=False)
        return self.updateProcessingJob(job_id, {"input.records_initialized": True})

    # Processing jobs are intentionally separate from records. A job can survive
    # an API-pod restart and may create many records while it runs.
    def createBatchProcessingJob(
        self,
        rg_id,
        user_info,
        bucket_name,
        prefix="",
        output_bucket_name=None,
        output_prefix=None,
        run_cleaning_functions=True,
        prevent_duplicates=False,
        job_id=None,
        documents=None,
        upload_expires_at=None,
    ):
        job_id = job_id or uuid.uuid4().hex
        processing_config = self.getRecordGroupProcessingConfig(rg_id, user_info)
        now = time.time()
        request_user = {
            key: user_info.get(key)
            for key in ("email", "default_team", "team", "name", "collaborator")
            if user_info.get(key) is not None
        }
        job = {
            "_id": job_id,
            "job_id": job_id,
            "type": "batch_document",
            "status": "queued",
            "record_group_id": rg_id,
            "request_user": request_user,
            "processing_config": processing_config,
            "input": {
                "bucket_name": bucket_name,
                "prefix": prefix or "",
                "output_bucket_name": output_bucket_name or bucket_name,
                "output_prefix": output_prefix,
            },
            "options": {
                "run_cleaning_functions": bool(run_cleaning_functions),
                "prevent_duplicates": bool(prevent_duplicates),
            },
            "worker": {"kubernetes_job_name": None, "image": None},
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "batches_total": 0,
            "batches_completed": 0,
            "summary": {
                "total_submitted": 0,
                "total_succeeded": 0,
                "total_failed": 0,
                "total_skipped_duplicates": 0,
                "failed_document_uris": [],
                "skipped_duplicate_uris": [],
            },
            "error": None,
            "attempt": 0,
        }
        if documents is not None:
            job["input"]["documents"] = documents
            job["input"]["upload_session_id"] = job_id
            job["input"]["upload_expires_at"] = upload_expires_at
        result = self.db.processing_jobs.update_one(
            {"_id": job_id}, {"$setOnInsert": job}, upsert=True
        )
        if result.upserted_id:
            self.recordHistory(
                "createBatchProcessingJob",
                request_user.get("email"),
                rg_id=rg_id,
                notes={
                    "job_id": job_id,
                    "bucket_name": bucket_name,
                    "prefix": prefix or "",
                },
            )
        return self.getProcessingJob(job_id)

    def listProcessingJobs(self, rg_id):
        return [
            self._serializeProcessingJob(job)
            for job in self.db.processing_jobs.find(
                {"record_group_id": rg_id}, {"input.documents": 0}
            )
            .sort("created_at", DESCENDING)
            .limit(10)
        ]

    def getProcessingHistoryProjects(self, user_info):
        """Return only projects/groups visible through the user's current team."""
        projects = self.fetchProjects(user_info)
        group_ids = {
            str(group_id)
            for project in projects
            for group_id in project.get("record_groups", [])
        }
        names = {
            str(group["_id"]): group.get("name") or str(group["_id"])
            for group in self.db.record_groups.find(
                {
                    "_id": {
                        "$in": [
                            ObjectId(value)
                            for value in group_ids
                            if ObjectId.is_valid(value)
                        ]
                    }
                },
                {"name": 1},
            )
        }
        return sorted(
            [
                {
                    "id": str(project["_id"]),
                    "name": project.get("name") or str(project["_id"]),
                    "record_groups": sorted(
                        [
                            {
                                "id": str(group_id),
                                "name": names.get(str(group_id), str(group_id)),
                            }
                            for group_id in project.get("record_groups", [])
                        ],
                        key=lambda group: (group["name"].casefold(), group["id"]),
                    ),
                }
                for project in projects
            ],
            key=lambda project: (project["name"].casefold(), project["id"]),
        )

    def fetchAllProcessingJobHistory(self, user_info, body):
        if not isinstance(body, dict):
            raise ValueError("Expected a history query object")
        for key in ("project_id", "record_group_id"):
            if key in body and (
                not isinstance(body[key], str) or not ObjectId.is_valid(body[key])
            ):
                raise ValueError(f"Invalid {key}")
        projects = self.getProcessingHistoryProjects(user_info)
        project_id = body.get("project_id")
        if project_id is not None:
            projects = [project for project in projects if project["id"] == project_id]
            if not projects:
                raise PermissionError(
                    "You are not authorized to view this project's uploads"
                )
        groups = {
            group["id"]: {
                "project_id": project["id"],
                "project_name": project["name"],
                "record_group_name": group["name"],
            }
            for project in projects
            for group in project["record_groups"]
        }
        group_id = body.get("record_group_id")
        if group_id is not None:
            if group_id not in groups:
                raise PermissionError(
                    "This record group is not available in the selected projects"
                )
            groups = {group_id: groups[group_id]}
        # Apply the authorized group set after parsing client filters, even when empty.
        result = self._fetchProcessingJobHistory({"$in": list(groups)}, body)
        for job in result["active_jobs"] + result["jobs"]:
            job.update(groups[job["record_group_id"]])
        return result

    def fetchProcessingJobHistory(self, rg_id, body):
        return self._fetchProcessingJobHistory(rg_id, body)

    def _fetchProcessingJobHistory(self, group_scope, body):
        pagination, history_filter = processing_job_history.history_query(body)
        # Group scope is trusted and independent of all submitted filters.
        history_filter["record_group_id"] = group_scope
        active_filter = {
            "record_group_id": group_scope,
            "status": {"$in": processing_job_history.ACTIVE_STATUSES},
        }

        def page(query, number):
            return [
                self._serializeProcessingJob(job)
                for job in self.db.processing_jobs.aggregate(
                    [
                        {"$match": query},
                        {"$sort": {"created_at": -1, "_id": -1}},
                        {"$skip": number * pagination["page_size"]},
                        {"$limit": pagination["page_size"]},
                        {"$project": processing_job_history.summary_projection()},
                    ]
                )
            ]

        return {
            "active_jobs": page(active_filter, pagination["active_page"]),
            "active_count": self.db.processing_jobs.count_documents(active_filter),
            "jobs": page(history_filter, pagination["page"]),
            "count": self.db.processing_jobs.count_documents(history_filter),
        }

    def fetchProcessingJobDetails(self, rg_id, job_id, file_kind, page, page_size):
        query = {"_id": job_id, "record_group_id": rg_id}
        jobs = list(
            self.db.processing_jobs.aggregate(
                [
                    {"$match": query},
                    {"$project": processing_job_history.summary_projection()},
                ]
            )
        )
        if not jobs:
            return None
        job = self._serializeProcessingJob(jobs[0])
        offset = page * page_size
        if file_kind == "records":
            record_query = {"record_group_id": rg_id, "processing_job_id": job_id}
            files = [
                {
                    "record_id": str(record["_id"]),
                    "name": record.get("name", ""),
                    "status": record.get("status"),
                    "source_uri": record.get("processing_source_uri"),
                }
                for record in self.db.records.find(
                    record_query, {"name": 1, "status": 1, "processing_source_uri": 1}
                )
                .sort("_id", ASCENDING)
                .skip(offset)
                .limit(page_size)
            ]
            count = self.db.records.count_documents(record_query)
        else:
            field = {
                "source": "input.documents",
                "failed": "summary.failed_document_uris",
                "skipped": "summary.skipped_duplicate_uris",
            }[file_kind]
            result = list(
                self.db.processing_jobs.aggregate(
                    [
                        {"$match": query},
                        {
                            "$project": {
                                "files": {
                                    "$slice": [
                                        {"$ifNull": [f"${field}", []]},
                                        offset,
                                        page_size,
                                    ]
                                },
                                "count": {"$size": {"$ifNull": [f"${field}", []]}},
                            }
                        },
                    ]
                )
            )[0]
            count = result["count"]
            files = []
            for item in result["files"]:
                uri = (
                    f"gs://{job['input']['bucket_name']}/{item['object_name']}"
                    if isinstance(item, dict)
                    else item
                )
                files.append(
                    {
                        "source_uri": uri,
                        "name": item.get("relative_path", item["name"])
                        if isinstance(item, dict)
                        else uri.rsplit("/", 1)[-1],
                        "record_id": directory_upload.processing_record_id(job_id, uri),
                    }
                )
            records = {
                str(record["_id"]): record
                for record in self.db.records.find(
                    {
                        "record_group_id": rg_id,
                        "processing_job_id": job_id,
                        "_id": {"$in": [ObjectId(item["record_id"]) for item in files]},
                    },
                    {"status": 1},
                )
            }
            for item in files:
                record = records.get(item["record_id"])
                item["status"] = record.get("status") if record else None
                if record is None:
                    item.pop("record_id")
        return {"job": job, "files": files, "file_count": count}

    def processingJobRetryReason(self, job, user_info):
        if job["status"] not in ("error", "completed_with_errors"):
            return "Only failed processing can be retried."
        if not job["input"].get("upload_session_id"):
            return "GCS batches do not support retry here. Review the failed files before submitting another batch."
        if job["request_user"].get("email") != user_info.get("email"):
            return "Only the original uploader can retry this upload."
        if not self.hasPermission(user_info["email"], "upload_document"):
            return "Upload permission is required to retry."
        session = self.getDirectoryUpload(
            job["input"]["upload_session_id"], job["record_group_id"], user_info
        )
        if session is None or session["expires_at"] <= time.time():
            return "The upload has expired. Contact an administrator to recover failed records."
        return None

    def getActiveProcessingJobs(self):
        return [
            self._serializeProcessingJob(job)
            for statuses in (["dispatched", "running"], ["queued"])
            for job in self.db.processing_jobs.find(
                {"status": {"$in": statuses}},
                {"input.documents": 0},
            )
            .sort("created_at", ASCENDING)
            .limit(100)
        ]

    def reserveProcessingCapacity(self, job_id, maximum):
        if maximum <= 0:
            return True
        try:
            self.db.processing_capacity.update_one(
                {
                    "_id": "batch_document",
                    "$or": [
                        {"jobs": job_id},
                        {f"jobs.{maximum - 1}": {"$exists": False}},
                    ],
                },
                {"$addToSet": {"jobs": job_id}},
                upsert=True,
            )
            return True
        except DuplicateKeyError:
            return False

    def releaseProcessingCapacity(self, job_id):
        self.db.processing_capacity.update_one(
            {"_id": "batch_document"}, {"$pull": {"jobs": job_id}}
        )

    def retryProcessingJob(self, job_id):
        job = self.db.processing_jobs.find_one_and_update(
            {
                "_id": job_id,
                "status": {"$in": ["error", "completed_with_errors"]},
                "input.upload_session_id": {"$exists": True},
            },
            {
                "$set": {
                    "status": "queued",
                    "error": None,
                    "started_at": None,
                    "last_progress_at": None,
                    "stage": None,
                    "completed_at": None,
                    "updated_at": time.time(),
                    "worker": {},
                    "batches_completed": 0,
                    "summary": {
                        "total_submitted": 0,
                        "total_succeeded": 0,
                        "total_failed": 0,
                        "total_skipped_duplicates": 0,
                        "failed_document_uris": [],
                        "skipped_duplicate_uris": [],
                    },
                },
                "$inc": {"attempt": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if job:
            self.db.records.update_many(
                {
                    "processing_job_id": job_id,
                    "status": {"$in": ["queued", "processing", "error"]},
                },
                {
                    "$set": {"status": "queued", "processing_attempt": job["attempt"]},
                    "$unset": {"error_message": ""},
                },
            )
            self.recordHistory(
                "retryProcessingJob",
                job["request_user"]["email"],
                rg_id=job["record_group_id"],
                notes={"job_id": job_id},
            )
        return self._serializeProcessingJob(job)

    def beginProcessingJobDispatch(self, job_id, fields, attempt=0):
        result = self.db.processing_jobs.update_one(
            {
                "_id": job_id,
                "status": "queued",
                "attempt": {"$in": [0, None]} if attempt == 0 else attempt,
            },
            {"$set": {**fields, "status": "dispatched", "updated_at": time.time()}},
        )
        return result.modified_count == 1

    def hasActiveProcessingJobs(self, rg_id):
        return (
            self.db.processing_jobs.find_one(
                {
                    "record_group_id": rg_id,
                    "status": {"$in": ["queued", "dispatched", "running"]},
                },
                {"_id": 1},
            )
            is not None
        )

    def _serializeProcessingJob(self, job):
        if job is None:
            return None
        job = dict(job)
        job["job_id"] = str(job.get("job_id") or job.get("_id"))
        job.pop("_id", None)
        return job

    def getProcessingJob(self, job_id):
        return self._serializeProcessingJob(
            self.db.processing_jobs.find_one({"_id": str(job_id)})
        )

    def updateProcessingJob(self, job_id, fields):
        if not fields:
            return self.getProcessingJob(job_id)
        fields = dict(fields)
        fields["updated_at"] = time.time()
        self.db.processing_jobs.update_one({"_id": str(job_id)}, {"$set": fields})
        return self.getProcessingJob(job_id)

    def claimProcessingJob(self, job_id, attempt=0):
        now = time.time()
        job = self.db.processing_jobs.find_one_and_update(
            {
                "_id": str(job_id),
                "status": "dispatched",
                "attempt": {"$in": [0, None]} if attempt == 0 else attempt,
            },
            {
                "$set": {
                    "status": "running",
                    "started_at": now,
                    "last_progress_at": now,
                    "stage": "preparing_documents",
                    "updated_at": now,
                    "error": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        serialized_job = self._serializeProcessingJob(job)
        if serialized_job is not None:
            self.recordHistory(
                "startProcessingJob",
                serialized_job.get("request_user", {}).get("email"),
                rg_id=serialized_job.get("record_group_id"),
                notes={"job_id": serialized_job["job_id"]},
            )
        return serialized_job

    def incrementProcessingJobSummary(
        self,
        job_id,
        total_submitted=0,
        total_succeeded=0,
        total_failed=0,
        total_skipped_duplicates=0,
        failed_document_uris=None,
        skipped_duplicate_uris=None,
        batches_completed=0,
    ):
        update = {
            "$set": {"updated_at": time.time(), "last_progress_at": time.time()},
            "$inc": {
                "summary.total_submitted": total_submitted,
                "summary.total_succeeded": total_succeeded,
                "summary.total_failed": total_failed,
                "summary.total_skipped_duplicates": total_skipped_duplicates,
                "batches_completed": batches_completed,
            },
        }
        push_values = {}
        if failed_document_uris:
            push_values["summary.failed_document_uris"] = {
                "$each": failed_document_uris
            }
        if skipped_duplicate_uris:
            push_values["summary.skipped_duplicate_uris"] = {
                "$each": skipped_duplicate_uris
            }
        if push_values:
            update["$push"] = push_values
        self.db.processing_jobs.update_one({"_id": str(job_id)}, update)
        return self.getProcessingJob(job_id)

    def recordProcessingJobProgress(self, job_id, stage):
        self.db.processing_jobs.update_one(
            {"_id": job_id, "status": "running"},
            {
                "$set": {
                    "stage": stage,
                    "last_progress_at": time.time(),
                    "updated_at": time.time(),
                }
            },
        )

    def completeProcessingJob(self, job_id, status, error=None, attempt=None):
        if status not in ("completed", "completed_with_errors", "error"):
            raise ValueError(f"Unsupported processing job status: {status}")
        query = {"_id": job_id, "status": {"$in": ["queued", "dispatched", "running"]}}
        if attempt is not None:
            query["attempt"] = {"$in": [0, None]} if attempt == 0 else attempt
        current = self.db.processing_jobs.find_one(query)
        if current is None:
            return self.getProcessingJob(job_id)
        attempt = current.get("attempt", 0)
        query["attempt"] = {"$in": [0, None]} if attempt == 0 else attempt
        if status in ("error", "completed_with_errors"):
            # Repair records before the terminal write. If the API stops here,
            # maintenance can repeat the repair while the job is still active.
            self.db.records.update_many(
                {
                    "processing_job_id": job_id,
                    "processing_attempt": attempt,
                    "status": {"$in": ["queued", "processing"]},
                },
                {
                    "$set": {
                        "status": "error",
                        "error_message": "Processing did not finish. Review the job status before retrying.",
                    }
                },
            )
        job = self._serializeProcessingJob(
            self.db.processing_jobs.find_one_and_update(
                query,
                {
                    "$set": {
                        "status": status,
                        "completed_at": time.time(),
                        "updated_at": time.time(),
                        "error": str(error)[:2000] if error else None,
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
        )
        if job is not None:
            self.releaseProcessingCapacity(f"{job_id}:{job.get('attempt', 0)}")
            self.recordHistory(
                "completeProcessingJob",
                job.get("request_user", {}).get("email"),
                rg_id=job.get("record_group_id"),
                notes={"job_id": job["job_id"], "status": status},
            )
        return job or self.getProcessingJob(job_id)

    ## update functions
    def updateProject(self, project_id, new_data, user_info={}):
        user = user_info.get("email", None)
        _id = ObjectId(project_id)
        ## need to choose a subset of the data to update. can't update entire record because _id is immutable
        myquery = {"_id": _id}
        newvalues = {"$set": new_data}
        self.db.projects.update_one(myquery, newvalues)
        self.recordHistory("updateProject", user, project_id)
        cursor = self.db.projects.find(myquery)
        for document in cursor:
            document["_id"] = str(document["_id"])
            return document
        return None

    @schema_import.catalog_write
    def updateRecordGroup(self, rg_id, new_data, user_info=None):
        user_info = user_info or {}
        _, current = self.fetchRecordGroupData(rg_id, user_info)
        if current is None:
            raise PermissionError("You do not have access to this record group.")
        allowed = {
            "name",
            "description",
            "settings",
            "documentType",
            "processorId",
            "schema_id",
            "attributes",
            "source_type",
        }
        if not isinstance(new_data, dict) or set(new_data) - allowed:
            raise schema_rules.SchemaError("Unsupported record group update fields.")
        if "attributes" in new_data:
            self.requireSchemaPermission(user_info, destructive=True)
            raise schema_rules.SchemaError(
                "Edit fields on the shared schema, not on the record group."
            )
        if "settings" in new_data and not isinstance(new_data["settings"], dict):
            raise schema_rules.SchemaError("settings must be an object.")
        for key in set(new_data) - {"settings"}:
            if not isinstance(new_data[key], str) and not (
                key in {"processorId", "schema_id"} and new_data[key] is None
            ):
                raise schema_rules.SchemaError(f"{key} must be a string.")
        new_data = dict(new_data)
        binding_change = bool(set(new_data) & {"schema_id", "processorId"})
        if binding_change or "documentType" in new_data:
            self.requireSchemaPermission(user_info, destructive=True, require_db=False)
        if USE_DB_PROCESSORS:
            if "schema_id" in new_data and "processorId" in new_data:
                raise schema_rules.SchemaError(
                    "Use schema_id to select a database schema."
                )
            if "processorId" in new_data:
                processor_id = new_data.pop("processorId")
                schema = (
                    self.getMongoProcessorByID(processor_id) if processor_id else None
                )
                if processor_id and schema is None:
                    raise schema_rules.SchemaError("Schema not found.", 404)
                new_data["schema_id"] = schema["schema_id"] if schema else None
            if "schema_id" in new_data:
                schema_id = new_data["schema_id"]
                if schema_id is not None:
                    self._schemaDocument(schema_id)
        elif "schema_id" in new_data:
            raise schema_rules.SchemaError(
                "Mongo schemas are inactive in repo mode.", 409
            )
        elif "processorId" in new_data:
            self.resolveRecordGroupSchema(
                {"processorId": new_data["processorId"]}, user_info, strict=True
            )
        changes = {
            key: value
            for key, value in new_data.items()
            if key not in current or current[key] != value
        }
        if not changes:
            return current
        if binding_change:
            # Missing/ambiguous legacy bindings must still be repairable or detachable.
            try:
                self.resolveRecordGroupSchema(current, user_info, strict=True)
            except schema_rules.SchemaLookupError:
                pass
            else:
                self._ensureRecordGroupsReconciled([rg_id], user_info)
            self._ensureRecordGroupsReconciled(
                [rg_id],
                user_info,
                schema_state=self._recordSchema({**current, **changes}, user_info),
            )
        query = {"_id": ObjectId(rg_id)}
        for key in set(changes) | (
            {"schema_id", "processorId", "attributes"} if binding_change else set()
        ):
            query[key] = current[key] if key in current else {"$exists": False}
        result = self.db.record_groups.update_one(query, {"$set": changes})
        if not result.matched_count:
            raise schema_rules.SchemaError(
                "The record group changed. Reload before saving.", 409
            )
        if binding_change:
            self._ensureRecordGroupsReconciled([rg_id], user_info)
        self.recordHistory(
            "updateRecordGroup",
            user_info.get("email"),
            rg_id=rg_id,
            query=changes,
            previous_state={key: current.get(key) for key in changes},
        )
        return self._recordGroupSchemaInfo({**current, **changes}, user_info)

    def connectRecordGroupProcessor(
        self, rg_id, processor_id, user_info, schema_id=None
    ):
        self.requireSchemaPermission(user_info, destructive=True, require_db=False)
        if USE_DB_PROCESSORS:
            if schema_id is not None:
                schema = self._schemaDocument(schema_id)
            else:
                schema = self.getMongoProcessorByID(processor_id)
                if not schema:
                    raise schema_rules.SchemaError("Select a schema.")
            return self.updateRecordGroup(
                rg_id,
                {"schema_id": str(schema.get("_id") or schema["schema_id"])},
                user_info,
            )
        if schema_id is not None:
            raise schema_rules.SchemaError(
                "Mongo schemas are inactive in repo mode.", 409
            )
        processor = self._canonicalRepoProcessor(
            self.getProcessorById(processor_id, user_info)
        )
        if not processor:
            raise schema_rules.SchemaError("Processor not found.", 404)
        return self.updateRecordGroup(
            rg_id,
            {"processorId": processor_id, "documentType": processor["documentType"]},
            user_info,
        )

    def fetchRecordForUser(self, record_id, user_info):
        try:
            record = self.getDocument("records", {"_id": ObjectId(record_id)})
        except Exception:
            return None
        if not record:
            return None
        rg_id = record.get("record_group_id")
        if rg_id not in self.getUserRecordGroups(user_info):
            return None
        record["_id"] = str(record["_id"])
        return record

    def getRecordDisplayImageUrls(self, record):
        image_urls = []
        rg_id = record.get("record_group_id")
        record_id = str(record.get("_id"))
        for image in record.get("image_files") or []:
            if util.imageIsValid(image):
                image_urls.append(get_document_image(rg_id, record_id, image))
        return image_urls

    def appendRecordImages(self, record_id, image_files, user_info):
        record = self.fetchRecordForUser(record_id, user_info)
        if record is None:
            raise PermissionError("User does not have access to this record.")

        existing_image_files = record.get("image_files") or []
        next_image_files = existing_image_files[:]
        for image_file in image_files:
            if image_file and image_file not in next_image_files:
                next_image_files.append(image_file)

        self.db.records.update_one(
            {"_id": ObjectId(record_id)},
            {"$set": {"image_files": next_image_files}},
        )
        self.recordHistory(
            "appendRecordImages",
            user_info.get("email", None),
            record_id=record_id,
            notes={
                "added_image_count": len(next_image_files) - len(existing_image_files)
            },
        )
        record["image_files"] = next_image_files
        return {
            "record_id": record_id,
            "image_files": next_image_files,
            "img_urls": self.getRecordDisplayImageUrls(record),
        }

    @time_it
    def updateRecordInternal(self, record_id, field, value):
        _id = ObjectId(record_id)
        search_query = {"_id": _id}

        update_query = {"$set": {field: value}}
        update_resp = self.db.records.update_one(
            search_query,
            update_query,
        )
        return update_resp

    @staticmethod
    def _validateRecordAttributeEdit(previous, replacement):
        if not isinstance(replacement, list) or len(previous) != len(replacement):
            raise schema_rules.SchemaError(
                "Use the field actions to change record fields."
            )
        for old, new in zip(previous, replacement):
            if (
                not isinstance(new, dict)
                or old.get("key") != new.get("key")
                or bool(old.get("deleted")) != bool(new.get("deleted"))
            ):
                raise schema_rules.SchemaError(
                    "Record edits cannot rename or retire fields."
                )
            if old.get("deleted"):
                if old != new:
                    raise schema_rules.SchemaError("Retired fields cannot be edited.")
            else:
                DataManager._validateRecordAttributeEdit(
                    old.get("subattributes") or [], new.get("subattributes") or []
                )

    @time_it
    def updateRecord(
        self,
        record_id,
        new_data,
        update_type=None,
        field_to_clean=None,
        user_info=None,
        forceUpdate=False,
        notes=None,
        calling_function=None,
        expected_attribute_revision=None,
    ):
        public_edit = calling_function == "update_record"
        if not isinstance(new_data, dict):
            raise schema_rules.SchemaError("Record update data must be an object.")
        if public_edit and not self.fetchRecordForUser(record_id, user_info):
            raise PermissionError("You do not have access to this record.")
        user = (user_info or {}).get("email")
        if not forceUpdate and (
            not user_info or not self.tryLockingRecord(record_id, user)
        ):
            return False
        record = self.db.records.find_one({"_id": ObjectId(record_id)})
        if record is None:
            raise schema_rules.SchemaError("Record not found.", 404)
        attribute_operation = (
            update_type
            in {
                "attribute",
                "attributesList",
                "insertField",
                "deleteField",
                "updateFieldCoordinates",
            }
            or (
                update_type == "review_status"
                and new_data.get("review_status") == "unreviewed"
            )
            or (update_type == "record" and "attributesList" in new_data)
        )
        if public_edit and update_type == "record":
            allowed = {
                "name",
                "attributesList",
                "review_status",
                "verification_status",
                "defective_categories",
                "defective_description",
            }
            if set(new_data) - allowed:
                raise schema_rules.SchemaError("Unsupported record update fields.")
        if attribute_operation:
            record = self._reconcileRecord(record, user=user_info)
            if public_edit and expected_attribute_revision != record.get(
                "attribute_revision"
            ):
                raise schema_rules.SchemaError(
                    "The record fields changed. Reload the record before saving.", 409
                )
        original = copy.deepcopy(record)
        field_operations = {"insertField", "deleteField", "updateFieldCoordinates"}
        response_path = None
        if update_type == "attribute":
            indexes = self._getFieldIndexes(new_data)
            target, _ = self._getAttributeAtPath(
                record.get("attributesList") or [], indexes
            )
            if target is None:
                raise schema_rules.SchemaError(
                    "This record field is unavailable. Reload the record.", 409
                )
            value = copy.deepcopy(new_data.get("v"))
            self._validateRecordAttributeEdit([target], [value])
            if field_to_clean:
                self.cleanAttribute(value, record_id=record_id, user_info=user_info)
            parent_list, _, _ = self._getAttributeParentList(
                record["attributesList"], indexes
            )
            parent_list[indexes[-1]] = value
            data_update = {"attributesList": record["attributesList"]}
            response_path = util.attribute_index_path_to_mongo_path(
                indexes[0], indexes[1:]
            )
            if new_data.get("review_status") == "unreviewed":
                data_update["review_status"] = "incomplete"
        elif update_type in field_operations:
            indexes = self._getFieldIndexes(new_data)
            if (
                update_type != "insertField"
                and self._getAttributeAtPath(
                    record.get("attributesList") or [], indexes
                )[0]
                is None
            ):
                raise schema_rules.SchemaError(
                    "This record field is unavailable. Reload the record.", 409
                )
            data_update = self._updateRecordAttributesForFieldOperation(
                ObjectId(record_id), new_data, update_type, user, copy.deepcopy(record)
            )
            if not data_update:
                raise schema_rules.SchemaError("Invalid record field operation.")
        elif (
            update_type == "review_status"
            and new_data.get("review_status") == "unreviewed"
        ):
            data_update = self.resetRecord(record_id, record, user)
        elif update_type == "record":
            data_update = copy.deepcopy(new_data)
        elif update_type in {
            "name",
            "review_status",
            "verification_status",
            "attributesList",
        }:
            data_update = {update_type: copy.deepcopy(new_data.get(update_type))}
            if update_type == "verification_status" and new_data.get("review_status"):
                data_update["review_status"] = new_data["review_status"]
            if (
                update_type == "review_status"
                and new_data.get("review_status") == "incomplete"
            ):
                data_update["verification_status"] = None
            if (
                update_type == "review_status"
                and new_data.get("review_status") == "defective"
            ):
                data_update.update(
                    defective_categories=new_data.get("defective_categories", []),
                    defective_description=new_data.get("defective_description"),
                )
        else:
            raise schema_rules.SchemaError("Unsupported record update type.")
        if (
            public_edit
            and update_type in {"record", "attributesList"}
            and "attributesList" in data_update
        ):
            self._validateRecordAttributeEdit(
                original.get("attributesList") or [], data_update["attributesList"]
            )
        if "attributesList" in data_update:
            group = self.db.record_groups.find_one(
                {"_id": ObjectId(record["record_group_id"])}
            )
            schema_state = self._recordSchema(group or {}, user_info)
            if public_edit and schema_state[2] != original.get(
                "attribute_schema_revision"
            ):
                raise schema_rules.SchemaError(
                    "The schema changed. Reload the record before saving.", 409
                )
            attributes = util.preserve_retired_attributes(
                original.get("attributesList"), data_update["attributesList"]
            )
            data_update.update(self._recordAttributeState(attributes, schema_state))
        query = (
            self._originalAttributeQuery(original)
            if attribute_operation
            else {"_id": ObjectId(record_id)}
        )
        updated = self.db.records.find_one_and_update(
            query, {"$set": data_update}, return_document=ReturnDocument.AFTER
        )
        if updated is None:
            raise schema_rules.SchemaError(
                "The record changed. Reload before saving again.", 409
            )
        if not forceUpdate:
            history_update = data_update
            previous_state = {key: original.get(key) for key in data_update}
            if response_path:
                indexes = self._getFieldIndexes(new_data)
                history_update = {
                    response_path: self._getAttributeAtPath(
                        updated["attributesList"], indexes
                    )[0]
                }
                previous_state = {
                    response_path: self._getAttributeAtPath(
                        original["attributesList"], indexes
                    )[0]
                }
            self.recordHistory(
                "updateRecord",
                user,
                record_id=record_id,
                query=history_update,
                previous_state=previous_state,
                notes=notes,
                calling_function=calling_function,
                update_type=update_type,
            )
        if update_type in field_operations:
            updated["_id"] = str(updated["_id"])
            return updated
        if response_path:
            value, _ = self._getAttributeAtPath(
                updated["attributesList"], self._getFieldIndexes(new_data)
            )
            return {
                response_path: value,
                "attribute_revision": updated["attribute_revision"],
                "review_status": updated.get("review_status"),
            }
        return data_update

    def updateRecordNotes(self, record_id, data, user_info=None):
        # _log.info(f"updating {record_id} with {data}")
        if user_info is not None:
            user = user_info.get("email", None)
        else:
            user = None
        _id = ObjectId(record_id)
        search_query = {"_id": _id}
        update_type = data["update_type"]
        index = data.get("index", None)
        updates = []
        if update_type == "add":
            ##TODO: check if new index is really new (ie, less than length of list).
            ## in the case that two users simultaneously add notes, there could be a race here
            newNoteText = data["text"]
            isReply = data.get("isReply", False)
            newNote = {
                "text": newNoteText,
                "record_id": record_id,
                "timestamp": time.time(),
                "creator": user,
                "resolved": False,
                "deleted": False,
                "lastUpdated": time.time(),
                "replies": [],
                "isReply": isReply,
                "lastUpdatedUser": user,
            }
            if isReply:
                replyToIndex = data["replyToIndex"]
                newNote["repliesTo"] = replyToIndex
                update1 = {
                    "$push": {
                        "record_notes": newNote,  ## add new note
                    }
                }
                update2 = {
                    "$push": {
                        f"record_notes.{replyToIndex}.replies": index,  ## add index to reply list
                    }
                }
                updates.append(update1)
                updates.append(update2)
            else:
                update = {"$push": {"record_notes": newNote}}
                updates.append(update)
        elif update_type == "edit":
            updatedText = data["text"]
            update = {
                "$set": {
                    f"record_notes.{index}.text": updatedText,
                    f"record_notes.{index}.lastUpdated": time.time(),
                    f"record_notes.{index}.lastUpdatedUser": user,
                }
            }
            updates.append(update)
        elif update_type == "delete":
            update = {
                "$set": {
                    f"record_notes.{index}.deleted": True,
                    f"record_notes.{index}.lastUpdated": time.time(),
                    f"record_notes.{index}.lastUpdatedUser": user,
                }
            }
            updates.append(update)
        elif update_type == "resolve" or update_type == "unresolve":
            new_resolve_value = False
            if update_type == "resolve":
                new_resolve_value = True
            update = {
                "$set": {
                    f"record_notes.{index}.resolved": new_resolve_value,
                    f"record_notes.{index}.lastUpdated": time.time(),
                    f"record_notes.{index}.lastUpdatedUser": user,
                }
            }
            updates.append(update)
        else:
            _log.error(f"invalid update type: {update_type}")
            return None

        for update in updates:
            self.db.records.update_one(search_query, update)
            self.recordHistory(
                "updateRecordNotes",
                user,
                record_id=record_id,
                query=update,
                notes="updateRecordNotes",
                calling_function="updateRecordNotes",
            )
        record_doc = self.db.records.find(search_query).next()
        return record_doc.get("record_notes", [])

    def create_record_group_processor_attribute_map(self, user=None):
        try:
            cursor = self.db.record_groups.find(
                {},
                {
                    "_id": 1,
                    "processorId": 1,
                    "attributes": 1,
                    "schema_id": 1,
                },
            )
            rg_processor_attribute_map = {}
            for rg in cursor:
                rg_id = str(rg["_id"])
                processor_attributes = self.getRecordGroupSchemaAttributes(
                    rg_document=rg, user=user
                )
                rg_processor_attribute_map[
                    rg_id
                ] = util.convert_processor_attributes_to_dict(processor_attributes)
            # _log.info(f"rg_processor_attribute_map: {rg_processor_attribute_map}")
            return rg_processor_attribute_map
        except Exception as e:
            _log.error(f"failed: {e}")
            return {}

    def resetRecord(self, record_id, record_data, user):
        # print(f"resetting record: {record_id}")
        record_attributes = util.normalize_record_attribute_tree(
            record_data["attributesList"]
        )

        def reset_attribute(attribute):
            if attribute.get("deleted"):
                return
            original_value = attribute.get("raw_text")
            attribute["value"] = original_value
            attribute["confidence"] = attribute.get("ai_confidence", None)
            attribute["edited"] = False
            attribute["cleaning_error"] = False
            attribute["uncleaned_value"] = None
            attribute["cleaned"] = False
            attribute["last_cleaned"] = None

            kept_subattributes = []
            for subattribute in attribute.get("subattributes") or []:
                if subattribute.get("user_added", False) and not subattribute.get(
                    "deleted"
                ):
                    _log.info(f"deleting user-added subfield: {subattribute}")
                    continue
                reset_attribute(subattribute)
                kept_subattributes.append(subattribute)
            attribute["subattributes"] = kept_subattributes

        kept_attributes = []
        for attribute in record_attributes:
            if attribute.get("user_added", False) and not attribute.get("deleted"):
                _log.info(f"deleting user-added field: {attribute}")
                continue
            reset_attribute(attribute)
            kept_attributes.append(attribute)

        update = {
            "review_status": "unreviewed",
            "attributesList": kept_attributes,
            "verification_status": None,
        }
        # history is recorded in the function that calls this
        return update

    ## delete functions
    def deleteProject(self, project_id, background_tasks, user_info):
        ## TODO: check if user is a part of the team who owns this project
        _log.info(f"deleting project {project_id}")
        _id = ObjectId(project_id)
        myquery = {"_id": _id}

        ## add to deleted projects collection first
        project_cursor = self.db.projects.find(myquery)
        project_document = project_cursor.next()
        project_document["deleted_by"] = user_info
        team = project_document.get("team", "")
        self.db.deleted_projects.insert_one(project_document)

        ## delete from projects collection
        self.db.projects.delete_one(myquery)

        ## delete record groups
        record_groups = project_document.get("record_groups", [])
        self.deleteRecordGroups(record_groups=record_groups, deletedBy=user_info)

        ## add records to deleted records collection and remove from records collection
        background_tasks.add_task(
            self._deleteRecords,
            query={"record_group_id": {"$in": record_groups}},
            deletedBy=user_info,
        )

        self.recordHistory(
            "deleteProject", user_info.get("email", None), project_id=project_id
        )

        self.removeProjectFromTeam(_id, team)
        return "success"

    def deleteRecordGroup(self, rg_id, background_tasks, user_info):
        _log.info(f"deleting record group {rg_id}")
        _id = ObjectId(rg_id)
        myquery = {"_id": _id}

        ## add to deleted record groups collection first
        record_group_cursor = self.db.record_groups.find(myquery)
        record_group_doc = record_group_cursor.next()
        record_group_doc["deleted_by"] = user_info
        team = record_group_doc.get("team", "")
        self.db.deleted_record_groups.insert_one(record_group_doc)

        ## delete from record groups collection
        self.db.record_groups.delete_one(myquery)

        ## add records to deleted records collection and remove from records collection
        background_tasks.add_task(
            self._deleteRecords,
            query={"record_group_id": rg_id},
            deletedBy=user_info,
        )

        self.recordHistory(
            "deleteRecordGroup", user_info.get("email", None), rg_id=rg_id
        )

        ## remove from project list
        self.removeRecordGroupFromProject(rg_id)

        self.removeRecordGroupFromTeam(_id, team)
        return "success"

    def deleteRecords(self, record_ids, user_info):
        _ids = [ObjectId(record_id) for record_id in record_ids]
        myquery = {"_id": {"$in": _ids}}
        self._deleteRecords(query=myquery, deletedBy=user_info)
        self.recordHistory(
            "deleteRecords", user=user_info.get("email", None), notes=myquery
        )
        return "success"

    def deleteRecordsByRecordGroup(self, rg_id, filter_by, user_info):
        query = dict(filter_by or {})
        query["record_group_id"] = rg_id
        ids = [
            record["_id"]
            for record in self.db.records.aggregate(
                util.active_records_pipeline(query) + [{"$project": {"_id": 1}}]
            )
        ]
        self._deleteRecords(
            query={"record_group_id": rg_id, "_id": {"$in": ids}}, deletedBy=user_info
        )
        self.recordHistory(
            "deleteRecordGroupRecords",
            user=user_info.get("email", None),
            rg_id=rg_id,
            notes=query,
        )
        return "success"

    def _moveDeletedRecordImages(self, record_document):
        record_id = str(record_document.get("_id", ""))
        record_group_id = record_document.get("record_group_id")
        if not record_id or not record_group_id:
            _log.info(
                f"cannot move deleted record images without record id and record group id: {record_document}"
            )
            return

        try:
            moved = storage_api.move_record_images_to_deleted(
                record_group_id, record_id
            )
            if moved:
                _log.info(
                    f"moved deleted record images from "
                    f"{storage_api.get_record_image_directory(record_group_id, record_id)} "
                    f"to {storage_api.get_deleted_record_image_directory(record_id)}"
                )
            else:
                _log.info(
                    f"no record image directory found at "
                    f"{storage_api.get_record_image_directory(record_group_id, record_id)}"
                )
        except Exception as e:
            _log.error(f"unable to move deleted record images for {record_id}: {e}")

    def _deleteRecords(self, query, deletedBy):
        user = deletedBy.get("email", None)
        _log.info(f"deleting records with query: {query}")
        ## add records to deleted records collection
        record_cursor = self.db.records.find(query)
        try:
            for record_document in record_cursor:
                record_document["deleted_by"] = user
                self.db.deleted_records.insert_one(record_document)
                # Storage key layout is owned by storage_api.
                self._moveDeletedRecordImages(record_document)
        except Exception as e:
            _log.error(f"unable to move all deleted records: {e}")

        ## Delete active records after archiving their documents and images.
        resp = self.db.records.delete_many(query)
        _log.info(f"delete resp = {resp}")

        return "success"

    def deleteRecordGroups(self, record_groups, deletedBy):
        user = deletedBy.get("email", None)
        _log.info(f"deleting record groups: {record_groups}")
        record_group_ids = []
        for i in range(len(record_groups)):
            record_group_ids.append(ObjectId(record_groups[i]))
        ## add to deleted records collection
        query = {"_id": {"$in": record_group_ids}}
        cursor = self.db.record_groups.find(query)
        try:
            for document in cursor:
                document["deleted_by"] = deletedBy
                self.db.deleted_record_groups.insert_one(document)
        except Exception as e:
            _log.error(f"unable to move all deleted record groups: {e}")

        ## Delete records associated with this project
        self.db.record_groups.delete_many(query)
        return "success"

    def removeProjectFromTeam(self, project_id, team):
        team_query = {"name": team}
        update = {"$pull": {"project_list": project_id}}
        self.db.teams.update_many(team_query, update)

    def removeRecordGroupFromProject(self, rg_id):
        query = {"record_groups": rg_id}
        update = {"$pull": {"record_groups": rg_id}}
        self.db.projects.update_many(query, update)

    def removeRecordGroupFromTeam(self, rg_id, team):
        team_query = {"name": team}
        update = {"$pull": {"record_groups": rg_id}}
        self.db.teams.update_many(team_query, update)

    @time_it
    def organizeRecordsByDocumentType(self, records):
        rg_processor_map = {}
        setsOfRecords = {}
        for record in records:
            record_group_id = record.get("record_group_id")
            processor_name = rg_processor_map.get(record_group_id, None)
            if not processor_name:
                processor_name = self.getProcessorByRecordGroupID(
                    record_group_id, returnNameOnly=True
                )
                rg_processor_map[record_group_id] = processor_name
                setsOfRecords[processor_name] = [record]
            else:
                setsOfRecords[processor_name].append(record)
        return setsOfRecords

    ## miscellaneous functions
    def downloadRecords(
        self,
        records,
        exportType,
        user_info,
        _id,
        location,
        selectedColumns=[],
        keep_all_columns=False,
        output_filename=None,
        request_origin="",
    ):
        schema_states = {}
        export_records = []
        for record in records:
            group_id = record["record_group_id"]
            if group_id not in schema_states:
                group = self.db.record_groups.find_one({"_id": ObjectId(group_id)})
                schema_states[group_id] = self._recordSchema(group or {}, user_info)
            schema, keep_unknown, _ = schema_states[group_id]
            attributes, _ = util.sortRecordAttributes(
                record.get("attributesList"),
                schema,
                keep_all_attributes=keep_unknown,
                add_missing_attributes=False,
            )
            export_records.append(
                {
                    **record,
                    "attributesList": util.active_attributes(attributes),
                }
            )
        records = export_records
        ## TODO: Should we use aliases for export?
        USE_ALIASES = True
        user = user_info.get("email", None)
        rg_attribute_map = self.create_record_group_processor_attribute_map(user_info)
        today = time.time()
        output_dir = self.app_settings.export_dir
        if output_filename is None:
            output_file = os.path.join(output_dir, f"{_id}_{today}.{exportType}")
        else:
            output_file = f"{output_filename}.{exportType}"
        attributes = ["file"]
        subattributes = []
        record_attributes = []

        def add_subattributes_to_csv_row(
            record_attribute,
            subattribute_columns,
            parent_column_name,
            document_subattributes,
            record_group_id,
        ):
            for document_subattribute in document_subattributes or []:
                subattribute_key = document_subattribute["key"]

                ## TODO: use alias?
                rg_schema = rg_attribute_map.get(record_group_id, {})
                subattribute_alias = self._getAttributeAlias(
                    document_subattribute, rg_schema
                )
                # if not subattribute_alias:
                #     _log.info(f"could not find subattribute_alias for {subattribute_key}")
                if USE_ALIASES and subattribute_alias:
                    subattribute_name = f"{parent_column_name}[{subattribute_alias}"
                else:
                    subattribute_name = f"{parent_column_name}[{subattribute_key}"

                original_subattribute_name = subattribute_name
                i = 2
                while (
                    subattribute_name in current_attributes
                    or subattribute_name in current_parent_attributes
                ):
                    ## add a number to the end of the attribute so it
                    ## is differentiable from other instances of the attribute
                    subattribute_name = f"{original_subattribute_name}_{i}"
                    i += 1
                current_attributes.add(subattribute_name)
                subattribute_name = f"{subattribute_name}]"

                subattribute_contains_subattributes = len(
                    document_subattribute.get("subattributes") or []
                )
                if not subattribute_contains_subattributes:
                    record_attribute[subattribute_name] = document_subattribute.get(
                        "value"
                    )
                    if subattribute_name not in subattribute_columns:
                        subattribute_columns.append(subattribute_name)
                else:
                    _log.info(
                        f"subattribute {subattribute_name} contains subattributes, not adding it"
                    )

                add_subattributes_to_csv_row(
                    record_attribute,
                    subattribute_columns,
                    subattribute_name,
                    document_subattribute.get("subattributes") or [],
                    record_group_id=record_group_id,
                )

        if exportType == "csv":
            for document in records:
                record_group_id = document["record_group_id"]
                document_id = str(document["_id"])
                try:
                    current_attributes = set()
                    current_parent_attributes = set()
                    record_attribute = {}
                    for document_attribute in document.get("attributesList", []):
                        attribute_key = document_attribute["key"].replace(" ", "")

                        ## TODO: use alias?
                        rg_schema = rg_attribute_map.get(record_group_id, {})
                        attribute_alias = self._getAttributeAlias(
                            document_attribute, rg_schema
                        )
                        if USE_ALIASES and attribute_alias:
                            attribute_name = f"{attribute_alias}"
                        else:
                            attribute_name = attribute_key

                        if (
                            document_attribute["key"] in selectedColumns
                            or keep_all_columns
                        ):
                            field_schema = (
                                rg_attribute_map.get(record_group_id, {}).get(
                                    attribute_name
                                )
                                or {}
                            )
                            database_type = field_schema.get("database_data_type")
                            if str(database_type).lower() == "table":
                                isParent = True
                            else:
                                isParent = False
                            original_attribute_name = attribute_name
                            i = 2
                            while (
                                attribute_name in current_attributes
                                or attribute_name in current_parent_attributes
                            ):
                                ## add a number to the end of the attribute so it (and its subattributes)
                                ## is differentiable from other instances of the attribute
                                attribute_name = f"{original_attribute_name}_{i}"
                                i += 1
                            if document_attribute.get("subattributes", None):
                                current_parent_attributes.add(attribute_name)
                                add_subattributes_to_csv_row(
                                    record_attribute,
                                    subattributes,
                                    attribute_name,
                                    document_attribute.get("subattributes") or [],
                                    record_group_id=record_group_id,
                                )
                            elif not isParent:
                                current_attributes.add(attribute_name)
                                if attribute_name not in attributes:
                                    attributes.append(attribute_name)
                                record_attribute[attribute_name] = document_attribute[
                                    "value"
                                ]

                    record_attribute["file"] = document.get("filename", "")
                    if "record_notes" in selectedColumns or keep_all_columns:
                        notes_list = document.get("record_notes") or []
                        active_notes = [
                            note
                            for note in notes_list
                            if not note.get("deleted", False)
                        ]
                        formatted_notes = []
                        for note in active_notes:
                            creator = note.get("creator", "Unknown")
                            text = note.get("text", "")
                            formatted_notes.append(f"{creator}: {text}")
                        record_attribute["record_notes"] = "; ".join(formatted_notes)
                        if "record_notes" not in attributes:
                            attributes.append("record_notes")
                    record_attribute["URL"] = f"{request_origin}/record/{document_id}"
                    record_attributes.append(record_attribute)
                except Exception as e:
                    _log.info(f"unable to add {document_id}: {e}")
            # compute the output file directory and name
            with open(output_file, "w", newline="") as csvfile:
                writer = csv.DictWriter(
                    csvfile, fieldnames=attributes + subattributes + ["URL"]
                )
                writer.writeheader()
                writer.writerows(record_attributes)
        else:  ## export type is JSON
            for document in records:
                document_id = str(document["_id"])
                try:
                    record_attribute = {}
                    for document_attribute in document.get("attributesList", []):
                        attribute_name = document_attribute["key"]
                        if attribute_name in selectedColumns or keep_all_columns:
                            record_attribute[attribute_name] = document_attribute
                    if "record_notes" in selectedColumns or keep_all_columns:
                        notes_list = document.get("record_notes") or []
                        active_notes = [
                            note
                            for note in notes_list
                            if not note.get("deleted", False)
                        ]
                        record_attribute["record_notes"] = active_notes
                    record_attribute["file"] = document.get("filename", "")
                    record_attributes.append(record_attribute)
                except Exception as e:
                    _log.info(f"unable to add {document_id}: {e}")
            with open(output_file, "w", newline="") as jsonfile:
                json.dump(
                    record_attributes, jsonfile, default=util.defaultJSONDumpHandler
                )

        if location == "project":
            self.recordHistory("downloadRecords", user=user, project_id=_id)
        elif location == "record_group":
            self.recordHistory("downloadRecords", user=user, rg_id=_id)
        elif location == "team":
            self.recordHistory(
                "downloadRecords", user=user, notes="downloaded team records"
            )
        return output_file

    def getUserPermissions(self, user):
        user_team = user["default_team"]
        roles = user.get("roles", {})

        system_roles = roles.get("system", [])
        team_roles = roles.get("team", {}).get(user_team, [])

        ## compile permissions from each role
        role_filters = []
        if system_roles:
            role_filters.append({"id": {"$in": system_roles}, "category": "system"})
        if team_roles:
            role_filters.append({"id": {"$in": team_roles}, "category": "team"})
        if not role_filters:
            return []

        query = {"$or": role_filters}
        role_cursor = self.db.roles.find(query)
        user_permissions = set()
        for each in role_cursor:
            for perm in each["permissions"]:
                user_permissions.add(perm)

        if "sys_admin" not in system_roles:
            user_permissions.discard(schema_rules.DESTRUCTIVE_PERMISSION)

        return list(user_permissions)

    def checkProjectValidity(self, projectId):
        try:
            project_id = ObjectId(projectId)
        except:
            return False
        project = self.getDocument("projects", {"_id": project_id})
        if project is not None:
            return True

    @time_it
    def checkIfRecordExists(self, filename, rg_id):
        return len(self.checkIfRecordsExist([filename], rg_id)) > 0

    @time_it
    def checkIfRecordsExist(self, filenames, rg_id):
        bases = {self.getFilenameBase(f) for f in filenames if self.getFilenameBase(f)}
        if not bases:
            return []

        record_cursor = self.db.records.find(
            {"record_group_id": rg_id}, {"filename": 1}
        )
        duplicate_records = set()
        for document in record_cursor:
            filename_base = self.getFilenameBase(document.get("filename", ""))
            if filename_base in bases:
                duplicate_records.add(filename_base)
        return list(duplicate_records)

    def getFilenameBase(self, filename):
        return os.path.splitext(os.path.basename(str(filename or "")))[0]

    def checkRecordGroupValidity(self, rg_id):
        try:
            rg_id = ObjectId(rg_id)
        except:
            return False
        rg = self.getDocument("record_groups", {"_id": rg_id})
        if rg is not None:
            return True

    def _getHistoryNumericType(self, value):
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return "int"
        if isinstance(value, float):
            return "float"
        return None

    def _annotateHistoryAttributesNumericTypes(self, attributes):
        if not isinstance(attributes, list):
            return

        for attribute in attributes:
            if not isinstance(attribute, dict):
                continue

            numeric_type = self._getHistoryNumericType(attribute.get("value"))
            if numeric_type is not None:
                attribute["value_numeric_type"] = numeric_type

            subattributes = attribute.get("subattributes")
            if isinstance(subattributes, list):
                self._annotateHistoryAttributesNumericTypes(subattributes)

    def _annotateHistoryPayloadNumericTypes(self, payload):
        if isinstance(payload, list):
            self._annotateHistoryAttributesNumericTypes(payload)
            for entry in payload:
                if isinstance(entry, (dict, list)):
                    self._annotateHistoryPayloadNumericTypes(entry)
            return payload

        if not isinstance(payload, dict):
            return payload

        if "key" in payload and "value" in payload:
            numeric_type = self._getHistoryNumericType(payload.get("value"))
            if numeric_type is not None:
                payload["value_numeric_type"] = numeric_type

        for key, value in payload.items():
            if key == "attributesList" and isinstance(value, list):
                self._annotateHistoryAttributesNumericTypes(value)
                continue
            if key.startswith("attributesList.") and isinstance(value, dict):
                self._annotateHistoryPayloadNumericTypes(value)
                continue
            if isinstance(value, (dict, list)):
                self._annotateHistoryPayloadNumericTypes(value)

        return payload

    def _getAttributeAlias(self, attribute, schema):
        attribute_key = attribute.get("key")
        # _log.info(f"{attribute}")
        parentAttribute = attribute.get("parentAttribute") or attribute.get(
            "topLevelAttribute"
        )
        schemaKey = (
            attribute_key
            if not parentAttribute
            else f"{parentAttribute}::{attribute_key}"
        )
        attribute_schema = schema.get(schemaKey)
        if attribute_schema:
            alias = attribute_schema.get("alias")
            _log.info(f"we found an attribute schema: {alias}")
            return alias
        else:
            _log.info(f"NO attribute schema for: {schemaKey}")
            _log.info(attribute)
        return None

    def _buildHistoryItem(
        self,
        action=None,
        user: str = None,
        project_id=None,
        rg_id=None,
        record_id=None,
        notes=None,
        query=None,
        previous_state=None,
        calling_function=None,
        timestamp=None,
        **kwargs,
    ):
        history_item = {
            "action": action,
            "user": user,
            "project_id": project_id,
            "record_group_id": rg_id,
            "record_id": record_id,
            "notes": notes,
            "query": query,
            "previous_state": previous_state,
            "calling_function": calling_function,
            "timestamp": timestamp if timestamp is not None else time.time(),
        }

        extra_fields = dict(kwargs)
        if (
            extra_fields.get("record_group_id") is None
            and extra_fields.get("rg_id") is not None
        ):
            extra_fields["record_group_id"] = extra_fields["rg_id"]
        extra_fields.pop("rg_id", None)
        history_item.update(extra_fields)
        return history_item

    def recordHistory(
        self,
        action,
        user: str = None,
        project_id=None,
        rg_id=None,
        record_id=None,
        notes=None,
        query=None,
        previous_state=None,
        calling_function=None,
        **kwargs,
    ):
        try:
            history_item = self._buildHistoryItem(
                action=action,
                user=user,
                project_id=project_id,
                rg_id=rg_id,
                record_id=record_id,
                notes=notes,
                query=query,
                previous_state=previous_state,
                calling_function=calling_function,
                **kwargs,
            )
            self.db.history.insert_one(history_item)
        except Exception as e:
            _log.error(f"unable to record history item: {e}")

    def recordHistoryBulk(self, updates):
        if not updates:
            return
        try:
            ts = time.time()
            history_ops = []
            for update in updates:
                if not isinstance(update, dict):
                    continue
                history_item = self._buildHistoryItem(
                    timestamp=update.get("timestamp", ts),
                    **update,
                )
                history_ops.append(InsertOne(history_item))
            if history_ops:
                self.db.history.bulk_write(history_ops, ordered=False)
        except Exception as e:
            _log.error(f"unable to record bulk history items: {e}")

    def cleanAttribute(self, attribute, record_id=None, rg_id=None, user_info=None):
        if record_id is None and rg_id is None:
            return None
        if rg_id is not None:
            _, _, processor_attributes = self.getProcessorByRecordGroupID(
                rg_id, user=user_info
            )
        else:
            _, _, processor_attributes = self.getProcessorByRecordID(
                record_id, user=user_info
            )

        ## convert processor attributes to dict
        processor_attributes = util.convert_processor_attributes_to_dict(
            processor_attributes
        )

        if attribute.get("isSubattribute", False):
            subattribute_identifier = util.get_attribute_identifier(attribute)
            util.cleanRecordAttribute(
                processor_attributes=processor_attributes,
                attribute=attribute,
                subattributeKey=subattribute_identifier,
            )
        else:
            util.cleanRecordAttribute(
                processor_attributes=processor_attributes, attribute=attribute
            )

    def cleanCollection(self, location, _id, user_info):
        if location == "record":
            record = self.fetchRecordForUser(_id, user_info)
            if not record:
                raise PermissionError("You do not have access to this record.")
            query = {"_id": ObjectId(_id)}
            group_id = record["record_group_id"]
        elif location == "record_group":
            if _id not in self.getUserRecordGroups(user_info):
                raise PermissionError("You do not have access to this record group.")
            query = {"record_group_id": _id}
            group_id = _id
        else:
            raise schema_rules.SchemaError(
                "Cleaning is supported for records and record groups."
            )
        self._ensureRecordGroupsReconciled([group_id], user_info)
        group = self.db.record_groups.find_one({"_id": ObjectId(group_id)})
        schema_state = self._recordSchema(group or {}, user_info)
        if schema_state[0] is None:
            raise schema_rules.SchemaError(
                "This record group has no active schema for cleaning.", 409
            )
        schema_map = util.convert_processor_attributes_to_dict(
            (schema_state[0] or {}).get("attributes")
        )
        for record in self.db.records.find(query).batch_size(100):
            current_group = self.db.record_groups.find_one({"_id": ObjectId(group_id)})
            if self._recordSchema(current_group or {}, user_info)[2] != schema_state[2]:
                raise schema_rules.SchemaError(
                    "The schema changed during cleaning. Completed records were saved; refresh and retry.",
                    409,
                )
            record = self._reconcileRecord(record, schema_state, user_info)
            original = copy.deepcopy(record)
            history = util.cleanRecords(schema_map, [record])[str(record["_id"])]
            changes = self._recordAttributeState(
                record.get("attributesList"), schema_state
            )
            if not self.db.records.update_one(
                self._originalAttributeQuery(original), {"$set": changes}
            ).matched_count:
                raise schema_rules.SchemaError(
                    "A record changed during cleaning. Completed records were saved; retry the remaining work.",
                    409,
                )
            self.recordHistory(
                "cleanRecord",
                user_info.get("email"),
                record_id=str(record["_id"]),
                rg_id=group_id,
                **history,
            )
        return True

    def getRecordImageFileUrlPairs(self, record_id, rg_id):
        """
        Get the URLs for all images in a record.

        Args:
            record_id: The record ID
            rg_id: The record group ID

        Returns:
            A list of tuples: [(image_filename, image_url), ...]
        """
        try:
            _id = ObjectId(record_id)
            document = self.db.records.find_one({"_id": _id})
            if not document:
                return []

            image_urls = []
            image_files = document.get("image_files", [])
            for image in image_files:
                if util.imageIsValid(image):
                    image_url = get_document_image(rg_id, record_id, image)
                    image_urls.append((image, image_url))

            # Fallback to filename if no image_files
            if len(image_urls) == 0 and document.get("filename"):
                image_url = get_document_image(rg_id, record_id, document["filename"])
                image_urls.append((document["filename"], image_url))

            return image_urls
        except Exception as e:
            _log.error(f"Error getting record image URLs: {e}")
            return []

    def updateRecordImageFiles(self, record_id, new_image_filenames, user_info):
        """
        Update the image_files list for a record after rotation.

        Args:
            record_id: The record ID
            new_image_filenames: List of new image filenames
            user_info: User information for logging/history

        Returns:
            True if successful, False otherwise
        """
        try:
            _id = ObjectId(record_id)
            search_query = {"_id": _id}
            user = user_info.get("email", None) if user_info else None

            # Update the image_files field
            update_query = {"$set": {"image_files": new_image_filenames}}
            result = self.db.records.update_one(search_query, update_query)

            if result.modified_count > 0:
                # Record history of the update
                self.recordHistory(
                    action="rotateImages",
                    user=user,
                    record_id=record_id,
                    query={"image_files": new_image_filenames},
                    calling_function="updateRecordImageFiles",
                )
                _log.info(f"Updated image files for record {record_id}")
                return True
            else:
                _log.warning(f"No records updated for {record_id}")
                return False
        except Exception as e:
            _log.error(f"Error updating record image files: {e}")
            return False


data_manager = DataManager()
