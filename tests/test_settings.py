from __future__ import annotations

import unittest

from services.settings import load_settings, migrate_flat_config


class MutableConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.saved = False

    def save_config(self) -> None:
        self.saved = True


class SettingsTest(unittest.TestCase):
    def test_flat_config_migrates_to_nested_and_saves(self) -> None:
        config = MutableConfig(
            {
                "default_category": "旧默认",
                "allowed_group_ids": ["10001"],
                "admin_qq_numbers": "20001,20002",
                "allowed_extensions": "jpg,png",
                "max_image_size_mb": 8,
                "recent_window": 7,
                "upload_receipt": False,
                "scheduled_send_enabled": True,
                "scheduled_send_cron": "30 8 * * *",
                "scheduled_send_group_ids": ["10001"],
                "scheduled_send_category": "猫猫",
            }
        )

        changed = migrate_flat_config(config)
        settings = load_settings(config)

        self.assertTrue(changed)
        self.assertTrue(config.saved)
        self.assertEqual(config["basic"]["default_category"], "旧默认")
        self.assertEqual(settings.basic.default_category, "旧默认")
        self.assertEqual(settings.permission.allowed_group_ids, ["10001"])
        self.assertEqual(settings.permission.admin_qq_numbers, ["20001", "20002"])
        self.assertEqual(settings.upload.max_image_size_mb, 8)
        self.assertFalse(settings.upload.upload_receipt)
        self.assertEqual(settings.send.recent_window, 7)
        self.assertEqual(settings.schedule.cron, "30 8 * * *")
        self.assertEqual(settings.schedule.category, "猫猫")

    def test_nested_config_missing_fields_uses_defaults(self) -> None:
        settings = load_settings({"permission": {"allowed_group_ids": ["10001"]}})

        self.assertEqual(settings.basic.default_category, "默认")
        self.assertEqual(settings.permission.allowed_group_ids, ["10001"])
        self.assertIn("jpg", settings.upload.allowed_extensions)
        self.assertEqual(settings.upload.inbox_category, "inbox")
        self.assertEqual(settings.send.max_batch_count, 3)
        self.assertFalse(settings.schedule.enabled)


if __name__ == "__main__":
    unittest.main()
