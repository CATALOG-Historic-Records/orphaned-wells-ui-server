import io
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

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


@pytest.mark.parametrize("extension", ["json", "csv"])
@pytest.mark.parametrize("include_pdf", [False, True])
def test_zip_files_stream_without_images(tmp_path, extension, include_pdf):
    export_file = tmp_path / f"records.{extension}"
    contents = b'[{"file": "well.pdf"}]' if extension == "json" else b"file\nwell.pdf\n"
    export_file.write_bytes(contents)
    embedded_pdfs = [("documents/well.pdf", b"pdf-bytes")] if include_pdf else []

    with patch.object(storage_api, "_get_bucket") as get_bucket:
        archive_bytes = b"".join(
            util.zip_files_stream(
                [str(export_file)],
                [],
                log_to_file=str(tmp_path / "zip_log.txt"),
                embedded_pdfs=embedded_pdfs,
            )
        )

    get_bucket.assert_not_called()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert archive.read(export_file.name) == contents
        assert archive.namelist() == [export_file.name] + [
            name for name, _ in embedded_pdfs
        ]
        for name, content in embedded_pdfs:
            assert archive.read(name) == content


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
