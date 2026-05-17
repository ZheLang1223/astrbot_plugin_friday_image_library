from __future__ import annotations

import json
from pathlib import Path

from astrbot.api import logger

from .image_library import CategoryNotFound, ImageLibraryError, NoImagesFound
from .image_transform import ImageTransformError


SCHEDULE_JOB_NAME = "Friday image library scheduled send"


class ScheduleService:
    def __init__(self, plugin) -> None:
        self.plugin = plugin
        self.job_id: str | None = None
        self.group_sessions: dict[str, str] = {}

    async def initialize(self) -> None:
        self.load_sessions()
        await self.setup()

    async def terminate(self) -> None:
        await self.clear()

    async def command(self, event, action: str = "status"):
        if not self.plugin.is_group_allowed(event):
            return
        if not self.plugin.is_admin(event):
            yield event.plain_result("仅管理员可管理定时发图。")
            return

        action = (action or "status").strip().lower()
        if action in {"bind", "on"}:
            group_id = self.plugin.group_id(event)
            if not group_id:
                yield event.plain_result("请在目标群内执行 /frischedule bind。")
                return
            configured_groups = self.plugin.settings.schedule.group_ids
            if configured_groups and group_id not in configured_groups:
                yield event.plain_result("当前群不在 schedule.group_ids 配置中。")
                return
            self.group_sessions[group_id] = str(
                getattr(event, "unified_msg_origin", "") or self.plugin.session_id(event)
            )
            self.save_sessions()
            yield event.plain_result(f"已绑定定时发图群：{group_id}")
            return

        if action == "test":
            group_id = self.plugin.group_id(event)
            if not group_id:
                yield event.plain_result("请在目标群内执行 /frischedule test。")
                return
            if group_id not in self.group_sessions:
                yield event.plain_result("当前群还未绑定，请先执行 /frischedule bind。")
                return
            result = await self.send_scheduled_image(target_group_ids=[group_id], force=True)
            if result["sent"]:
                yield event.plain_result("定时发图测试已发送。")
            else:
                errors = "；".join(result["failed"][:3]) or "没有可发送图片。"
                yield event.plain_result(f"定时发图测试失败：{errors}")
            return

        if action == "reload":
            await self.setup()
            yield event.plain_result("定时发图配置已重载。")
            return

        if action == "status":
            yield event.plain_result(self.status_text())
            return

        yield event.plain_result("用法：/frischedule bind|status|test|reload")

    def sessions_path(self) -> Path:
        return self.plugin.data_root() / "schedule_sessions.json"

    def load_sessions(self) -> None:
        path = self.sessions_path()
        if not path.exists():
            self.group_sessions = {}
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.group_sessions = {
                    str(group_id): str(session)
                    for group_id, session in data.items()
                    if str(group_id).strip() and str(session).strip()
                }
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"Failed to load Friday schedule sessions: {exc}")
            self.group_sessions = {}

    def save_sessions(self) -> None:
        path = self.sessions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.group_sessions, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def setup(self) -> None:
        await self.clear()
        settings = self.plugin.settings.schedule
        if not settings.enabled:
            return
        if not settings.group_ids:
            logger.warning("schedule.enabled is true but schedule.group_ids is empty.")
            return
        cron_manager = getattr(self.plugin.context, "cron_manager", None)
        add_basic_job = getattr(cron_manager, "add_basic_job", None)
        if not callable(add_basic_job):
            logger.warning("AstrBot CronManager is not available; scheduled image send disabled.")
            return
        try:
            job = await add_basic_job(
                name=SCHEDULE_JOB_NAME,
                cron_expression=settings.cron,
                handler=self.send_scheduled_image,
                description="Friday 本地图库定时发图",
                timezone="Asia/Shanghai",
                payload={},
                enabled=True,
                persistent=False,
            )
            self.job_id = str(getattr(job, "job_id", "") or "")
        except Exception as exc:  # pragma: no cover - depends on AstrBot runtime
            logger.warning(f"Failed to register Friday scheduled send job: {exc}")
            self.job_id = None

    async def clear(self) -> None:
        if not self.job_id:
            return
        cron_manager = getattr(self.plugin.context, "cron_manager", None)
        delete_job = getattr(cron_manager, "delete_job", None)
        if callable(delete_job):
            try:
                await delete_job(self.job_id)
            except Exception as exc:  # pragma: no cover - depends on AstrBot runtime
                logger.warning(f"Failed to delete Friday scheduled send job: {exc}")
        self.job_id = None

    async def send_scheduled_image(
        self,
        *,
        target_group_ids: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, list[str] | int]:
        settings = self.plugin.settings.schedule
        if not force and not settings.enabled:
            return {"sent": 0, "failed": []}
        group_ids = target_group_ids or settings.group_ids
        failed: list[str] = []
        sent = 0
        for group_id in group_ids:
            session = self.group_sessions.get(str(group_id))
            if not session:
                failed.append(f"{group_id}: 未绑定会话，请在群内执行 /frischedule bind")
                continue
            try:
                record = self.plugin.require_library().select_random(
                    category=settings.category,
                    session_id=session,
                )
                send_path = self.plugin.commands.send_path_for_record(record)
                self.plugin.require_library().record_send(record.id, session)
                record = self.plugin.require_library().get_image(record.id) or record
                chain = self.plugin.commands.message_chain(send_path, self.plugin.image_info_text(record))
                send_message = getattr(self.plugin.context, "send_message", None)
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

    def status_text(self) -> str:
        settings = self.plugin.settings.schedule
        lines = [
            "定时发图状态：",
            f"- 启用：{'是' if settings.enabled else '否'}",
            f"- Cron：{settings.cron}",
            f"- 分类：{settings.category or '全部'}",
            f"- 已注册任务：{'是' if self.job_id else '否'}",
        ]
        if settings.group_ids:
            bound = [group_id for group_id in settings.group_ids if group_id in self.group_sessions]
            unbound = [group_id for group_id in settings.group_ids if group_id not in self.group_sessions]
            lines.append(f"- 配置群：{'、'.join(settings.group_ids)}")
            lines.append(f"- 已绑定：{'、'.join(bound) if bound else '无'}")
            lines.append(f"- 未绑定：{'、'.join(unbound) if unbound else '无'}")
        else:
            lines.append("- 配置群：未配置")
        return "\n".join(lines)
