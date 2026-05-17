from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain

from .image_library import CategoryNotFound, ImageLibraryError, InvalidCategoryName, NoImagesFound
from .image_transform import ImageTransformError, transformed_send_path
from .upload_pipeline import UploadRequest


class CommandService:
    def __init__(self, plugin) -> None:
        self.plugin = plugin

    async def random_image(
        self,
        event: AstrMessageEvent,
        token: str = "",
        count_text: str = "",
    ) -> AsyncGenerator[object, None]:
        if not self.plugin.is_group_allowed(event):
            return
        category, tag, count = self._parse_random_args(token, count_text)
        library = self.plugin.require_library()
        sent = 0
        for _ in range(count):
            try:
                record = library.select_random(
                    category=category,
                    tag=tag,
                    session_id=self.plugin.session_id(event),
                )
            except CategoryNotFound:
                yield event.plain_result(self.plugin.category_missing_message(category or ""))
                return
            except (NoImagesFound, InvalidCategoryName) as exc:
                yield event.plain_result(str(exc))
                return
            try:
                send_path = self.send_path_for_record(record)
            except ImageTransformError as exc:
                yield event.plain_result(str(exc))
                return
            library.record_send(record.id, self.plugin.session_id(event))
            record = library.get_image(record.id) or record
            result = self.combined_image_result(event, send_path, self.plugin.image_info_text(record))
            if result is not None:
                yield result
            else:
                yield event.image_result(str(send_path))
                yield event.plain_result(self.plugin.image_info_text(record))
            sent += 1
        if sent == 0:
            yield event.plain_result("图库里还没有可发送图片。")

    async def upload(
        self,
        event: AstrMessageEvent,
        category: str = "",
    ) -> AsyncGenerator[object, None]:
        if not self.plugin.is_group_allowed(event):
            return
        if not self.plugin.is_admin(event):
            yield event.plain_result("仅管理员可上传图片。")
            return
        request = UploadRequest(
            category=(category or "").strip() or None,
            uploader_id=self.plugin.sender_id(event),
            source_session=self.plugin.session_id(event),
        )
        summary = await self.plugin.upload_pipeline().upload_event(event, request)
        if not summary.records and not summary.failed:
            yield event.plain_result(
                "没有检测到可上传的图片。请发送 /friup 并附带图片，"
                "或回复一条图片消息后发送该指令。"
            )
            return
        if (
            not self.plugin.settings.upload.upload_receipt
            and summary.saved_count > 0
            and summary.duplicate_count == 0
            and not summary.failed
        ):
            return
        lines = [
            f"上传完成：分类 {category.strip() if category.strip() else '待整理'}",
            f"- 新增：{summary.saved_count} 张",
            f"- 已存在：{summary.duplicate_count} 张",
        ]
        if summary.failed:
            lines.append("- 失败：" + "；".join(summary.failed[:3]))
        yield event.plain_result("\n".join(lines))

    async def categories(self, event: AstrMessageEvent) -> AsyncGenerator[object, None]:
        if not self.plugin.is_group_allowed(event):
            return
        categories = self.plugin.require_library().category_stats()
        if not categories:
            yield event.plain_result("图库还没有分类。可以发送 /friup 并附带图片。")
            return
        lines = ["当前图库分类："]
        lines.extend(
            f"- {item['category']}: {item['image_count']} 张，发送 {item['send_count']} 次"
            for item in categories
        )
        yield event.plain_result("\n".join(lines))

    async def help(self, event: AstrMessageEvent) -> AsyncGenerator[object, None]:
        if not self.plugin.is_group_allowed(event):
            return
        yield event.plain_result(
            "\n".join(
                [
                    "Friday 本地图库 v1.4：",
                    "/friday - 从全部分类随机发一张",
                    "/friday 分类名 - 从指定分类随机发一张",
                    "/friday #标签 - 从指定标签随机发一张",
                    "/friday 分类名 数量 - 一次发送多张",
                    "/friup - 附带图片或回复图片后上传到待整理",
                    "/friup 分类名 - 上传到指定分类",
                    "/frihelp - 查看帮助",
                ]
            )
        )

    def send_path_for_record(self, record) -> Path:
        if record.safety_status == "sensitive" and record.send_transform == "none":
            record = self.plugin.require_library().update_image_info(
                record.id,
                safety_status="sensitive",
                send_transform="rotate_180",
            )
        return transformed_send_path(record, self.plugin.transform_root())

    def message_chain(self, image_path: Path, text: str) -> MessageChain:
        return MessageChain().file_image(str(image_path)).message(text)

    def combined_image_result(self, event: AstrMessageEvent, image_path: Path, text: str):
        try:
            return event.chain_result([Comp.Image.fromFileSystem(str(image_path)), Comp.Plain(text)])
        except Exception:
            return None

    def _parse_random_args(self, token: str, count_text: str) -> tuple[str | None, str | None, int]:
        token = (token or "").strip()
        count = self._parse_count(count_text)
        if token.isdigit() and not count_text:
            count = self._bounded_count(token)
            token = ""
        category = token or None
        tag = None
        if token.startswith("#") and len(token) > 1:
            tag = token[1:].strip()
            category = None
        return category, tag, count

    def _parse_count(self, value: str) -> int:
        if not value:
            return 1
        return self._bounded_count(value)

    def _bounded_count(self, value: str) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError):
            return 1
        return max(1, min(count, self.plugin.settings.send.max_batch_count))
