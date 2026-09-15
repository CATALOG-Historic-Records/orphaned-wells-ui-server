"""Upload tests use an isolated database; never load local runtime secrets."""

import importlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mongomock
import pytest


@pytest.fixture
def manager(tmp_path):
    db = mongomock.MongoClient().upload_tests
    settings = SimpleNamespace(img_dir=tmp_path, log_dir=tmp_path, export_dir=tmp_path)
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
        result.using_default_processor = False
        yield result
