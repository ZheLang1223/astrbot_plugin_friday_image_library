from __future__ import annotations

import inspect
import json
import tempfile
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from quart import jsonify, request, send_file
except ImportError:  # pragma: no cover - AstrBot runtime provides quart for Plugin Pages
    jsonify = None
    request = None
    send_file = None

try:
    from .services.image_io import (
        ImageExtractionError,
        extract_images_from_event,
        normalize_extensions,
        safe_filename,
        validate_image_file,
    )
    from .services.image_library import (
        CategoryNotFound,
        ImageLibrary,
        ImageLibraryError,
        InvalidCategoryName,
        NoImagesFound,
        UnsupportedImageType,
    )
    from .services.image_transform import ImageTransformError, transformed_send_path
except ImportError:  # pragma: no cover - compatibility with path-based plugin loaders
    from services.image_io import (
        ImageExtractionError,
        extract_images_from_event,
        normalize_extensions,
        safe_filename,
        validate_image_file,
    )
    from services.image_library import (
        CategoryNotFound,
        ImageLibrary,
        ImageLibraryError,
        InvalidCategoryName,
        NoImagesFound,
        UnsupportedImageType,
    )
    from services.image_transform import ImageTransformError, transformed_send_path


PLUGIN_NAME = "astrbot_plugin_friday_image_library"
VERSION = "1.2.6"
SCHEDULE_JOB_NAME = "Friday image library scheduled send"


@register(PLUGIN_NAME, "zhelang", "QQ 本地图片库随机发送、上传和 Web 管理插件", VERSION)
class FridayImageLibraryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.library: ImageLibrary | None = None
        self._schedule_job_id: str | None = None
        self._group_sessions: dict[str, str] = {}
        self._register_web_apis(context)

    async def initialize(self):
        self.library = self._create_library()
        self._check_pillow()
        self._load_schedule_sessions()
        await self._setup_schedule()
        logger.info(f"Friday image library initialized at {self.library.library_root}")

    def _check_pillow(self):
        try:
            from PIL import __version__
            logger.info(f"Pillow {__version__} detected")
        except ImportError:
            logger.warning(
                "Pillow not installed. Sensitive image rotation will not work. "
                "Run: pip install Pillow>=10.0.0"
            )

    @filter.command("frione")
    async def random_image(self, event: AstrMessageEvent, category: str = ""):
        async for result in self._random_image_result(event, category):
            yield result

    @filter.command("friday")
    async def friday(self, event: AstrMessageEvent, category: str = ""):
        async for result in self._random_image_result(event, category):
            yield result

    async def _random_image_result(self, event: AstrMessageEvent, category: str = ""):
        if not self._is_group_allowed(event):
            return
        library = self._require_library()
        requested_category = self._category_or_none(category)
        try:
            record = library.select_random(
                category=requested_category,
                session_id=self._session_id(event),
            )
        except CategoryNotFound:
            yield event.plain_result(self._category_missing_message(requested_category or ""))
            return
        except (NoImagesFound, InvalidCategoryName) as exc:
            yield event.plain_result(str(exc))
            return
        try:
            send_path = self._send_path_for_record(record)
        except ImageTransformError as exc:
            yield event.plain_result(str(exc))
            return

        library.record_send(record.id, self._session_id(event))
        record = library.get_image(record.id) or record
        result = self._combined_image_result(event, send_path, self._image_info_text(record))
        if result is not None:
            yield result
        else:
            yield event.image_result(str(send_path))
            yield event.plain_result(self._image_info_text(record))

    @filter.command("friup")
    async def upload(self, event: AstrMessageEvent, category: str = ""):
        if not self._is_group_allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可上传图片。")
            return
        library = self._require_library()
        category_input = self._category_or_default(category)
        try:
            library.validate_category_name(category_input)
        except InvalidCategoryName as exc:
            yield event.plain_result(str(exc))
            return

        extraction = await extract_images_from_event(
            event,
            allowed_extensions=self._allowed_extensions(),
            max_size_bytes=self._max_size_bytes(),
        )
        if not extraction.images:
            details = "\n".join(f"- {error}" for error in extraction.errors[:3])
            suffix = f"\n{details}" if details else ""
            yield event.plain_result(
                "没有检测到可上传的图片。请发送 /friup 分类名 并附带图片，"
                "或回复一条图片消息后发送该指令。"
                f"{suffix}"
            )
            return

        saved_count = 0
        duplicate_count = 0
        failed: list[str] = list(extraction.errors)

        for image in extraction.images:
            try:
                result = library.add_image(
                    category=category_input,
                    source_path=image.path,
                    original_name=image.source_name,
                    detected_extension=image.extension,
                    uploader_id=self._sender_id(event),
                    source_session=self._session_id(event),
                )
            except (ImageLibraryError, UnsupportedImageType) as exc:
                failed.append(str(exc))
                continue
            if result.status == "duplicate":
                duplicate_count += 1
            else:
                saved_count += 1

        if not self._upload_receipt() and saved_count > 0 and duplicate_count == 0 and not failed:
            return

        lines = [
            f"上传完成：分类 {category_input}",
            f"- 新增：{saved_count} 张",
            f"- 已存在：{duplicate_count} 张",
        ]
        if failed:
            lines.append("- 失败：" + "；".join(failed[:3]))
        yield event.plain_result("\n".join(lines))

    @filter.command("friupload")
    async def upload_alias(self, event: AstrMessageEvent, category: str = ""):
        async for result in self.upload(event, category):
            yield result

    @filter.command("friclass")
    async def categories(self, event: AstrMessageEvent):
        if not self._is_group_allowed(event):
            return
        categories = self._require_library().category_stats()
        if not categories:
            yield event.plain_result("图库还没有分类。可以发送 /friup 分类名 并附带图片。")
            return
        lines = ["当前图库分类："]
        lines.extend(
            f"- {item['category']}: {item['image_count']} 张，发送 {item['send_count']} 次"
            for item in categories
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("frihelp")
    async def help(self, event: AstrMessageEvent):
        if not self._is_group_allowed(event):
            return
        yield event.plain_result(
            "\n".join(
                [
                    "Friday 本地图库 v1.3：",
                    "/friday - 从全部分类随机发一张",
                    "/friday 分类名 - 从指定分类随机发一张",
                    "/frione - 从全部分类随机发一张",
                    "/frione 分类名 - 从指定分类随机发一张",
                    "/friclass - 查看分类和数量",
                    "/friup 分类名 - 附带图片或回复图片后上传",
                    "/frischedule status - 查看定时发图状态",
                    "/frihelp - 查看帮助",
                    "提示：/friupload 仍可用作 /friup 的别名",
                ]
            )
        )

    @filter.command("frischedule")
    async def schedule(self, event: AstrMessageEvent, action: str = "status"):
        if not self._is_group_allowed(event):
            return
        if not self._is_admin(event):
            yield event.plain_result("仅管理员可管理定时发图。")
            return

        action = (action or "status").strip().lower()
        if action in {"bind", "on"}:
            group_id = self._group_id(event)
            if not group_id:
                yield event.plain_result("请在目标群内执行 /frischedule bind。")
                return
            scheduled_groups = self._scheduled_group_ids()
            if scheduled_groups and group_id not in scheduled_groups:
                yield event.plain_result("当前群不在 scheduled_send_group_ids 配置中。")
                return
            unified_origin = getattr(event, "unified_msg_origin", "") or self._session_id(event)
            self._group_sessions[group_id] = str(unified_origin)
            self._save_schedule_sessions()
            yield event.plain_result(f"已绑定定时发图群：{group_id}")
            return

        if action == "test":
            group_id = self._group_id(event)
            if not group_id:
                yield event.plain_result("请在目标群内执行 /frischedule test。")
                return
            if group_id not in self._group_sessions:
                yield event.plain_result("当前群还未绑定，请先执行 /frischedule bind。")
                return
            result = await self._send_scheduled_image(target_group_ids=[group_id], force=True)
            if result["sent"]:
                yield event.plain_result("定时发图测试已发送。")
            else:
                errors = "；".join(result["failed"][:3]) or "没有可发送图片。"
                yield event.plain_result(f"定时发图测试失败：{errors}")
            return

        if action == "reload":
            await self._setup_schedule()
            yield event.plain_result("定时发图配置已重载。")
            return

        if action == "status":
            yield event.plain_result(self._schedule_status_text())
            return

        yield event.plain_result("用法：/frischedule bind|status|test|reload")

    async def terminate(self):
        await self._clear_schedule()
        logger.info("Friday image library plugin terminated.")

    async def api_stats(self):
        return self._json({"ok": True, "data": self._require_library().stats()})

    async def api_categories(self):
        return self._json({"ok": True, "data": self._require_library().category_stats()})

    async def api_images(self):
        args = getattr(request, "args", {}) if request is not None else {}
        category = str(args.get("category", "")).strip()
        query = str(args.get("query", "")).strip()
        visibility = str(args.get("visibility", "")).strip() or None
        safety_status = str(args.get("safety_status", "")).strip() or None
        limit = _int_arg(args.get("limit"), 50)
        offset = _int_arg(args.get("offset"), 0)
        try:
            records = self._require_library().list_images(
                category=category or None,
                query=query,
                visibility=visibility,
                safety_status=safety_status,
                limit=limit,
                offset=offset,
            )
        except (ImageLibraryError, ImageExtractionError) as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        data = [self._image_dict(record) for record in records]
        return self._json({"ok": True, "data": data})

    async def api_update_image(self):
        payload = await self._json_payload()
        image_id = str(payload.get("id", "")).strip()
        if not image_id:
            return self._json({"ok": False, "error": "缺少图片 ID。"}, 400)
        rating = payload.get("rating") if "rating" in payload else None
        clear_rating = "rating" in payload and (rating is None or rating == "")
        try:
            record = self._require_library().update_image_info(
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
            return self._json({"ok": False, "error": str(exc)}, 400)
        return self._json({"ok": True, "data": self._image_dict(record)})

    async def api_upload(self, category: str = ""):
        category = self._category_or_default(category)
        library = self._require_library()
        try:
            library.validate_category_name(category)
            uploaded = await self._uploaded_file()
            if uploaded is None:
                return self._json({"ok": False, "error": "缺少上传文件。"}, 400)
            temp_path, filename = uploaded
            image = validate_image_file(
                temp_path,
                allowed_extensions=self._allowed_extensions(),
                max_size_bytes=self._max_size_bytes(),
            )
            result = library.add_image(
                category=category,
                source_path=image.path,
                original_name=filename or image.source_name,
                detected_extension=image.extension,
                uploader_id="web",
                source_session="web",
            )
        except (ImageLibraryError, ImageExtractionError) as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        finally:
            if "temp_path" in locals() and temp_path.exists():
                temp_path.unlink()
        return self._json(
            {
                "ok": True,
                "status": result.status,
                "data": self._image_dict(result.record),
            }
        )

    async def api_preview(self):
        if request is None or send_file is None:
            return self._json({"ok": False, "error": "当前环境不支持文件预览。"}, 500)
        image_id = str(request.args.get("id", "")).strip()
        record = self._require_library().get_image(image_id)
        if record is None:
            return self._json({"ok": False, "error": "图片不存在。"}, 404)
        path = record.path.resolve()
        library_root = self._require_library().library_root.resolve()
        try:
            path.relative_to(library_root)
        except ValueError:
            return self._json({"ok": False, "error": "图片文件不可读取。"}, 404)
        if not path.is_file():
            return self._json({"ok": False, "error": "图片文件不可读取。"}, 404)
        response = send_file(str(path), mimetype=self._mimetype(record.extension))
        if inspect.isawaitable(response):
            return await response
        return response

    async def api_batch_update(self):
        payload = await self._json_payload()
        ids = payload.get("ids", [])
        updates = payload.get("updates", {})
        if not ids or not updates:
            return self._json({"ok": False, "error": "缺少 ids 或 updates。"}, 400)
        if not isinstance(ids, list) or not isinstance(updates, dict):
            return self._json({"ok": False, "error": "ids 必须是列表，updates 必须是对象。"}, 400)
        try:
            result = self._require_library().batch_update_image_info(ids, updates)
        except ImageLibraryError as exc:
            return self._json({"ok": False, "error": str(exc)}, 400)
        return self._json({"ok": True, "data": result})

    async def api_batch_delete(self):
        payload = await self._json_payload()
        ids = payload.get("ids", [])
        if not ids:
            return self._json({"ok": False, "error": "缺少 ids。"}, 400)
        if not isinstance(ids, list):
            return self._json({"ok": False, "error": "ids 必须是列表。"}, 400)
        library = self._require_library()
        deleted = 0
        failed: list[dict[str, str]] = []
        for image_id in ids:
            try:
                library.delete_image(image_id)
                deleted += 1
            except ImageLibraryError as exc:
                failed.append({"id": str(image_id), "error": str(exc)})
        return self._json({"ok": True, "data": {"deleted": deleted, "failed": failed}})

    def _register_web_apis(self, context: Context) -> None:
        register_web_api = getattr(context, "register_web_api", None)
        if not callable(register_web_api):
            return
        routes = [
            ("/stats", self.api_stats, ["GET"], "Friday image library stats"),
            ("/images", self.api_images, ["GET"], "Friday image list"),
            ("/categories", self.api_categories, ["GET"], "Friday image categories"),
            ("/image/update", self.api_update_image, ["POST"], "Friday image update"),
            ("/image/batch-update", self.api_batch_update, ["POST"], "Friday image batch update"),
            ("/image/batch-delete", self.api_batch_delete, ["POST"], "Friday image batch delete"),
            ("/upload", self.api_upload, ["POST"], "Friday image upload"),
            ("/upload/<category>", self.api_upload, ["POST"], "Friday image upload"),
            ("/preview", self.api_preview, ["GET"], "Friday image preview"),
        ]
        for path, handler, methods, description in routes:
            register_web_api(f"/{PLUGIN_NAME}{path}", handler, methods, description)

    def _create_library(self) -> ImageLibrary:
        data_root = self._data_root()
        library_root = data_root / "library"
        return ImageLibrary(
            library_root,
            db_path=data_root / "friday_images.sqlite3",
            allowed_extensions=self._allowed_extensions(),
            recent_window=self._recent_window(),
        )

    def _transform_root(self) -> Path:
        return self._data_root() / "transformed"

    def _data_root(self) -> Path:
        plugin_name = getattr(self, "name", PLUGIN_NAME) or PLUGIN_NAME
        return Path(get_astrbot_data_path()) / "plugin_data" / plugin_name

    def _require_library(self) -> ImageLibrary:
        if self.library is None:
            self.library = self._create_library()
        return self.library

    def _config_get(self, key: str, default: Any) -> Any:
        getter = getattr(self.config, "get", None)
        if callable(getter):
            return getter(key, default)
        return default

    def _allowed_extensions(self) -> set[str]:
        return normalize_extensions(self._config_get("allowed_extensions", None))

    def _max_size_bytes(self) -> int:
        mb = self._config_get("max_image_size_mb", 20)
        try:
            return max(0, int(mb)) * 1024 * 1024
        except (TypeError, ValueError):
            return 20 * 1024 * 1024

    def _recent_window(self) -> int:
        value = self._config_get("recent_window", 20)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 20

    def _upload_receipt(self) -> bool:
        return bool(self._config_get("upload_receipt", True))

    def _allowed_group_ids(self) -> list[str]:
        raw = self._config_get("allowed_group_ids", [])
        return [str(g).strip() for g in raw if str(g).strip()]

    def _is_group_allowed(self, event: AstrMessageEvent) -> bool:
        allowed = self._allowed_group_ids()
        if not allowed:
            return True
        group_id = self._group_id(event)
        if not group_id:
            return True
        return str(group_id) in allowed

    def _admin_qq_numbers(self) -> list[str]:
        raw = self._config_get("admin_qq_numbers", [])
        return [str(q).strip() for q in raw if str(q).strip()]

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self._admin_qq_numbers()
        if not admins:
            return True
        return self._sender_id(event) in admins

    def _scheduled_send_enabled(self) -> bool:
        return bool(self._config_get("scheduled_send_enabled", False))

    def _scheduled_cron(self) -> str:
        value = str(self._config_get("scheduled_send_cron", "0 9 * * *")).strip()
        return value if len(value.split()) == 5 else "0 9 * * *"

    def _scheduled_group_ids(self) -> list[str]:
        raw = self._config_get("scheduled_send_group_ids", [])
        return [str(g).strip() for g in raw if str(g).strip()]

    def _scheduled_category(self) -> str | None:
        category = str(self._config_get("scheduled_send_category", "") or "").strip()
        return category or None

    def _schedule_sessions_path(self) -> Path:
        return self._data_root() / "schedule_sessions.json"

    def _load_schedule_sessions(self) -> None:
        path = self._schedule_sessions_path()
        if not path.exists():
            self._group_sessions = {}
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._group_sessions = {
                    str(group_id): str(session)
                    for group_id, session in data.items()
                    if str(group_id).strip() and str(session).strip()
                }
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Failed to load Friday schedule sessions: {exc}")
            self._group_sessions = {}

    def _save_schedule_sessions(self) -> None:
        path = self._schedule_sessions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self._group_sessions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _setup_schedule(self) -> None:
        await self._clear_schedule()
        if not self._scheduled_send_enabled():
            return
        if not self._scheduled_group_ids():
            logger.warning("scheduled_send_enabled is true but scheduled_send_group_ids is empty.")
            return
        cron_manager = getattr(self.context, "cron_manager", None)
        add_basic_job = getattr(cron_manager, "add_basic_job", None)
        if not callable(add_basic_job):
            logger.warning("AstrBot CronManager is not available; scheduled image send disabled.")
            return
        try:
            job = await add_basic_job(
                name=SCHEDULE_JOB_NAME,
                cron_expression=self._scheduled_cron(),
                handler=self._send_scheduled_image,
                description="Friday 本地图库定时发图",
                timezone="Asia/Shanghai",
                payload={},
                enabled=True,
                persistent=False,
            )
            self._schedule_job_id = str(getattr(job, "job_id", "") or "")
        except Exception as exc:  # pragma: no cover - depends on AstrBot runtime
            logger.warning(f"Failed to register Friday scheduled send job: {exc}")
            self._schedule_job_id = None

    async def _clear_schedule(self) -> None:
        if not self._schedule_job_id:
            return
        cron_manager = getattr(self.context, "cron_manager", None)
        delete_job = getattr(cron_manager, "delete_job", None)
        if callable(delete_job):
            try:
                await delete_job(self._schedule_job_id)
            except Exception as exc:  # pragma: no cover - depends on AstrBot runtime
                logger.warning(f"Failed to delete Friday scheduled send job: {exc}")
        self._schedule_job_id = None

    async def _send_scheduled_image(
        self,
        *,
        target_group_ids: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, list[str] | int]:
        if not force and not self._scheduled_send_enabled():
            return {"sent": 0, "failed": []}
        group_ids = target_group_ids or self._scheduled_group_ids()
        failed: list[str] = []
        sent = 0
        for group_id in group_ids:
            session = self._group_sessions.get(str(group_id))
            if not session:
                failed.append(f"{group_id}: 未绑定会话，请在群内执行 /frischedule bind")
                continue
            try:
                record = self._require_library().select_random(
                    category=self._scheduled_category(),
                    session_id=session,
                )
                send_path = self._send_path_for_record(record)
                self._require_library().record_send(record.id, session)
                record = self._require_library().get_image(record.id) or record
                chain = self._message_chain(send_path, self._image_info_text(record))
                send_message = getattr(self.context, "send_message", None)
                if not callable(send_message):
                    raise ImageLibraryError("当前 AstrBot Context 不支持主动发送。")
                ok = await send_message(session, chain)
                if ok is False:
                    raise ImageLibraryError("AstrBot 未找到可发送的目标会话。")
                sent += 1
            except (ImageLibraryError, ImageTransformError, CategoryNotFound, NoImagesFound) as exc:
                failed.append(f"{group_id}: {exc}")
            except Exception as exc:  # pragma: no cover - adapter/runtime guard
                logger.warning(f"Friday scheduled send failed for group {group_id}: {exc}")
                failed.append(f"{group_id}: {exc}")
        return {"sent": sent, "failed": failed}

    def _schedule_status_text(self) -> str:
        group_ids = self._scheduled_group_ids()
        lines = [
            "定时发图状态：",
            f"- 启用：{'是' if self._scheduled_send_enabled() else '否'}",
            f"- Cron：{self._scheduled_cron()}",
            f"- 分类：{self._scheduled_category() or '全部'}",
            f"- 已注册任务：{'是' if self._schedule_job_id else '否'}",
        ]
        if group_ids:
            bound = [group_id for group_id in group_ids if group_id in self._group_sessions]
            unbound = [group_id for group_id in group_ids if group_id not in self._group_sessions]
            lines.append(f"- 配置群：{'、'.join(group_ids)}")
            lines.append(f"- 已绑定：{'、'.join(bound) if bound else '无'}")
            lines.append(f"- 未绑定：{'、'.join(unbound) if unbound else '无'}")
        else:
            lines.append("- 配置群：未配置")
        return "\n".join(lines)

    def _category_or_default(self, category: str) -> str:
        category = (category or "").strip()
        if category:
            return category
        return str(self._config_get("default_category", "默认")).strip() or "默认"

    def _category_or_none(self, category: str) -> str | None:
        category = (category or "").strip()
        return category or None

    def _session_id(self, event: AstrMessageEvent) -> str:
        unified = getattr(event, "unified_msg_origin", "")
        if unified:
            return str(unified)
        message_obj = getattr(event, "message_obj", None)
        session_id = getattr(message_obj, "session_id", "")
        if session_id:
            return str(session_id)
        return self._sender_id(event) or "global"

    def _sender_id(self, event: AstrMessageEvent) -> str:
        get_sender_id = getattr(event, "get_sender_id", None)
        if callable(get_sender_id):
            return str(get_sender_id())
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        user_id = getattr(sender, "user_id", "")
        return str(user_id or "")

    def _group_id(self, event: AstrMessageEvent) -> str:
        get_group_id = getattr(event, "get_group_id", None)
        if callable(get_group_id):
            group_id = get_group_id()
            if group_id:
                return str(group_id)
        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", "")
        return str(group_id or "")

    def _category_missing_message(self, category: str) -> str:
        categories = self._require_library().list_categories()
        if not categories:
            return f"分类不存在：{category}。当前图库还没有分类。"
        available = "、".join(name for name, _ in categories)
        return f"分类不存在：{category}。可用分类：{available}"

    def _image_info_text(self, record) -> str:
        description = record.description or "未填写"
        tags = "、".join(record.tags) if record.tags else "未标记"
        return "\n".join(
            [
                f"标题：{record.title or record.short_id}",
                f"描述：{description}",
                f"标签：{tags}",
                f"发送次数：{record.send_count}",
            ]
        )

    def _send_path_for_record(self, record) -> Path:
        if record.safety_status == "sensitive" and record.send_transform == "none":
            record = self._require_library().update_image_info(
                record.id,
                safety_status="sensitive",
                send_transform="rotate_180",
            )
        return transformed_send_path(record, self._transform_root())

    def _message_chain(self, image_path: Path, text: str) -> MessageChain:
        return MessageChain().file_image(str(image_path)).message(text)

    def _combined_image_result(self, event: AstrMessageEvent, image_path: Path, text: str):
        try:
            return event.chain_result([Comp.Image.fromFileSystem(str(image_path)), Comp.Plain(text)])
        except Exception as exc:  # pragma: no cover - adapter compatibility fallback
            logger.warning(f"Failed to build combined image result: {exc}")
            return None

    def _image_dict(self, record) -> dict[str, object]:
        data = self._require_library().to_dict(record)
        data["preview_url"] = f"/api/plug/{PLUGIN_NAME}/preview?id={record.id}"
        return data

    def _json(self, payload: dict[str, object], status: int = 200):
        if jsonify is None:
            return payload
        response = jsonify(payload)
        response.status_code = status
        return response

    async def _json_payload(self) -> dict[str, Any]:
        if request is None:
            return {}
        payload = request.get_json(silent=True)
        if inspect.isawaitable(payload):
            payload = await payload
        return payload or {}

    async def _uploaded_file(self) -> tuple[Path, str] | None:
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

    def _mimetype(self, extension: str) -> str:
        return {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(extension, "application/octet-stream")


def _int_arg(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
