from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from services.image_library import ImageLibrary, ImageLibraryError, NoImagesFound, _slugify
from services.image_transform import transformed_send_path


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

    def test_flat_migration_cleans_duplicate_old_files_without_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_root = root / "library"
            write_image(library_root / "猫猫" / "same.jpg", b"same")
            write_image(library_root / "狗狗" / "same.jpg", b"same")

            library = self.make_library(root)
            records = library.list_images(limit=10)
            health = library.health_check()

            self.assertEqual(len(records), 1)
            self.assertFalse((library_root / "猫猫" / "same.jpg").exists())
            self.assertFalse((library_root / "狗狗" / "same.jpg").exists())
            self.assertTrue(health["ok"])
            self.assertEqual(health["orphan_files"], [])

    def test_flat_migration_reuses_existing_flat_file_with_same_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_root = root / "library"
            write_image(library_root / "same.jpg", b"same")
            write_image(library_root / "猫猫" / "same.jpg", b"same")

            library = self.make_library(root)
            records = library.list_images(limit=10)
            health = library.health_check()

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].relative_path, "same.jpg")
            self.assertTrue((library_root / "same.jpg").exists())
            self.assertFalse((library_root / "猫猫" / "same.jpg").exists())
            self.assertTrue(health["ok"])

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
            sent_record = library.record_send(result.record.id, "session")
            image_path = result.record.path

            self.assertEqual(sent_record.send_count, 2)
            deleted = library.delete_image(result.record.id)

            self.assertEqual(deleted.id, result.record.id)
            self.assertFalse(image_path.exists())
            self.assertIsNone(library.get_image(result.record.id))
            conn = sqlite3.connect(root / "friday_images.sqlite3")
            try:
                count = conn.execute("SELECT COUNT(*) FROM send_history").fetchone()[0]
            finally:
                conn.close()
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

    def test_update_rejects_conflicting_visibility_and_safety_status(self) -> None:
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

            with self.assertRaises(ImageLibraryError):
                library.update_image_info(
                    result.record.id,
                    visibility="hidden",
                    safety_status="normal",
                )

    def test_send_transform_rotates_even_when_safety_status_is_normal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from PIL import Image

            root = Path(tmp)
            source = root / "source.png"
            image = Image.new("RGB", (3, 5), "white")
            image.putpixel((0, 0), (255, 0, 0))
            image.putpixel((2, 4), (0, 0, 255))
            image.save(source)
            library = self.make_library(root)
            result = library.add_image(
                category="默认",
                source_path=source,
                original_name="source.png",
                detected_extension="png",
            )

            record = library.update_image_info(result.record.id, send_transform="rotate_180")
            send_path = transformed_send_path(record, root / "transformed")

            self.assertNotEqual(send_path, record.path)
            self.assertTrue(send_path.exists())
            rotated = Image.open(send_path)
            self.assertEqual(rotated.getpixel((2, 4)), (255, 0, 0))

    def test_batch_move_tags_rename_and_merge_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_one = root / "one.jpg"
            source_two = root / "two.jpg"
            write_image(source_one, b"one")
            write_image(source_two, b"two")
            library = self.make_library(root)
            first = library.add_image(
                category="待整理",
                source_path=source_one,
                original_name="one.jpg",
                detected_extension="jpg",
            )
            second = library.add_image(
                category="猫猫",
                source_path=source_two,
                original_name="two.jpg",
                detected_extension="jpg",
            )

            moved = library.batch_move_category([first.record.id], "猫猫")
            tagged = library.batch_update_tags(
                [first.record.id, second.record.id],
                ["可爱", "精选"],
                operation="add",
            )
            renamed = library.rename_category("猫猫", "猫图")
            merged = library.merge_categories("待整理", "猫图")

            first_record = library.get_image(first.record.id)
            second_record = library.get_image(second.record.id)
            self.assertEqual(moved["updated"], 1)
            self.assertEqual(tagged["updated"], 2)
            self.assertEqual(renamed["display_name"], "猫图")
            self.assertEqual(merged["target"], second_record.category)
            self.assertIsNotNone(first_record)
            self.assertIn("精选", first_record.tags)
            self.assertEqual(library.to_dict(second_record)["category_display_name"], "猫图")

    def test_select_random_supports_exact_tag_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_one = root / "one.jpg"
            source_two = root / "two.jpg"
            write_image(source_one, b"one")
            write_image(source_two, b"two")
            library = self.make_library(root)
            first = library.add_image(
                category="默认",
                source_path=source_one,
                original_name="one.jpg",
                detected_extension="jpg",
            )
            second = library.add_image(
                category="默认",
                source_path=source_two,
                original_name="two.jpg",
                detected_extension="jpg",
            )
            library.update_image_info(first.record.id, tags=["cat"])
            library.update_image_info(second.record.id, tags=["catalog"])

            selected = library.select_random(category=None, tag="cat", session_id="session")

            self.assertEqual(selected.id, first.record.id)

    def test_empty_category_is_visible_and_resolves_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = self.make_library(root)

            created = library.create_category_from_input("待整理")
            stats = library.category_stats()
            listed = library.list_categories()
            images = library.list_images(category="待整理")

            self.assertEqual(created["display_name"], "待整理")
            self.assertIn(
                {"category": "待整理", "slug": created["slug"], "image_count": 0, "send_count": 0, "latest_upload": None},
                stats,
            )
            self.assertIn(("待整理", 0), listed)
            self.assertEqual(images, [])
            with self.assertRaises(NoImagesFound):
                library.select_random(category="待整理", session_id="session")

    def test_category_display_name_must_be_unique_on_rename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = self.make_library(root)
            library.create_category_from_input("猫猫")
            library.create_category_from_input("狗狗")

            with self.assertRaises(ImageLibraryError):
                library.rename_category("狗狗", "猫猫")

    def test_record_send_rejects_missing_image_without_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library = self.make_library(root)

            with self.assertRaises(ImageLibraryError):
                library.record_send("missing", "session")

            conn = sqlite3.connect(root / "friday_images.sqlite3")
            try:
                count = conn.execute("SELECT COUNT(*) FROM send_history").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(count, 0)

    def test_health_check_reports_missing_and_orphan_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.jpg"
            orphan = root / "library" / "orphan.jpg"
            write_image(source)
            library = self.make_library(root)
            result = library.add_image(
                category="默认",
                source_path=source,
                original_name="source.jpg",
                detected_extension="jpg",
            )
            result.record.path.unlink()
            write_image(orphan, b"orphan")

            health = library.health_check()

            self.assertFalse(health["ok"])
            self.assertEqual(health["issue_count"], 2)
            self.assertEqual(len(health["missing_files"]), 1)
            self.assertEqual(health["orphan_files"], ["orphan.jpg"])


if __name__ == "__main__":
    unittest.main()
