from types import SimpleNamespace
from unittest.mock import patch

from ogrre.internal import storage_api, util


class FakeBucket:
    def __init__(self, sizes):
        self.sizes = sizes
        self.calls = []

    def get_blob(self, key, timeout=None):
        self.calls.append((key, timeout))
        size = self.sizes.get(key)
        if size is None:
            return None
        return SimpleNamespace(size=size)


def test_get_file_sizes_reuses_bucket_and_deduplicates_keys():
    bucket = FakeBucket({"a": 10, "b": 20})

    with patch.object(storage_api, "_is_local", return_value=False), patch.object(
        storage_api, "_get_bucket", return_value=(None, bucket)
    ):
        sizes, missing_count = storage_api.get_file_sizes(
            ["a", "b", "a", "missing"], bucket_name="bucket", max_workers=1
        )

    assert sizes == {"a": 10, "b": 20}
    assert missing_count == 1
    assert bucket.calls == [("a", 10), ("b", 10), ("missing", 10)]


def test_compute_total_size_logs_missing_blob_summary(caplog):
    with patch.object(
        storage_api, "get_file_sizes", return_value=({"a": 10, "b": 20}, 2)
    ):
        total_size = util.compute_total_size([], ["a", "b", "missing-1", "missing-2"])

    assert total_size == 30
    assert "Skipped 2 missing or unreadable blob(s)" in caplog.text


def test_zip_files_stream_uses_bulk_size_lookup_before_streaming_images():
    existing_path = "uploads/group/record/image.png"
    missing_path = "uploads/group/record/missing.png"
    documents = {
        "record": {
            "files": ["image.png", "missing.png"],
            "rg_id": "group",
            "record_id": "record",
            "record_name": "record",
        }
    }

    with patch.object(
        storage_api, "get_file_sizes", return_value=({existing_path: 10}, 1)
    ) as get_sizes, patch.object(
        storage_api, "file_exists", return_value=True
    ) as file_exists, patch.object(
        storage_api, "iter_file_bytes", return_value=[b"image-bytes"]
    ) as iter_file_bytes:
        chunks = list(util.zip_files_stream([], documents, log_to_file=None))

    assert chunks
    get_sizes.assert_called_once()
    requested_paths = list(get_sizes.call_args.args[0])
    assert requested_paths == [existing_path, missing_path]
    file_exists.assert_not_called()
    iter_file_bytes.assert_called_once_with(
        existing_path, bucket_name=util.BUCKET_NAME, chunk_size=65536
    )


def test_generate_gcs_paths_returns_dict_when_documents_empty():
    assert util.generate_gcs_paths([]) == {}
    assert util.generate_gcs_paths({}) == {}
    assert util.generate_gcs_paths(None) == {}


def test_zip_files_stream_with_empty_documents_succeeds(tmp_path):
    dummy_file = tmp_path / "records.csv"
    dummy_file.write_text("id,name\n1,well_1\n")
    log_file = tmp_path / "zip_log.txt"

    chunks = list(
        util.zip_files_stream(
            [str(dummy_file)],
            documents=[],
            log_to_file=str(log_file),
            embedded_pdfs=[],
        )
    )
    assert chunks

