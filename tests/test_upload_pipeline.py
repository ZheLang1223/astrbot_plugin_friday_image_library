from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.image_library import ImageLibrary
from services.settings import UploadSettings
from services.upload_pipeline import UploadPipeline, UploadRequest


def write_jpeg(path: Path, marker: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0friday-test" + marker)


class UploadPipelineTest(unittest.TestCase):
    def make_pipeline(self, root: Path) -> UploadPipeline:
        library = ImageLibrary(root / "library", db_path=root / "friday_images.sqlite3")
        return UploadPipeline(library, UploadSettings())

    def test_upload_without_category_goes_to_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            write_jpeg(source)
            pipeline = self.make_pipeline(root)

            summary = pipeline.upload_file(
                source,
                "source.jpg",
                UploadRequest(category=None, uploader_id="user", source_session="session"),
            )

            self.assertEqual(summary.saved_count, 1)
            self.assertEqual(summary.duplicate_count, 0)
            self.assertEqual(summary.records[0].category, "inbox")
            data = pipeline.library.to_dict(summary.records[0])
            self.assertEqual(data["category_display_name"], "待整理")

    def test_duplicate_upload_does_not_create_second_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            write_jpeg(source, b"same")
            pipeline = self.make_pipeline(root)
            request = UploadRequest(category="猫猫", uploader_id="user", source_session="web")

            first = pipeline.upload_file(source, "cat.jpg", request)
            second = pipeline.upload_file(source, "cat.jpg", request)

            self.assertEqual(first.saved_count, 1)
            self.assertEqual(second.saved_count, 0)
            self.assertEqual(second.duplicate_count, 1)
            self.assertEqual(len(pipeline.library.list_images(limit=10)), 1)

    def test_invalid_file_reports_failure_without_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.txt"
            source.write_text("not an image", encoding="utf-8")
            pipeline = self.make_pipeline(root)

            summary = pipeline.upload_file(
                source,
                "source.txt",
                UploadRequest(category=None, uploader_id="user", source_session="web"),
            )

            self.assertEqual(summary.saved_count, 0)
            self.assertEqual(summary.duplicate_count, 0)
            self.assertEqual(len(summary.failed), 1)
            self.assertEqual(summary.records, [])


if __name__ == "__main__":
    unittest.main()
