from __future__ import annotations

import unittest
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_event_module = types.ModuleType("astrbot.api.event")


class StubAstrMessageEvent:
    pass


class StubMessageChain:
    def __init__(self) -> None:
        self.chain = []

    def message(self, text: str):
        self.chain.append(("text", text))
        return self

    def file_image(self, path: str):
        self.chain.append(("image", path))
        return self


astrbot_event_module.AstrMessageEvent = StubAstrMessageEvent
astrbot_event_module.MessageChain = StubMessageChain
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules.setdefault("astrbot.api", astrbot_api_module)
sys.modules.setdefault("astrbot.api.event", astrbot_event_module)

from services.commands import CommandService


@dataclass
class FakeSendSettings:
    max_batch_count: int = 3


@dataclass
class FakeSettings:
    send: FakeSendSettings


@dataclass
class FakeRecord:
    id: str = "image-id"
    short_id: str = "image-id"
    safety_status: str = "normal"
    send_transform: str = "none"
    send_count: int = 0


class FakeLibrary:
    def __init__(self, record: FakeRecord) -> None:
        self.record = record
        self.recorded: list[tuple[str, str]] = []

    def select_random(self, *, category, tag, session_id):
        return self.record

    def record_send(self, image_id: str, session_id: str) -> FakeRecord:
        self.recorded.append((image_id, session_id))
        self.record = FakeRecord(send_count=self.record.send_count + 1)
        return self.record


class FakePlugin:
    def __init__(self, library: FakeLibrary, image_path: Path) -> None:
        self.library = library
        self.image_path = image_path
        self.settings = FakeSettings(send=FakeSendSettings())
        self.info_records: list[FakeRecord] = []

    def is_group_allowed(self, event) -> bool:
        return True

    def require_library(self) -> FakeLibrary:
        return self.library

    def session_id(self, event) -> str:
        return "session"

    def image_info_text(self, record) -> str:
        self.info_records.append(record)
        return f"发送次数：{record.send_count}"

    def transform_root(self) -> Path:
        return self.image_path.parent


class FakeEvent:
    def __init__(self, *, fail_on_send: int | None = None) -> None:
        self.fail_on_send = fail_on_send
        self.sent = 0

    async def send(self, message_chain) -> None:
        self.sent += 1
        if self.fail_on_send == self.sent:
            raise RuntimeError("send failed")


class CommandServiceTest(unittest.IsolatedAsyncioTestCase):
    async def test_random_image_records_after_image_send_success(self) -> None:
        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "image.jpg"
            image_path.write_bytes(b"\xff\xd8\xff")
            record = FakeRecord()
            library = FakeLibrary(record)
            plugin = FakePlugin(library, image_path)
            service = CommandService(plugin)
            service.send_path_for_record = lambda selected: image_path

            await service.random_image(FakeEvent())

            self.assertEqual(library.recorded, [(record.id, "session")])
            self.assertEqual(plugin.info_records[-1].send_count, 1)

    async def test_random_image_does_not_record_when_image_send_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "image.jpg"
            image_path.write_bytes(b"\xff\xd8\xff")
            library = FakeLibrary(FakeRecord())
            plugin = FakePlugin(library, image_path)
            service = CommandService(plugin)
            service.send_path_for_record = lambda selected: image_path

            with self.assertRaises(RuntimeError):
                await service.random_image(FakeEvent(fail_on_send=1))

            self.assertEqual(library.recorded, [])


if __name__ == "__main__":
    unittest.main()
