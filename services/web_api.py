from __future__ import annotations

import inspect
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .image_io import ImageExtractionError, safe_filename
from .image_library import CategoryNotFound, ImageLibraryError
from .upload_pipeline import UploadRequest

try:
    from quart import jsonify, request, send_file
except ImportError:  # pragma: no cover - AstrBot runtime provides quart for Plugin Pages
    jsonify = None
    request = None
    send_file = None


class WebApiService:
    def __init__(self, plugin, plugin_name: str) -> None:
        self.plugin = plugin
        self.plugin_name = plugin_name

    def register(self, context) -> None:
        register_web_api = getattr(context, "register_web_api", None)
        if not callable(register_web_api):
            return
        routes = [
            ("/stats", self.api_stats, ["GET"], "Friday image library stats"),
            ("/images", self.api_images, ["GET"], "Friday image list"),
            ("/categories", self.api_categories, ["GET"], "Friday image categories"),
            ("/inbox/stats", self.api_inbox_stats, ["GET"], "Friday image inbox stats"),
            ("/image/update", self.api_update_image, ["POST"], "Friday image update"),
            ("/image/batch-update", self.api_batch_update, ["POST"], "Friday image batch update"),
            ("/image/batch-delete", self.api_batch_delete, ["POST"], "Friday image batch delete"),
            ("/image/batch-move-category", self.api_batch_move_category, ["POST"], "Friday image batch category move"),
            ("/image/batch-tags", self.api_batch_tags, ["POST"], "Friday image batch tags"),
            ("/category/create", self.api_category_create, ["POST"], "Friday image category create"),
            ("/category/rename", self.api_category_rename, ["POST"], "Friday image category rename"),
            ("/category/merge", self.api_category_merge, ["POST"], "Friday image category merge"),
            ("/upload", self.api_upload, ["POST"], "Friday image upload"),
            ("/upload/<category>", self.api_upload, ["POST"], "Friday image upload"),
            ("/preview", self.api_preview, ["GET"], "Friday image preview"),
        ]
        for path, handler, methods, description in routes:
            register_web_api(f"/{self.plugin_name}{path}", handler, methods, description)

    async def api_stats(self):
        return self.json({"ok": True, "data": self.plugin.require_library().stats()})

    async def api_categories(self):
        return self.json({"ok": True, "data": self.plugin.require_library().category_stats()})

    async def api_inbox_stats(self):
        inbox = self.plugin.settings.upload.inbox_category
        return self.json(
            {
                "ok": True,
                "data": {
                    "category": inbox,
                    "count": self.plugin.require_library().category_image_count(inbox),
                    "image_count": self.plugin.require_library().category_image_count(inbox),
                },
            }
        )

    async def api_images(self):
        args = getattr(request, "args", {}) if request is not None else {}
        category = str(args.get("category", "")).strip()
        query = str(args.get("query", "")).strip()
        visibility = str(args.get("visibility", "")).strip() or None
        safety_status = str(args.get("safety_status", "")).strip() or None
        limit = self.int_arg(args.get("limit"), 50)
        offset = self.int_arg(args.get("offset"), 0)
        try:
            records = self.plugin.require_library().list_images(
                category=category or None,
                query=query,
                visibility=visibility,
                safety_status=safety_status,
                limit=limit,
                offset=offset,
            )
        except (ImageLibraryError, ImageExtractionError) as exc:
            return self.json({"ok": False, "error": str(exc)}, 400)
        data = [self.plugin.image_dict(record) for record in records]
        return self.json({"ok": True, "data": data})

    async def api_update_image(self):
        payload = await self.json_payload()
        image_id = str(payload.get("id", "")).strip()
        if not image_id:
            return self.json({"ok": False, "error": "缺少图片 ID。"}, 400)
        rating = payload.get("rating") if "rating" in payload else None
        clear_rating = "rating" in payload and (rating is None or rating == "")
        try:
            record = self.plugin.require_library().update_image_info(
                image_id,
                title=payload.get("title"),
                description=payload.get("description"),
                tags=payload.get("tags"),
                rating=rating,
                clear_rating=clear_rating,
                visibility=payload.get("visibility"),
                safety_status=payload.get("safety_status"),
                send_transform=payload.get("send_transform"),
            )
        except (ImageLibraryError, ImageExtractionError) as exc:
            return self.json({"ok": False, "error": str(exc)}, 400)
        return self.json({"ok": True, "data": self.plugin.image_dict(record)})

    async def api_upload(self, category: str = ""):
        temp_path = None
        try:
            uploaded = await self.uploaded_file()
            if uploaded is None:
                return self.json({"ok": False, "error": "缺少上传文件。"}, 400)
            temp_path, filename = uploaded
            summary = self.plugin.upload_pipeline().upload_file(
                temp_path,
                filename,
                UploadRequest(
                    category=(category or "").strip() or None,
                    uploader_id="web",
                    source_session="web",
                ),
            )
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
        data = summary.to_dict()
        if summary.records:
            data["image"] = self.plugin.image_dict(summary.records[-1])
        return self.json({"ok": not bool(summary.failed and not summary.records), "status": summary.status, "data": data})

    async def api_batch_update(self):
        payload = await self.json_payload()
        ids = payload.get("ids", [])
        updates = payload.get("updates", {})
        if not ids or not updates:
            return self.json({"ok": False, "error": "缺少 ids 或 updates。"}, 400)
        if not isinstance(ids, list) or not isinstance(updates, dict):
            return self.json({"ok": False, "error": "ids 必须是列表，updates 必须是对象。"}, 400)
        try:
            result = self.plugin.require_library().batch_update_image_info(ids, updates)
        except ImageLibraryError as exc:
            return self.json({"ok": False, "error": str(exc)}, 400)
        return self.json({"ok": True, "data": result})

    async def api_batch_delete(self):
        payload = await self.json_payload()
        ids = payload.get("ids", [])
        if not ids:
            return self.json({"ok": False, "error": "缺少 ids。"}, 400)
        if not isinstance(ids, list):
            return self.json({"ok": False, "error": "ids 必须是列表。"}, 400)
        deleted = 0
        failed: list[dict[str, str]] = []
        for image_id in ids:
            try:
                self.plugin.require_library().delete_image(image_id)
                deleted += 1
            except ImageLibraryError as exc:
                failed.append({"id": str(image_id), "error": str(exc)})
        return self.json({"ok": True, "data": {"deleted": deleted, "failed": failed}})

    async def api_batch_move_category(self):
        payload = await self.json_payload()
        ids = payload.get("ids", [])
        category = str(payload.get("category", "")).strip()
        if not ids or not category:
            return self.json({"ok": False, "error": "缺少 ids 或 category。"}, 400)
        if not isinstance(ids, list):
            return self.json({"ok": False, "error": "ids 必须是列表。"}, 400)
        try:
            result = self.plugin.require_library().batch_move_category(ids, category)
        except ImageLibraryError as exc:
            return self.json({"ok": False, "error": str(exc)}, 400)
        return self.json({"ok": True, "data": result})

    async def api_batch_tags(self):
        payload = await self.json_payload()
        ids = payload.get("ids", [])
        tags = payload.get("tags", [])
        operation = str(payload.get("operation", "add")).strip()
        if not ids or not tags:
            return self.json({"ok": False, "error": "缺少 ids 或 tags。"}, 400)
        if not isinstance(ids, list):
            return self.json({"ok": False, "error": "ids 必须是列表。"}, 400)
        try:
            result = self.plugin.require_library().batch_update_tags(ids, tags, operation=operation)
        except ImageLibraryError as exc:
            return self.json({"ok": False, "error": str(exc)}, 400)
        return self.json({"ok": True, "data": result})

    async def api_category_create(self):
        payload = await self.json_payload()
        category = str(payload.get("category", "")).strip()
        if not category:
            return self.json({"ok": False, "error": "缺少 category。"}, 400)
        try:
            result = self.plugin.require_library().create_category_from_input(category)
        except ImageLibraryError as exc:
            return self.json({"ok": False, "error": str(exc)}, 400)
        return self.json({"ok": True, "data": result})

    async def api_category_rename(self):
        payload = await self.json_payload()
        category = str(payload.get("category", "")).strip()
        display_name = str(payload.get("display_name", "")).strip()
        if not category or not display_name:
            return self.json({"ok": False, "error": "缺少 category 或 display_name。"}, 400)
        try:
            result = self.plugin.require_library().rename_category(category, display_name)
        except ImageLibraryError as exc:
            return self.json({"ok": False, "error": str(exc)}, 400)
        return self.json({"ok": True, "data": result})

    async def api_category_merge(self):
        payload = await self.json_payload()
        source = str(payload.get("source") or payload.get("source_category") or "").strip()
        target = str(payload.get("target") or payload.get("target_category") or "").strip()
        if not source or not target:
            return self.json({"ok": False, "error": "缺少 source 或 target。"}, 400)
        try:
            protected = self.plugin.require_library().validate_category_name(
                self.plugin.settings.upload.inbox_category
            )
            result = self.plugin.require_library().merge_categories(
                source,
                target,
                protected_slugs={protected},
            )
        except ImageLibraryError as exc:
            return self.json({"ok": False, "error": str(exc)}, 400)
        return self.json({"ok": True, "data": result})

    async def api_preview(self):
        if request is None or send_file is None:
            return self.json({"ok": False, "error": "当前环境不支持文件预览。"}, 500)
        image_id = str(request.args.get("id", "")).strip()
        record = self.plugin.require_library().get_image(image_id)
        if record is None:
            return self.json({"ok": False, "error": "图片不存在。"}, 404)
        path = record.path.resolve()
        library_root = self.plugin.require_library().library_root.resolve()
        try:
            path.relative_to(library_root)
        except ValueError:
            return self.json({"ok": False, "error": "图片文件不可读取。"}, 404)
        if not path.is_file():
            return self.json({"ok": False, "error": "图片文件不可读取。"}, 404)
        response = send_file(str(path), mimetype=self.mimetype(record.extension))
        if inspect.isawaitable(response):
            return await response
        return response

    def json(self, payload: dict[str, object], status: int = 200):
        if jsonify is None:
            return payload
        response = jsonify(payload)
        response.status_code = status
        return response

    async def json_payload(self) -> dict[str, Any]:
        if request is None:
            return {}
        payload = request.get_json(silent=True)
        if inspect.isawaitable(payload):
            payload = await payload
        return payload or {}

    async def uploaded_file(self) -> tuple[Path, str] | None:
        if request is None:
            return None
        files = request.files
        if inspect.isawaitable(files):
            files = await files
        file_obj = files.get("file") if files else None
        if file_obj is None:
            return None
        filename = getattr(file_obj, "filename", "") or "upload"
        suffix = Path(filename).suffix.lower()
        temp_path = (
            Path(tempfile.gettempdir())
            / f"friday_upload_{uuid.uuid4().hex}_{safe_filename(filename)}{suffix}"
        )
        save = getattr(file_obj, "save", None)
        if callable(save):
            result = save(str(temp_path))
            if inspect.isawaitable(result):
                await result
        else:
            read = file_obj.read()
            if inspect.isawaitable(read):
                read = await read
            temp_path.write_bytes(read)
        return temp_path, filename

    def mimetype(self, extension: str) -> str:
        return {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(extension, "application/octet-stream")

    def int_arg(self, value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
