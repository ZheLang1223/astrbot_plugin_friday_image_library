from __future__ import annotations

import hashlib
import json
import random
import re
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .image_io import normalize_extensions, safe_filename

SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*$")
CATEGORY_INPUT_RE = re.compile(r"^[0-9A-Za-z_\-\u4e00-\u9fff]+$")
BATCH_UPDATE_FIELDS = {
    "title",
    "description",
    "tags",
    "rating",
    "visibility",
    "safety_status",
    "send_transform",
}


def _slugify(name: str) -> str:
    """Convert a category name to an English slug."""
    name = (name or "").strip()
    try:
        from pypinyin import lazy_pinyin
        parts = lazy_pinyin(name)
        raw = "_".join(parts)
    except ImportError:
        parts = []
        for char in name:
            if char.isascii() and char.isalnum():
                parts.append(char.lower())
            elif char in {"_", "-"}:
                parts.append("_")
            elif "\u4e00" <= char <= "\u9fff":
                parts.append(f"u{ord(char):x}")
            else:
                parts.append("_")
        raw = "_".join(parts)
    slug = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug or not slug[0].isascii() or not slug[0].isalpha():
        slug = "cat_" + (slug or "default")
    return slug[:40]


SAFETY_STATUSES = {"normal", "sensitive", "hidden"}
SEND_TRANSFORMS = {"none", "rotate_180"}


class ImageLibraryError(Exception):
    """Base error for image library operations."""


class InvalidCategoryName(ImageLibraryError):
    pass


class CategoryNotFound(ImageLibraryError):
    pass


class NoImagesFound(ImageLibraryError):
    pass


class UnsupportedImageType(ImageLibraryError):
    pass


@dataclass(frozen=True)
class ImageRecord:
    id: str
    category: str
    path: Path
    relative_path: str
    sha256: str
    size: int
    extension: str
    title: str
    description: str
    tags: list[str]
    rating: int | None
    visibility: str
    safety_status: str
    send_transform: str
    original_name: str
    uploader_id: str
    source_session: str
    created_at: str
    updated_at: str
    send_count: int
    last_sent_at: str | None

    @property
    def short_id(self) -> str:
        return self.id[:12]


@dataclass(frozen=True)
class SaveImageResult:
    status: str
    record: ImageRecord
    duplicate_of: Path | None = None

    @property
    def path(self) -> Path:
        return self.record.path

    @property
    def sha256(self) -> str:
        return self.record.sha256

    @property
    def size(self) -> int:
        return self.record.size


class ImageLibrary:
    def __init__(
        self,
        library_root: Path | str,
        *,
        db_path: Path | str | None = None,
        allowed_extensions: Iterable[str] | str | None = None,
        recent_window: int = 20,
        default_category: str = "default",
    ) -> None:
        self.library_root = Path(library_root)
        self.library_root.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else self.library_root.parent / "friday_images.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.allowed_extensions = normalize_extensions(allowed_extensions)
        self.recent_window = max(0, int(recent_window or 0))
        self.default_category = str(default_category or "default").strip() or "default"
        self._init_db()
        self._migrate_to_flat_library()
        self.sync_filesystem()

    def validate_category_name(self, category: str) -> str:
        slug, _ = self.normalize_category(category)
        return slug

    def normalize_category(self, category: str) -> tuple[str, str]:
        display_name = (category or "").strip()
        if not display_name:
            raise InvalidCategoryName("分类名不能为空。")
        if not CATEGORY_INPUT_RE.fullmatch(display_name):
            raise InvalidCategoryName("分类名只能包含中文、英文、数字、下划线或短横线。")
        slug = self.resolve_category(display_name)
        existing_display_name = self.get_category_display_name(slug)
        if existing_display_name != slug:
            display_name = existing_display_name
        return slug, display_name

    def resolve_category(self, user_input: str) -> str:
        """Resolve user input (slug or display_name) to a slug."""
        display_name = user_input.strip()
        slug = display_name.lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT slug FROM categories WHERE display_name = ?",
                (display_name,),
            ).fetchone()
            if row:
                return row["slug"]
            if SLUG_RE.fullmatch(slug):
                row = conn.execute(
                    "SELECT slug FROM categories WHERE slug = ?",
                    (slug,),
                ).fetchone()
                return row["slug"] if row else slug
        return _slugify(user_input)

    def create_category(self, slug: str, display_name: str) -> None:
        display_name = (display_name or slug).strip() or slug
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO categories (slug, display_name, created_at) VALUES (?, ?, ?)",
                (slug, display_name, now_iso()),
            )
            row = conn.execute(
                "SELECT display_name FROM categories WHERE slug = ?",
                (slug,),
            ).fetchone()
            if row and row["display_name"] == slug and display_name != slug:
                conn.execute(
                    "UPDATE categories SET display_name = ? WHERE slug = ?",
                    (display_name, slug),
                )

    def create_category_from_input(self, category: str) -> dict[str, object]:
        slug, display_name = self.normalize_category(category)
        self.create_category(slug, display_name)
        return {"slug": slug, "display_name": self.get_category_display_name(slug)}

    def rename_category(self, category: str, display_name: str) -> dict[str, object]:
        slug = self.validate_category_name(category)
        if not self.category_row_exists(slug):
            raise CategoryNotFound(f"分类不存在：{category}")
        display_name = (display_name or "").strip()
        if not display_name:
            raise InvalidCategoryName("分类名不能为空。")
        if not CATEGORY_INPUT_RE.fullmatch(display_name):
            raise InvalidCategoryName("分类名只能包含中文、英文、数字、下划线或短横线。")
        with self._connect() as conn:
            duplicate = conn.execute(
                "SELECT slug FROM categories WHERE display_name = ? AND slug != ? LIMIT 1",
                (display_name, slug),
            ).fetchone()
            if duplicate:
                raise ImageLibraryError(f"分类显示名已存在：{display_name}")
            conn.execute(
                "UPDATE categories SET display_name = ? WHERE slug = ?",
                (display_name, slug),
            )
        return {"slug": slug, "display_name": display_name}

    def merge_categories(
        self,
        source_category: str,
        target_category: str,
        *,
        protected_slugs: set[str] | None = None,
    ) -> dict[str, object]:
        source_slug = self.validate_category_name(source_category)
        target_slug, target_display_name = self.normalize_category(target_category)
        protected_slugs = protected_slugs or set()
        if source_slug in protected_slugs:
            raise ImageLibraryError("系统分类不能被合并删除。")
        if source_slug == target_slug:
            raise ImageLibraryError("源分类和目标分类不能相同。")
        if not self.category_row_exists(source_slug) and not self.category_exists(source_slug):
            raise CategoryNotFound(f"分类不存在：{source_category}")
        self.create_category(target_slug, target_display_name)
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE images SET category = ?, updated_at = ? WHERE category = ?",
                (target_slug, now_iso(), source_slug),
            )
            moved = int(cursor.rowcount or 0)
            conn.execute("DELETE FROM categories WHERE slug = ?", (source_slug,))
        return {
            "source": source_slug,
            "target": target_slug,
            "moved": moved,
        }

    def get_category_display_name(self, slug: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT display_name FROM categories WHERE slug = ?",
                (slug,),
            ).fetchone()
            return row["display_name"] if row else slug

    def list_categories_with_display(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.slug AS slug,
                       c.display_name AS display_name,
                       COUNT(i.id) AS image_count,
                       COALESCE(SUM(i.send_count), 0) AS send_count,
                       MAX(i.created_at) AS latest_upload
                FROM categories c
                LEFT JOIN images i ON i.category = c.slug
                GROUP BY c.slug, c.display_name
                ORDER BY c.slug
                """
            ).fetchall()
        return [
            {
                "category": row["slug"],
                "display_name": row["display_name"],
                "image_count": int(row["image_count"]),
                "send_count": int(row["send_count"] or 0),
                "latest_upload": row["latest_upload"],
            }
            for row in rows
        ]

    def list_categories(self) -> list[tuple[str, int]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.display_name AS display_name, COUNT(i.id) AS image_count
                FROM categories c
                LEFT JOIN images i ON i.category = c.slug
                GROUP BY c.slug, c.display_name
                ORDER BY c.slug
                """
            ).fetchall()
        return [
            (row["display_name"], int(row["image_count"]))
            for row in rows
        ]

    def category_stats(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.slug AS slug,
                       c.display_name AS display_name,
                       COUNT(i.id) AS image_count,
                       COALESCE(SUM(i.send_count), 0) AS send_count,
                       MAX(i.created_at) AS latest_upload
                FROM categories c
                LEFT JOIN images i ON i.category = c.slug
                GROUP BY c.slug, c.display_name
                ORDER BY c.slug
                """
            ).fetchall()
        return [
            {
                "category": row["display_name"],
                "slug": row["slug"],
                "image_count": int(row["image_count"]),
                "send_count": int(row["send_count"] or 0),
                "latest_upload": row["latest_upload"],
            }
            for row in rows
        ]

    def stats(self) -> dict[str, object]:
        with self._connect() as conn:
            image_row = conn.execute(
                """
                SELECT COUNT(*) AS image_count,
                       COALESCE(SUM(send_count), 0) AS send_count,
                       MAX(created_at) AS latest_upload
                FROM images
                """
            ).fetchone()
            category_row = conn.execute(
                "SELECT COUNT(*) AS category_count FROM categories"
            ).fetchone()
        return {
            "image_count": int(image_row["image_count"] or 0),
            "category_count": int(category_row["category_count"] or 0),
            "send_count": int(image_row["send_count"] or 0),
            "latest_upload": image_row["latest_upload"],
        }

    def list_images(
        self,
        category: str | None = None,
        *,
        query: str = "",
        visibility: str | None = None,
        safety_status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ImageRecord]:
        where: list[str] = []
        params: list[object] = []
        if category:
            category = self.validate_category_name(category)
            if not self.category_exists(category):
                raise CategoryNotFound(f"分类不存在：{category}")
            where.append("category = ?")
            params.append(category)
        if visibility:
            where.append("visibility = ?")
            params.append(visibility)
        if safety_status:
            where.append("safety_status = ?")
            params.append(safety_status)
        if query:
            like = f"%{query.strip()}%"
            where.append(
                "(title LIKE ? OR description LIKE ? OR tags_json LIKE ? OR original_name LIKE ? OR id LIKE ?)"
            )
            params.extend([like, like, like, like, like])

        sql = "SELECT * FROM images"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([max(1, min(int(limit), 200)), max(0, int(offset))])

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_image(self, image_id: str) -> ImageRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM images WHERE id = ? OR substr(id, 1, 12) = ?",
                (image_id, image_id),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def category_exists(self, category: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM categories WHERE slug = ?
                UNION
                SELECT 1 FROM images WHERE category = ?
                LIMIT 1
                """,
                (category, category),
            ).fetchone()
        return row is not None

    def category_row_exists(self, category: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM categories WHERE slug = ? LIMIT 1",
                (category,),
            ).fetchone()
        return row is not None

    def category_image_count(self, category: str) -> int:
        category = self.validate_category_name(category)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS image_count FROM images WHERE category = ?",
                (category,),
            ).fetchone()
        return int(row["image_count"] or 0)

    def select_random(
        self,
        *,
        category: str | None,
        session_id: str,
        tag: str | None = None,
    ) -> ImageRecord:
        where = ["visibility = 'public'", "safety_status != 'hidden'"]
        params: list[object] = []
        if category:
            category = self.validate_category_name(category)
            if not self.category_exists(category):
                raise CategoryNotFound(f"分类不存在：{category}")
            where.append("category = ?")
            params.append(category)
        if tag:
            tag = str(tag).strip()
            if tag:
                where.append("tags_json LIKE ?")
                params.append(f"%{tag}%")

        recent_ids = self._recent_image_ids(session_id)
        recent_set = set(recent_ids)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM images WHERE " + " AND ".join(where),
                params,
            ).fetchall()

        candidates = [self._row_to_record(row) for row in rows]
        if tag:
            candidates = [record for record in candidates if tag in record.tags]
        if not candidates:
            if category:
                raise NoImagesFound(f"分类里还没有可发送图片：{category}")
            if tag:
                raise NoImagesFound(f"标签下还没有可发送图片：#{tag}")
            raise NoImagesFound("图库里还没有可发送图片。")

        available = [record for record in candidates if record.id not in recent_set]
        return random.choice(available or candidates)

    def add_image(
        self,
        *,
        category: str,
        source_path: Path | str,
        original_name: str | None = None,
        detected_extension: str | None = None,
        uploader_id: str = "",
        source_session: str = "",
    ) -> SaveImageResult:
        category, display_name = self.normalize_category(category)
        source_path = Path(source_path)
        if not source_path.exists() or not source_path.is_file():
            raise ImageLibraryError("待保存图片不存在。")

        extension = (detected_extension or source_path.suffix.lower().lstrip(".")).lower()
        if extension == "jpeg":
            extension = "jpg"
        if extension not in self.allowed_extensions:
            allowed_text = ", ".join(sorted(self.allowed_extensions))
            raise UnsupportedImageType(f"不支持的图片格式：{extension}。允许格式：{allowed_text}。")

        sha256 = sha256_file(source_path)
        duplicate = self.find_duplicate(sha256)
        if duplicate:
            return SaveImageResult("duplicate", duplicate, duplicate.path)

        self.create_category(category, display_name)
        self.library_root.mkdir(parents=True, exist_ok=True)
        target = self._unique_target(self.library_root, original_name, sha256, extension)
        shutil.copy2(source_path, target)
        relative_path = target.name
        now = now_iso()
        title = safe_filename(original_name, fallback=target.stem)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO images (
                    id, sha256, category, relative_path, title, description, tags_json,
                    rating, visibility, safety_status, send_transform, size_bytes, extension,
                    original_name, uploader_id, source_session, created_at, updated_at,
                    send_count, last_sent_at
                )
                VALUES (?, ?, ?, ?, ?, '', '[]', NULL, 'public', 'normal', 'none',
                        ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                """,
                (
                    sha256,
                    sha256,
                    category,
                    relative_path,
                    title,
                    target.stat().st_size,
                    extension,
                    original_name or target.name,
                    uploader_id,
                    source_session,
                    now,
                    now,
                ),
            )
        record = self.get_image(sha256)
        if record is None:
            raise ImageLibraryError("图片写入数据库后无法读取记录。")
        return SaveImageResult("saved", record)

    def find_duplicate(self, sha256: str) -> ImageRecord | None:
        return self.get_image(sha256)

    def update_image_info(
        self,
        image_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        rating: int | None = None,
        clear_rating: bool = False,
        visibility: str | None = None,
        safety_status: str | None = None,
        send_transform: str | None = None,
    ) -> ImageRecord:
        record = self.get_image(image_id)
        if record is None:
            raise ImageLibraryError("图片不存在。")
        updates: list[str] = []
        params: list[object] = []
        if title is not None:
            updates.append("title = ?")
            params.append(str(title).strip()[:120])
        if description is not None:
            updates.append("description = ?")
            params.append(str(description).strip()[:1000])
        if tags is not None:
            normalized_tags = normalize_tags(tags)
            updates.append("tags_json = ?")
            params.append(json.dumps(normalized_tags, ensure_ascii=False))
        if clear_rating:
            updates.append("rating = NULL")
        elif rating is not None:
            rating_value = int(rating)
            if rating_value < 0 or rating_value > 5:
                raise ImageLibraryError("评分必须在 0 到 5 之间。")
            updates.append("rating = ?")
            params.append(rating_value)
        if visibility is not None:
            if visibility not in {"public", "hidden"}:
                raise ImageLibraryError("可见性只支持 public 或 hidden。")
            updates.append("visibility = ?")
            params.append(visibility)
            if visibility == "hidden":
                updates.append("safety_status = ?")
                params.append("hidden")
            elif record.safety_status == "hidden":
                updates.append("safety_status = ?")
                params.append("normal")
        if safety_status is not None:
            if safety_status not in SAFETY_STATUSES:
                raise ImageLibraryError("敏感状态只支持 normal、sensitive 或 hidden。")
            updates.append("safety_status = ?")
            params.append(safety_status)
            updates.append("visibility = ?")
            params.append("hidden" if safety_status == "hidden" else "public")
            if safety_status == "sensitive" and send_transform is None and record.send_transform == "none":
                updates.append("send_transform = ?")
                params.append("rotate_180")
        if send_transform is not None:
            if send_transform not in SEND_TRANSFORMS:
                raise ImageLibraryError("发送变换只支持 none 或 rotate_180。")
            updates.append("send_transform = ?")
            params.append(send_transform)
        if not updates:
            return record
        updates.append("updated_at = ?")
        params.append(now_iso())
        params.append(record.id)
        with self._connect() as conn:
            conn.execute(f"UPDATE images SET {', '.join(updates)} WHERE id = ?", params)
        updated = self.get_image(record.id)
        if updated is None:
            raise ImageLibraryError("图片更新后无法读取记录。")
        return updated

    def batch_update_image_info(
        self,
        image_ids: Iterable[str],
        updates: dict[str, object],
    ) -> dict[str, object]:
        safe_updates = {key: value for key, value in updates.items() if key in BATCH_UPDATE_FIELDS}
        if not safe_updates:
            raise ImageLibraryError("没有可批量更新的字段。")

        updated = 0
        failed: list[dict[str, str]] = []
        for raw_id in image_ids:
            image_id = str(raw_id).strip()
            if not image_id:
                continue
            payload = dict(safe_updates)
            if "rating" in payload and (payload["rating"] is None or payload["rating"] == ""):
                payload.pop("rating")
                payload["clear_rating"] = True
            try:
                self.update_image_info(image_id, **payload)
                updated += 1
            except (ImageLibraryError, TypeError, ValueError) as exc:
                failed.append({"id": image_id, "error": str(exc)})
        return {"updated": updated, "failed": failed}

    def batch_move_category(
        self,
        image_ids: Iterable[str],
        category: str,
    ) -> dict[str, object]:
        target_slug, display_name = self.normalize_category(category)
        self.create_category(target_slug, display_name)
        moved = 0
        failed: list[dict[str, str]] = []
        for raw_id in image_ids:
            image_id = str(raw_id).strip()
            if not image_id:
                continue
            record = self.get_image(image_id)
            if record is None:
                failed.append({"id": image_id, "error": "图片不存在。"})
                continue
            with self._connect() as conn:
                conn.execute(
                    "UPDATE images SET category = ?, updated_at = ? WHERE id = ?",
                    (target_slug, now_iso(), record.id),
                )
            moved += 1
        return {"moved": moved, "updated": moved, "failed": failed, "category": target_slug}

    def batch_update_tags(
        self,
        image_ids: Iterable[str],
        tags: Iterable[str] | str,
        *,
        operation: str,
    ) -> dict[str, object]:
        incoming = normalize_tags(tags)
        if not incoming:
            raise ImageLibraryError("缺少标签。")
        if operation not in {"add", "remove", "set"}:
            raise ImageLibraryError("标签操作只支持 add、remove 或 set。")
        updated = 0
        failed: list[dict[str, str]] = []
        for raw_id in image_ids:
            image_id = str(raw_id).strip()
            if not image_id:
                continue
            record = self.get_image(image_id)
            if record is None:
                failed.append({"id": image_id, "error": "图片不存在。"})
                continue
            if operation == "set":
                next_tags = incoming
            elif operation == "add":
                next_tags = normalize_tags([*record.tags, *incoming])
            else:
                remove_set = set(incoming)
                next_tags = [tag for tag in record.tags if tag not in remove_set]
            self.update_image_info(record.id, tags=next_tags)
            updated += 1
        return {"updated": updated, "failed": failed}

    def delete_image(self, image_id: str) -> ImageRecord:
        record = self.get_image(image_id)
        if record is None:
            raise ImageLibraryError("图片不存在。")
        path = record.path.resolve()
        try:
            path.relative_to(self.library_root.resolve())
        except ValueError as exc:
            raise ImageLibraryError("图片文件路径不在图库目录内。") from exc
        with self._connect() as conn:
            conn.execute("DELETE FROM send_history WHERE image_id = ?", (record.id,))
            conn.execute("DELETE FROM images WHERE id = ?", (record.id,))
        if path.exists() and path.is_file():
            path.unlink()
        return record

    def record_send(self, image_id: str, session_id: str) -> ImageRecord:
        timestamp = now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE images
                SET send_count = send_count + 1,
                    last_sent_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (timestamp, timestamp, image_id),
            )
            if cursor.rowcount == 0:
                raise ImageLibraryError("图片不存在。")
            conn.execute(
                "INSERT INTO send_history (image_id, session_id, sent_at) VALUES (?, ?, ?)",
                (image_id, session_id or "global", timestamp),
            )
        record = self.get_image(image_id)
        if record is None:
            raise ImageLibraryError("图片发送记录写入后无法读取记录。")
        return record

    def sync_filesystem(self) -> None:
        with self._connect() as conn:
            existing_ids = {row["id"] for row in conn.execute("SELECT id FROM images").fetchall()}
        for path in self._scan_image_paths():
            sha256 = sha256_file(path)
            if sha256 in existing_ids:
                continue
            relative_path = path.name
            category, display_name = self.normalize_category(self._config_get_default_category())
            self.create_category(category, display_name)
            extension = path.suffix.lower().lstrip(".")
            if extension == "jpeg":
                extension = "jpg"
            now = now_iso()
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO images (
                        id, sha256, category, relative_path, title, description, tags_json,
                        rating, visibility, safety_status, send_transform, size_bytes, extension,
                        original_name, uploader_id, source_session, created_at, updated_at,
                        send_count, last_sent_at
                    )
                    VALUES (?, ?, ?, ?, ?, '', '[]', NULL, 'public', 'normal', 'none',
                            ?, ?, ?, '', '', ?, ?, 0, NULL)
                    """,
                    (
                        sha256,
                        sha256,
                        category,
                        relative_path,
                        safe_filename(path.name, fallback=path.stem),
                        path.stat().st_size,
                        extension,
                        path.name,
                        now,
                        now,
                    ),
                )

    def _config_get_default_category(self) -> str:
        return self.default_category

    def to_dict(self, record: ImageRecord) -> dict[str, object]:
        display_name = self.get_category_display_name(record.category)
        return {
            "id": record.id,
            "short_id": record.short_id,
            "category": display_name,
            "category_slug": record.category,
            "category_display_name": display_name,
            "relative_path": record.relative_path,
            "title": record.title,
            "description": record.description,
            "tags": record.tags,
            "rating": record.rating,
            "visibility": record.visibility,
            "safety_status": record.safety_status,
            "send_transform": record.send_transform,
            "size_bytes": record.size,
            "extension": record.extension,
            "original_name": record.original_name,
            "uploader_id": record.uploader_id,
            "source_session": record.source_session,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "send_count": record.send_count,
            "last_sent_at": record.last_sent_at,
        }

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS images (
                    id TEXT PRIMARY KEY,
                    sha256 TEXT UNIQUE NOT NULL,
                    category TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    rating INTEGER,
                    visibility TEXT NOT NULL DEFAULT 'public',
                    safety_status TEXT NOT NULL DEFAULT 'normal',
                    send_transform TEXT NOT NULL DEFAULT 'none',
                    size_bytes INTEGER NOT NULL,
                    extension TEXT NOT NULL,
                    original_name TEXT NOT NULL DEFAULT '',
                    uploader_id TEXT NOT NULL DEFAULT '',
                    source_session TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    send_count INTEGER NOT NULL DEFAULT 0,
                    last_sent_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS send_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    sent_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS categories (
                    slug TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_images_category ON images(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_images_visibility ON images(visibility)")
            self._ensure_column(conn, "images", "safety_status", "TEXT NOT NULL DEFAULT 'normal'")
            self._ensure_column(conn, "images", "send_transform", "TEXT NOT NULL DEFAULT 'none'")
            conn.execute(
                """
                UPDATE images
                SET safety_status = 'hidden'
                WHERE visibility = 'hidden' AND safety_status != 'hidden'
                """
            )
            conn.execute(
                """
                UPDATE images
                SET send_transform = 'rotate_180'
                WHERE safety_status = 'sensitive' AND send_transform = 'none'
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_images_safety ON images(safety_status)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_send_history_session ON send_history(session_id, sent_at)"
            )

    def _get_schema_version(self, key: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM schema_version WHERE key = ?", (key,)
            ).fetchone()
            return int(row["value"]) if row else 0

    def _set_schema_version(self, key: str, value: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (key, value) VALUES (?, ?)",
                (key, value),
            )

    def _migrate_to_flat_library(self) -> None:
        if self._get_schema_version("flat_migration") >= 1:
            return
        subdirs = [d for d in self.library_root.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if not subdirs:
            self._ensure_category_rows()
            self._set_schema_version("flat_migration", 1)
            return
        slug_map: dict[str, str] = {}
        with self._connect() as conn:
            used_slugs = {
                row["slug"] for row in conn.execute("SELECT slug FROM categories").fetchall()
            }
            for subdir in subdirs:
                old_name = subdir.name
                row = conn.execute(
                    "SELECT slug FROM categories WHERE display_name = ?",
                    (old_name,),
                ).fetchone()
                if row:
                    slug = row["slug"]
                else:
                    slug = _slugify(old_name)
                    base_slug = slug
                    counter = 1
                    while slug in used_slugs or slug in slug_map.values():
                        slug = f"{base_slug}_{counter}"
                        counter += 1
                slug_map[old_name] = slug
                used_slugs.add(slug)

            for old_name, slug in slug_map.items():
                conn.execute(
                    "INSERT OR IGNORE INTO categories (slug, display_name, created_at) VALUES (?, ?, ?)",
                    (slug, old_name, now_iso()),
                )
            for old_name, slug in slug_map.items():
                old_dir = self.library_root / old_name
                for file_path in old_dir.iterdir():
                    if (
                        not file_path.is_file()
                        or file_path.suffix.lower().lstrip(".") not in self.allowed_extensions
                    ):
                        continue
                    sha256 = sha256_file(file_path)
                    extension = file_path.suffix.lower().lstrip(".")
                    if extension == "jpeg":
                        extension = "jpg"
                    new_path = self.library_root / file_path.name
                    if new_path.exists():
                        new_path = self._unique_target(
                            self.library_root,
                            file_path.name,
                            sha256,
                            extension,
                        )
                    file_path.rename(new_path)
                    old_relative = f"{old_name}/{file_path.name}"
                    new_relative = new_path.name
                    cursor = conn.execute(
                        "UPDATE images SET relative_path = ?, category = ? WHERE relative_path = ?",
                        (new_relative, slug, old_relative),
                    )
                    if cursor.rowcount == 0:
                        duplicate = conn.execute(
                            "SELECT 1 FROM images WHERE sha256 = ? LIMIT 1",
                            (sha256,),
                        ).fetchone()
                        if duplicate is None:
                            self._insert_filesystem_record(
                                conn,
                                path=new_path,
                                category=slug,
                                sha256=sha256,
                                extension=extension,
                                source_session="migration",
                            )
                conn.execute(
                    "UPDATE images SET category = ? WHERE category = ?",
                    (slug, old_name),
                )
            for old_name in slug_map:
                old_dir = self.library_root / old_name
                try:
                    if old_dir.exists() and not any(old_dir.iterdir()):
                        old_dir.rmdir()
                except OSError:
                    pass
        self._ensure_category_rows()
        self._set_schema_version("flat_migration", 1)

    def _ensure_category_rows(self) -> None:
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT category FROM images").fetchall()
            used_slugs = {
                row["slug"] for row in conn.execute("SELECT slug FROM categories").fetchall()
            }
            for row in rows:
                category = str(row["category"] or "").strip()
                if not category:
                    continue
                if category in used_slugs:
                    continue
                if SLUG_RE.fullmatch(category):
                    conn.execute(
                        "INSERT OR IGNORE INTO categories (slug, display_name, created_at) VALUES (?, ?, ?)",
                        (category, category, now_iso()),
                    )
                    used_slugs.add(category)
                    continue
                slug = _slugify(category)
                base_slug = slug
                counter = 1
                while slug in used_slugs:
                    slug = f"{base_slug}_{counter}"
                    counter += 1
                conn.execute(
                    "INSERT OR IGNORE INTO categories (slug, display_name, created_at) VALUES (?, ?, ?)",
                    (slug, category, now_iso()),
                )
                conn.execute("UPDATE images SET category = ? WHERE category = ?", (slug, category))
                used_slugs.add(slug)

    def _insert_filesystem_record(
        self,
        conn: sqlite3.Connection,
        *,
        path: Path,
        category: str,
        sha256: str,
        extension: str,
        source_session: str,
    ) -> None:
        now = now_iso()
        conn.execute(
            """
            INSERT OR IGNORE INTO images (
                id, sha256, category, relative_path, title, description, tags_json,
                rating, visibility, safety_status, send_transform, size_bytes, extension,
                original_name, uploader_id, source_session, created_at, updated_at,
                send_count, last_sent_at
            )
            VALUES (?, ?, ?, ?, ?, '', '[]', NULL, 'public', 'normal', 'none',
                    ?, ?, ?, '', ?, ?, ?, 0, NULL)
            """,
            (
                sha256,
                sha256,
                category,
                path.name,
                safe_filename(path.name, fallback=path.stem),
                path.stat().st_size,
                extension,
                path.name,
                source_session,
                now,
                now,
            ),
        )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _recent_image_ids(self, session_id: str) -> list[str]:
        if self.recent_window <= 0:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT image_id
                FROM send_history
                WHERE session_id = ?
                ORDER BY sent_at DESC, id DESC
                LIMIT ?
                """,
                (session_id or "global", self.recent_window),
            ).fetchall()
        return [row["image_id"] for row in rows]

    def _scan_image_paths(self) -> list[Path]:
        paths: list[Path] = []
        for path in sorted(self.library_root.iterdir(), key=lambda item: item.name):
            if path.is_file() and path.suffix.lower().lstrip(".") in self.allowed_extensions:
                paths.append(path)
        return paths

    def _row_to_record(self, row: sqlite3.Row) -> ImageRecord:
        tags = json.loads(row["tags_json"] or "[]")
        if not isinstance(tags, list):
            tags = []
        path = self.library_root / row["relative_path"]
        return ImageRecord(
            id=row["id"],
            category=row["category"],
            path=path,
            relative_path=row["relative_path"],
            sha256=row["sha256"],
            size=int(row["size_bytes"]),
            extension=row["extension"],
            title=row["title"],
            description=row["description"],
            tags=[str(tag) for tag in tags],
            rating=row["rating"],
            visibility=row["visibility"],
            safety_status=row["safety_status"],
            send_transform=row["send_transform"],
            original_name=row["original_name"],
            uploader_id=row["uploader_id"],
            source_session=row["source_session"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            send_count=int(row["send_count"]),
            last_sent_at=row["last_sent_at"],
        )

    def _unique_target(
        self,
        category_dir: Path,
        original_name: str | None,
        sha256: str,
        extension: str,
    ) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        stem = safe_filename(original_name, fallback="image")
        base_name = f"{timestamp}-{sha256[:12]}-{stem}"
        target = category_dir / f"{base_name}.{extension}"
        counter = 1
        while target.exists():
            target = category_dir / f"{base_name}-{counter}.{extension}"
            counter += 1
        return target

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def normalize_tags(tags: Iterable[str] | str | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        raw = re.split(r"[\s,，;；#]+", tags)
    else:
        raw = [str(tag) for tag in tags]
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = item.strip()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        result.append(tag[:32])
    return result[:20]


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
