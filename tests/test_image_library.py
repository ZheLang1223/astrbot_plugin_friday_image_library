from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.image_library import ImageLibrary, ImageLibraryError, _slugify


def write_image(path: Path, marker: bytes = b"") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0friday-test" + marker)


class ImageLibraryTest(unittest.TestCase):
    def make_library(self, root: Path) -> ImageLibrary:
        return ImageLibrary(root / "library", db_path=root / "friday_images.sqlite3")

    def test_slugify_chinese_without_pypinyin_does_not_collapse_to_default(self) -> None:
        slug = _slugify("猫猫")
        self.assertNotEqual(slug, "cat_default")
        self.assertRegex(slug, r"^[a-z][a-z0-9_]*$")

    def test_add_image_stores_slug_and_keeps_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            write_image(source)
            library = self.make_library(root)

            result = library.add_image(
                category="猫猫",
                source_path=source,
                original_name="cat.jpg",
                detected_extension="jpg",
            )

            self.assertNotEqual(result.record.category, "猫猫")
            data = library.to_dict(result.record)
            self.assertEqual(data["category"], "猫猫")
            self.assertEqual(data["category_display_name"], "猫猫")
            self.assertEqual(data["category_slug"], result.record.category)
            self.assertEqual(library.list_images(category="猫猫")[0].id, result.record.id)

    def test_flat_migration_imports_old_subdir_files_and_uniquifies_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_root = root / "library"
            write_image(library_root / "猫猫" / "same.jpg", b"one")
            write_image(library_root / "狗狗" / "same.jpg", b"two")

            library = self.make_library(root)
            records = library.list_images(limit=10)
            relative_paths = {record.relative_path for record in records}
            categories = {
                library.to_dict(record)["category_display_name"]
                for record in records
            }

            self.assertEqual(len(records), 2)
            self.assertEqual(len(relative_paths), 2)
            self.assertEqual(categories, {"猫猫", "狗狗"})
            self.assertFalse((library_root / "猫猫" / "same.jpg").exists())
            self.assertTrue(all((library_root / path).is_file() for path in relative_paths))

    def test_delete_image_removes_record_history_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            write_image(source)
            library = self.make_library(root)
            result = library.add_image(
                category="默认",
                source_path=source,
                original_name="source.jpg",
                detected_extension="jpg",
            )
            library.record_send(result.record.id, "session")
            image_path = result.record.path

            deleted = library.delete_image(result.record.id)

            self.assertEqual(deleted.id, result.record.id)
            self.assertFalse(image_path.exists())
            self.assertIsNone(library.get_image(result.record.id))
            with sqlite3.connect(root / "friday_images.sqlite3") as conn:
                count = conn.execute("SELECT COUNT(*) FROM send_history").fetchone()[0]
            self.assertEqual(count, 0)

    def test_batch_update_allows_only_safe_fields_and_reports_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            write_image(source)
            library = self.make_library(root)
            result = library.add_image(
                category="默认",
                source_path=source,
                original_name="source.jpg",
                detected_extension="jpg",
            )

            batch_result = library.batch_update_image_info(
                [result.record.id, "missing"],
                {
                    "safety_status": "sensitive",
                    "send_transform": "rotate_180",
                    "relative_path": "blocked.jpg",
                },
            )
            updated = library.get_image(result.record.id)

            self.assertEqual(batch_result["updated"], 1)
            self.assertEqual(len(batch_result["failed"]), 1)
            self.assertIsNotNone(updated)
            self.assertEqual(updated.safety_status, "sensitive")
            self.assertEqual(updated.send_transform, "rotate_180")
            with self.assertRaises(ImageLibraryError):
                library.batch_update_image_info([result.record.id], {"relative_path": "blocked.jpg"})


if __name__ == "__main__":
    unittest.main()
