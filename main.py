from __future__ import annotations

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

try:
    from .services.commands import CommandService
    from .services.image_io import normalize_extensions
    from .services.image_library import ImageLibrary
    from .services.scheduler import ScheduleService
    from .services.settings import PluginSettings, load_settings
    from .services.upload_pipeline import UploadPipeline
    from .services.web_api import WebApiService
except ImportError:  # pragma: no cover - compatibility with path-based plugin loaders
    from services.commands import CommandService
    from services.image_io import normalize_extensions
    from services.image_library import ImageLibrary
    from services.scheduler import ScheduleService
    from services.settings import PluginSettings, load_settings
    from services.upload_pipeline import UploadPipeline
    from services.web_api import WebApiService


PLUGIN_NAME = "astrbot_plugin_friday_image_library"
VERSION = "1.4.5"


@register(PLUGIN_NAME, "zhelang", "QQ 本地图片库随机发送、上传和 Web 管理插件", VERSION)
class FridayImageLibraryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.settings: PluginSettings = load_settings(self.config)
        self.library: ImageLibrary | None = None
        self._upload_pipeline: UploadPipeline | None = None
        self.commands = CommandService(self)
        self.scheduler = ScheduleService(self)
        self.web_api = WebApiService(self, PLUGIN_NAME)
        self.web_api.register(context)

    async def initialize(self):
        self.settings = load_settings(self.config)
        self.library = self._create_library()
        self._upload_pipeline = UploadPipeline(self.library, self.settings.upload)
        self._check_pillow()
        await self.scheduler.initialize()
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

    @filter.command("friday")
    async def friday(self, event: AstrMessageEvent, token: str = "", count: str = ""):
        await self.commands.random_image(event, token, count)

    @filter.command("frione")
    async def random_image_alias(self, event: AstrMessageEvent, token: str = "", count: str = ""):
        await self.commands.random_image(event, token, count)

    @filter.command("friup")
    async def upload(self, event: AstrMessageEvent, category: str = ""):
        await self.commands.upload(event, category)

    @filter.command("friupload")
    async def upload_alias(self, event: AstrMessageEvent, category: str = ""):
        await self.commands.upload(event, category)

    @filter.command("friclass")
    async def categories(self, event: AstrMessageEvent):
        await self.commands.categories(event)

    @filter.command("frihelp")
    async def help(self, event: AstrMessageEvent):
        await self.commands.help(event)

    @filter.command("frischedule")
    async def schedule(self, event: AstrMessageEvent, action: str = "status"):
        async for result in self.scheduler.command(event, action):
            yield result

    async def terminate(self):
        await self.scheduler.terminate()
        logger.info("Friday image library plugin terminated.")

    def _create_library(self) -> ImageLibrary:
        data_root = self.data_root()
        library_root = data_root / "library"
        return ImageLibrary(
            library_root,
            db_path=data_root / "friday_images.sqlite3",
            allowed_extensions=normalize_extensions(self.settings.upload.allowed_extensions),
            recent_window=self.settings.send.recent_window,
            default_category=self.settings.basic.default_category,
        )

    def data_root(self) -> Path:
        plugin_name = getattr(self, "name", PLUGIN_NAME) or PLUGIN_NAME
        return Path(get_astrbot_data_path()) / "plugin_data" / plugin_name

    def transform_root(self) -> Path:
        return self.data_root() / "transformed"

    def require_library(self) -> ImageLibrary:
        if self.library is None:
            self.library = self._create_library()
        return self.library

    def upload_pipeline(self) -> UploadPipeline:
        if self._upload_pipeline is None:
            self._upload_pipeline = UploadPipeline(self.require_library(), self.settings.upload)
        return self._upload_pipeline

    def is_group_allowed(self, event: AstrMessageEvent) -> bool:
        allowed = self.settings.permission.allowed_group_ids
        if not allowed:
            return True
        group_id = self.group_id(event)
        if not group_id:
            return True
        return group_id in allowed

    def is_admin(self, event: AstrMessageEvent) -> bool:
        admins = self.settings.permission.admin_qq_numbers
        if not admins:
            return True
        return self.sender_id(event) in admins

    def session_id(self, event: AstrMessageEvent) -> str:
        unified = getattr(event, "unified_msg_origin", "")
        if unified:
            return str(unified)
        message_obj = getattr(event, "message_obj", None)
        session_id = getattr(message_obj, "session_id", "")
        if session_id:
            return str(session_id)
        return self.sender_id(event) or "global"

    def sender_id(self, event: AstrMessageEvent) -> str:
        get_sender_id = getattr(event, "get_sender_id", None)
        if callable(get_sender_id):
            return str(get_sender_id())
        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        user_id = getattr(sender, "user_id", "")
        return str(user_id or "")

    def group_id(self, event: AstrMessageEvent) -> str:
        get_group_id = getattr(event, "get_group_id", None)
        if callable(get_group_id):
            group_id = get_group_id()
            if group_id:
                return str(group_id)
        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", "")
        return str(group_id or "")

    def category_missing_message(self, category: str) -> str:
        categories = self.require_library().list_categories()
        if not categories:
            return f"分类不存在：{category}。当前图库还没有分类。"
        available = "、".join(name for name, _ in categories)
        return f"分类不存在：{category}。可用分类：{available}"

    def image_info_text(self, record) -> str:
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

    def image_dict(self, record) -> dict[str, object]:
        data = self.require_library().to_dict(record)
        data["preview_url"] = f"/api/plug/{PLUGIN_NAME}/preview?id={record.id}"
        return data
