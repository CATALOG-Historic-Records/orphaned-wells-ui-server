"""Upload tests use an isolated database; never load local runtime secrets."""

import importlib
import inspect
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mongomock
import mongomock.collection
import pytest


def _ensure_mongomock_pymongo_bulk_sort_compat():
    """Allow PyMongo >= 4.11 UpdateOne/ReplaceOne bulk operations with mongomock 4.3.0."""
    builder = mongomock.collection.BulkOperationBuilder
    if "sort" not in inspect.signature(builder.add_update).parameters:
        orig_add_update = builder.add_update

        def add_update(
            self,
            selector,
            doc,
            multi=False,
            upsert=False,
            collation=None,
            array_filters=None,
            hint=None,
            sort=None,
        ):
            return orig_add_update(
                self,
                selector,
                doc,
                multi=multi,
                upsert=upsert,
                collation=collation,
                array_filters=array_filters,
                hint=hint,
            )

        builder.add_update = add_update

    if "sort" not in inspect.signature(builder.add_replace).parameters:
        orig_add_replace = builder.add_replace

        def add_replace(
            self, selector, doc, upsert, collation=None, hint=None, sort=None
        ):
            return orig_add_replace(
                self, selector, doc, upsert, collation=collation, hint=hint
            )

        builder.add_replace = add_replace


_ensure_mongomock_pymongo_bulk_sort_compat()


@pytest.fixture
def manager(tmp_path):
    db = mongomock.MongoClient().upload_tests
    settings = SimpleNamespace(img_dir=tmp_path, log_dir=tmp_path)
    with patch(
        "ogrre.internal.mongodb_connection.connectToDatabase", return_value=db
    ), patch("ogrre.internal.settings.AppSettings", return_value=settings), patch(
        "ogrre_data_cleaning.processor_schemas.processor_api.get_processor_list",
        return_value=[],
    ):
        module = importlib.import_module("ogrre.internal.data_manager")
        result = module.DataManager.__new__(module.DataManager)
        result.db = db
        result.app_settings = settings
        result.recordHistory = Mock()
        # Job lifecycle tests isolate extraction configuration. Schema tests
        # remove this stub to exercise the real resolver.
        result.getRecordGroupProcessingConfig = Mock(
            return_value={
                "processor_id": "test-processor",
                "model_id": "test-model",
                "processor_attributes": [],
                "using_default_processor": False,
            }
        )
        result.collaborator = "isgs"
        yield result
