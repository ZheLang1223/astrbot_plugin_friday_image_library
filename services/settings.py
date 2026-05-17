from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .image_io import normalize_extensions


@dataclass(frozen=True)
class BasicSettings:
    default_category: str = "默认"


@dataclass(frozen=True)
class PermissionSettings:
    allowed_group_ids: list[str] = field(default_factory=list)
    admin_qq_numbers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UploadSettings:
    allowed_extensions: set[str] = field(default_factory=lambda: normalize_extensions(None))
    max_image_size_mb: int = 20
    upload_receipt: bool = True
    inbox_category: str = "inbox"

    def __post_init__(self):
        object.__setattr__(
            self,
            "allowed_extensions",
            normalize_extensions(self.allowed_extensions),
        )

    @property
    def max_size_bytes(self) -> int:
        return max(0, int(self.max_image_size_mb or 0)) * 1024 * 1024


@dataclass(frozen=True)
class SendSettings:
    recent_window: int = 20
    max_batch_count: int = 3


@dataclass(frozen=True)
class ScheduleSettings:
    enabled: bool = False
    cron: str = "0 9 * * *"
    group_ids: list[str] = field(default_factory=list)
    category: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "group_ids", self.group_ids or [])
        category = str(self.category or "").strip() or None
        object.__setattr__(self, "category", category)


@dataclass(frozen=True)
class PluginSettings:
    basic: BasicSettings
    permission: PermissionSettings
    upload: UploadSettings
    send: SendSettings
    schedule: ScheduleSettings


FLAT_TO_NESTED = {
    "default_category": ("basic", "default_category"),
    "allowed_group_ids": ("permission", "allowed_group_ids"),
    "admin_qq_numbers": ("permission", "admin_qq_numbers"),
    "allowed_extensions": ("upload", "allowed_extensions"),
    "max_image_size_mb": ("upload", "max_image_size_mb"),
    "upload_receipt": ("upload", "upload_receipt"),
    "recent_window": ("send", "recent_window"),
    "scheduled_send_enabled": ("schedule", "enabled"),
    "scheduled_send_cron": ("schedule", "cron"),
    "scheduled_send_group_ids": ("schedule", "group_ids"),
    "scheduled_send_category": ("schedule", "category"),
}


def load_settings(config: Any) -> PluginSettings:
    migrate_flat_config(config)
    return PluginSettings(
        basic=BasicSettings(
            default_category=_string_value(
                _nested_get(config, "basic", "default_category", "默认"),
                "默认",
            ),
        ),
        permission=PermissionSettings(
            allowed_group_ids=_string_list(
                _nested_get(config, "permission", "allowed_group_ids", [])
            ),
            admin_qq_numbers=_string_list(
                _nested_get(config, "permission", "admin_qq_numbers", [])
            ),
        ),
        upload=UploadSettings(
            allowed_extensions=_nested_get(
                config,
                "upload",
                "allowed_extensions",
                ["jpg", "jpeg", "png", "gif", "webp"],
            ),
            max_image_size_mb=_int_value(
                _nested_get(config, "upload", "max_image_size_mb", 20),
                20,
            ),
            upload_receipt=bool(_nested_get(config, "upload", "upload_receipt", True)),
            inbox_category=_string_value(
                _nested_get(config, "upload", "inbox_category", "inbox"),
                "inbox",
            ),
        ),
        send=SendSettings(
            recent_window=max(
                0,
                _int_value(_nested_get(config, "send", "recent_window", 20), 20),
            ),
            max_batch_count=max(
                1,
                _int_value(_nested_get(config, "send", "max_batch_count", 3), 3),
            ),
        ),
        schedule=ScheduleSettings(
            enabled=bool(_nested_get(config, "schedule", "enabled", False)),
            cron=_cron_value(_nested_get(config, "schedule", "cron", "0 9 * * *")),
            group_ids=_string_list(_nested_get(config, "schedule", "group_ids", [])),
            category=_optional_string(_nested_get(config, "schedule", "category", "")),
        ),
    )


def migrate_flat_config(config: Any) -> bool:
    if not _is_mutable_mapping(config):
        return False
    changed = False
    for flat_key, (section, key) in FLAT_TO_NESTED.items():
        if flat_key not in config:
            continue
        section_data = config.get(section)
        if not isinstance(section_data, dict):
            section_data = {}
            config[section] = section_data
            changed = True
        if key not in section_data:
            section_data[key] = config.get(flat_key)
            changed = True
    upload = config.get("upload")
    if not isinstance(upload, dict):
        upload = {}
        config["upload"] = upload
        changed = True
    if isinstance(upload, dict) and "inbox_category" not in upload:
        upload["inbox_category"] = "inbox"
        changed = True
    send = config.get("send")
    if not isinstance(send, dict):
        send = {}
        config["send"] = send
        changed = True
    if isinstance(send, dict) and "max_batch_count" not in send:
        send["max_batch_count"] = 3
        changed = True
    if changed:
        save_config = getattr(config, "save_config", None)
        if callable(save_config):
            save_config()
    return changed


def _is_mutable_mapping(config: Any) -> bool:
    return hasattr(config, "__contains__") and hasattr(config, "__setitem__") and hasattr(config, "get")


def _nested_get(config: Any, section: str, key: str, default: Any) -> Any:
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    section_data = getter(section, {})
    if isinstance(section_data, dict) and key in section_data:
        return section_data.get(key, default)
    flat_key = _flat_key_for(section, key)
    if flat_key:
        return getter(flat_key, default)
    return default


def _flat_key_for(section: str, key: str) -> str | None:
    for flat_key, target in FLAT_TO_NESTED.items():
        if target == (section, key):
            return flat_key
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("，", ",").split(",")]
    else:
        items = [str(item).strip() for item in (value or [])]
    return [item for item in items if item]


def _string_value(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cron_value(value: Any) -> str:
    text = str(value or "").strip()
    return text if len(text.split()) == 5 else "0 9 * * *"
