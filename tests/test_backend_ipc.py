from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from meeting_survivor import backend as backend_module
from meeting_survivor.avatar import AvatarPreparationCancelled
from meeting_survivor.backend import BackendServer
from meeting_survivor.cli import build_parser, main


async def _read_json(reader: asyncio.StreamReader) -> dict:
    line = await asyncio.wait_for(reader.readline(), timeout=2)
    if not line:
        raise AssertionError("socket closed before response")
    return json.loads(line.decode("utf-8"))


async def _send_json(writer: asyncio.StreamWriter, payload: dict) -> None:
    writer.write(json.dumps(payload).encode("utf-8") + b"\n")
    await writer.drain()


async def _read_until_response(reader: asyncio.StreamReader, request_id: str) -> list[dict]:
    messages = []
    while not any(message.get("id") == request_id for message in messages):
        messages.append(await _read_json(reader))
    return messages


class BackendIPCSession:
    def __init__(self, *, camera_frame_dir: Path | None = None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.socket_path = self.root / "ipc" / "backend.sock"
        self.app_support = self.root / "app-support"
        self.camera_frame_dir = camera_frame_dir or (self.root / "group" / "CameraFrames")
        self.server = BackendServer(self.socket_path, self.app_support, self.camera_frame_dir)
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.server.start())
        for _ in range(100):
            if self.socket_path.exists():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("backend socket was not created")

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.server._stop_event.set()
            await asyncio.wait_for(self.task, timeout=2)
        self.tmp.cleanup()


class BackendIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = BackendIPCSession()
        await self.session.start()
        self.reader, self.writer = await asyncio.open_unix_connection(str(self.session.socket_path))

    async def asyncTearDown(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()
        await self.session.stop()

    async def test_handshake_accepts_protocol_version_one(self) -> None:
        await _send_json(self.writer, {"id": "1", "method": "handshake", "params": {"protocolVersion": 1}})
        response = await _read_json(self.reader)
        ready = await _read_json(self.reader)
        self.assertEqual(response["id"], "1")
        self.assertEqual(response["result"]["protocolVersion"], 1)
        self.assertTrue(response["result"]["ok"])
        self.assertEqual(ready["method"], "event")
        self.assertEqual(ready["params"]["type"], "backendReady")

    async def test_handshake_rejects_incompatible_protocol(self) -> None:
        await _send_json(self.writer, {"id": "1", "method": "handshake", "params": {"protocolVersion": 999}})
        response = await _read_json(self.reader)
        self.assertEqual(response["id"], "1")
        self.assertEqual(response["error"]["code"], "incompatibleProtocol")

    async def test_malformed_missing_fields_and_unknown_method_return_structured_errors(self) -> None:
        self.writer.write(b"not-json\n")
        await self.writer.drain()
        malformed = await _read_json(self.reader)
        self.assertEqual(malformed["error"]["code"], "parseError")
        self.assertNotIn("id", malformed)

        await _send_json(self.writer, {"method": "handshake"})
        missing_id = await _read_json(self.reader)
        self.assertEqual(missing_id["error"]["code"], "invalidRequest")
        self.assertNotIn("id", missing_id)

        await _send_json(self.writer, {"id": "missing-method"})
        missing_method = await _read_json(self.reader)
        self.assertEqual(missing_method["id"], "missing-method")
        self.assertEqual(missing_method["error"]["code"], "invalidRequest")

        await _send_json(self.writer, {"id": "bad-params", "method": "handshake", "params": []})
        bad_params = await _read_json(self.reader)
        self.assertEqual(bad_params["id"], "bad-params")
        self.assertEqual(bad_params["error"]["code"], "invalidRequest")

        await _send_json(self.writer, {"id": "2", "method": "missingMethod"})
        unknown = await _read_json(self.reader)
        self.assertEqual(unknown["id"], "2")
        self.assertEqual(unknown["error"]["code"], "methodNotFound")

    async def test_list_audio_devices_returns_structured_records(self) -> None:
        record = {
            "id": "0",
            "index": 0,
            "name": "Built-in Microphone",
            "hostApi": "Core Audio",
            "maxInputChannels": 1,
            "maxOutputChannels": 0,
            "defaultSampleRate": 48000.0,
            "isInput": True,
            "isOutput": False,
            "isDefaultInput": True,
            "isDefaultOutput": False,
        }
        with mock.patch.object(backend_module, "list_audio_devices_data", return_value=[record]):
            await _send_json(self.writer, {"id": "3", "method": "listAudioDevices"})
            response = await _read_json(self.reader)
        self.assertEqual(response["id"], "3")
        self.assertEqual(response["result"], {"devices": [record]})

    async def test_list_avatars_reads_app_support_metadata(self) -> None:
        avatar_dir = self.session.app_support / "avatars" / "me"
        avatar_dir.mkdir(parents=True)
        (avatar_dir / "metadata.json").write_text(
            json.dumps({"source_video": "/tmp/me.mov", "frame_count": 10, "width": 1280, "height": 720})
        )
        await _send_json(self.writer, {"id": "4", "method": "listAvatars"})
        response = await _read_json(self.reader)
        self.assertEqual(response["id"], "4")
        self.assertEqual(response["result"]["avatars"][0]["id"], "me")
        self.assertEqual(response["result"]["avatars"][0]["status"], "ready")
        self.assertEqual(response["result"]["avatars"][0]["frameCount"], 10)

    async def test_prepare_avatar_rejects_unsafe_avatar_name(self) -> None:
        source = self.session.root / "source.mov"
        source.write_bytes(b"fake")
        await _send_json(
            self.writer,
            {"id": "unsafe", "method": "prepareAvatar", "params": {"videoPath": str(source), "avatarName": "../bad"}},
        )
        response = await _read_json(self.reader)
        self.assertEqual(response["id"], "unsafe")
        self.assertEqual(response["error"]["code"], "invalidParams")

    async def test_prepare_avatar_emits_events_and_final_response(self) -> None:
        source = self.session.root / "source.mov"
        source.write_bytes(b"fake")

        def fake_prepare_avatar(**kwargs):
            kwargs["progress_callback"]("extractFrames", 1, 1)
            kwargs["avatar_dir"].mkdir(parents=True, exist_ok=True)
            (kwargs["avatar_dir"] / "metadata.json").write_text("{}")
            return kwargs["avatar_dir"]

        with mock.patch.object(backend_module, "prepare_avatar", side_effect=fake_prepare_avatar):
            await _send_json(
                self.writer,
                {
                    "id": "5",
                    "method": "prepareAvatar",
                    "params": {"videoPath": str(source), "avatarName": "me", "skipLatents": True},
                },
            )
            messages = [await _read_json(self.reader) for _ in range(4)]

        events = [m for m in messages if m.get("method") == "event"]
        response = next(m for m in messages if m.get("id") == "5")
        self.assertIn("prepareProgress", {e["params"]["type"] for e in events})
        self.assertIn("prepareCompleted", {e["params"]["type"] for e in events})
        self.assertEqual(response["result"]["avatarId"], "me")
        self.assertEqual(response["result"]["operationId"], events[0]["params"]["operationId"])

    async def test_prepare_avatar_failure_emits_event_and_structured_error(self) -> None:
        source = self.session.root / "source.mov"
        source.write_bytes(b"fake")

        with (
            mock.patch.object(backend_module, "prepare_avatar", side_effect=RuntimeError("boom")),
            mock.patch.object(backend_module.logging, "exception"),
        ):
            await _send_json(self.writer, {"id": "fail", "method": "prepareAvatar", "params": {"videoPath": str(source)}})
            first = await _read_json(self.reader)
            second = await _read_json(self.reader)
            third = await _read_json(self.reader)

        messages = [first, second, third]
        failed = next(m for m in messages if m.get("method") == "event" and m["params"]["type"] == "prepareFailed")
        response = next(m for m in messages if m.get("id") == "fail")
        self.assertEqual(failed["params"]["code"], "prepareFailed")
        self.assertEqual(response["error"]["code"], "internalError")

    async def test_cancel_operation_reports_operation_cancelled(self) -> None:
        source = self.session.root / "source.mov"
        source.write_bytes(b"fake")

        def fake_prepare_avatar(**kwargs):
            kwargs["progress_callback"]("extractFrames", 1, 2)
            while not kwargs["cancel_event"].is_set():
                time.sleep(0.01)
            raise AvatarPreparationCancelled("operation cancelled")

        with mock.patch.object(backend_module, "prepare_avatar", side_effect=fake_prepare_avatar):
            await _send_json(self.writer, {"id": "prepare", "method": "prepareAvatar", "params": {"videoPath": str(source)}})
            start_event = await _read_json(self.reader)
            operation_id = start_event["params"]["operationId"]
            await _send_json(self.writer, {"id": "cancel", "method": "cancelOperation", "params": {"operationId": operation_id}})
            messages = []
            while not any(m.get("id") == "prepare" for m in messages):
                messages.append(await _read_json(self.reader))

        cancel_response = next(m for m in messages if m.get("id") == "cancel")
        prepare_response = next(m for m in messages if m.get("id") == "prepare")
        failed = next(m for m in messages if m.get("method") == "event" and m["params"]["type"] == "prepareFailed")
        self.assertTrue(cancel_response["result"]["cancelled"])
        self.assertEqual(failed["params"]["code"], "operationCancelled")
        self.assertEqual(prepare_response["error"]["code"], "operationCancelled")

    async def test_get_session_state_returns_defaults(self) -> None:
        await _send_json(self.writer, {"id": "state", "method": "getSessionState"})
        response = await _read_json(self.reader)
        self.assertEqual(response["id"], "state")
        self.assertEqual(response["result"]["state"], "stopped")
        self.assertIsNone(response["result"]["activeAvatarId"])
        self.assertEqual(response["result"]["precision"], "fp16")
        self.assertEqual(response["result"]["audioDelayMs"], 400)

    async def test_start_stop_session_and_setters_emit_session_events(self) -> None:
        avatar_dir = self.session.app_support / "avatars" / "me"
        avatar_dir.mkdir(parents=True)
        (avatar_dir / "metadata.json").write_text("{}")

        await _send_json(self.writer, {"id": "avatar", "method": "setActiveAvatar", "params": {"avatarId": "me"}})
        avatar_messages = await _read_until_response(self.reader, "avatar")
        self.assertIn("sessionState", {m["params"]["type"] for m in avatar_messages if m.get("method") == "event"})
        self.assertEqual(avatar_messages[-1]["result"]["activeAvatarId"], "me")

        await _send_json(self.writer, {"id": "delay", "method": "setAudioDelay", "params": {"audioDelayMs": 450}})
        delay_messages = await _read_until_response(self.reader, "delay")
        self.assertEqual(delay_messages[-1]["result"]["audioDelayMs"], 450)

        def fake_run_camera(opts):
            while not opts.stop_event.is_set():
                time.sleep(0.01)
            return {"displayed": 0}

        with mock.patch.object(backend_module, "run_camera", side_effect=fake_run_camera):
            await _send_json(
                self.writer,
                {
                    "id": "start",
                    "method": "startSession",
                    "params": {"avatarId": "me", "inputDeviceId": "mic-1", "outputDeviceId": "out-1", "audioDelayMs": 500},
                },
            )
            start_messages = await _read_until_response(self.reader, "start")
            event_types = {m["params"]["type"] for m in start_messages if m.get("method") == "event"}
            start_response = start_messages[-1]
            self.assertIn("sessionState", event_types)
            self.assertIn("sessionStats", event_types)
            self.assertEqual(start_response["result"]["state"], "running")
            self.assertEqual(start_response["result"]["inputDeviceId"], "mic-1")
            self.assertEqual(start_response["result"]["outputDeviceId"], "out-1")
            self.assertEqual(start_response["result"]["audioDelayMs"], 500)
            self.assertIsInstance(start_response["result"]["startedAt"], float)

            await _send_json(self.writer, {"id": "stop", "method": "stopSession"})
            stop_messages = await _read_until_response(self.reader, "stop")
        self.assertEqual(stop_messages[-1]["result"]["state"], "stopped")
        self.assertIsNone(stop_messages[-1]["result"]["startedAt"])

    async def test_session_runtime_failure_reports_error_after_stopped_state(self) -> None:
        avatar_dir = self.session.app_support / "avatars" / "me"
        avatar_dir.mkdir(parents=True)
        (avatar_dir / "metadata.json").write_text("{}")

        def fake_run_camera(opts):
            raise ValueError("No input device matching '6'")

        with (
            mock.patch.object(backend_module, "run_camera", side_effect=fake_run_camera),
            mock.patch.object(backend_module.logging, "exception"),
        ):
            await _send_json(self.writer, {"id": "start", "method": "startSession", "params": {"avatarId": "me", "inputDeviceId": "6"}})
            messages = []
            while not any(m.get("method") == "event" and m["params"]["type"] == "error" for m in messages):
                messages.append(await _read_json(self.reader))

        stopped_index = next(
            index
            for index, message in enumerate(messages)
            if message.get("method") == "event" and message["params"]["type"] == "sessionState" and message["params"]["state"] == "stopped"
        )
        error_index = next(index for index, message in enumerate(messages) if message.get("method") == "event" and message["params"]["type"] == "error")
        self.assertLess(stopped_index, error_index)
        self.assertEqual(messages[error_index]["params"]["message"], "No input device matching '6'")
        self.assertEqual(self.session.server._session["state"], "stopped")
        self.assertIsNone(self.session.server._preview_sender_task)

    async def test_start_session_rejects_unprepared_avatar(self) -> None:
        await _send_json(self.writer, {"id": "start", "method": "startSession", "params": {"avatarId": "missing"}})
        response = await _read_json(self.reader)
        self.assertEqual(response["id"], "start")
        self.assertEqual(response["error"]["code"], "invalidParams")

    async def test_start_session_emits_preview_frame_path_and_stats(self) -> None:
        avatar_dir = self.session.app_support / "avatars" / "me"
        avatar_dir.mkdir(parents=True)
        (avatar_dir / "metadata.json").write_text("{}")

        def fake_run_camera(opts):
            frame = np.zeros((4, 6, 3), dtype=np.uint8)
            frame[:, :, 1] = 128
            opts.frame_callback(frame)
            opts.stats_callback({"previewFps": 25.0, "generatedFps": 12.5, "queueDepth": 1, "droppedJobs": 2, "renderMs": 33.0})
            while not opts.stop_event.is_set():
                time.sleep(0.01)
            return {"displayed": 1}

        with mock.patch.object(backend_module, "run_camera", side_effect=fake_run_camera):
            await _send_json(self.writer, {"id": "start", "method": "startSession", "params": {"avatarId": "me"}})
            messages = []
            while not (
                any(m.get("method") == "event" and m["params"]["type"] == "previewFrame" for m in messages)
                and any(
                    m.get("method") == "event"
                    and m["params"]["type"] == "sessionStats"
                    and m["params"]["previewFps"] == 25.0
                    for m in messages
                )
            ):
                messages.append(await _read_json(self.reader))
            await _send_json(self.writer, {"id": "stop", "method": "stopSession"})
            await _read_until_response(self.reader, "stop")

        frame_event = next(m for m in messages if m.get("method") == "event" and m["params"]["type"] == "previewFrame")
        stats_event = next(
            m for m in messages
            if m.get("method") == "event" and m["params"]["type"] == "sessionStats" and m["params"]["previewFps"] == 25.0
        )
        frame_path = Path(frame_event["params"]["previewPath"])
        self.assertTrue(frame_path.resolve().is_relative_to((self.session.app_support / "preview").resolve()))
        self.assertTrue(frame_path.exists())
        self.assertIsNotNone(cv2.imread(str(frame_path)))
        self.assertEqual(frame_event["params"]["previewSequence"], 1)
        self.assertEqual(frame_event["params"]["previewWidth"], 6)
        self.assertEqual(frame_event["params"]["previewHeight"], 4)
        self.assertEqual(stats_event["params"]["previewFps"], 25.0)
        self.assertEqual(stats_event["params"]["queueDepth"], 1)

    async def test_start_session_writes_camera_frame_feed_when_enabled(self) -> None:
        avatar_dir = self.session.app_support / "avatars" / "me"
        avatar_dir.mkdir(parents=True)
        (avatar_dir / "metadata.json").write_text("{}")

        def fake_run_camera(opts):
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            frame[:, :, 0] = 10
            frame[:, :, 1] = 20
            frame[:, :, 2] = 30
            opts.frame_callback(frame)
            while not opts.stop_event.is_set():
                time.sleep(0.01)
            return {"displayed": 1}

        with mock.patch.object(backend_module, "run_camera", side_effect=fake_run_camera):
            await _send_json(self.writer, {"id": "start", "method": "startSession", "params": {"avatarId": "me", "virtualCamera": True}})
            messages = await _read_until_response(self.reader, "start")
            self.assertEqual(messages[-1]["result"]["virtualCamera"], True)
            for _ in range(100):
                metadata_path = self.session.camera_frame_dir / "latest.json"
                if metadata_path.exists():
                    metadata = json.loads(metadata_path.read_text())
                    if metadata.get("state") == "running":
                        break
                await asyncio.sleep(0.01)
            else:
                self.fail("camera frame metadata was not written")

            frame_path = self.session.camera_frame_dir / metadata["frameFile"]
            self.assertTrue(frame_path.exists())
            self.assertEqual(metadata["width"], 1280)
            self.assertEqual(metadata["height"], 720)
            self.assertEqual(metadata["pixelFormat"], "bgra8")
            self.assertEqual(metadata["bytesPerRow"], 1280 * 4)
            self.assertEqual(frame_path.stat().st_size, 1280 * 720 * 4)
            self.assertEqual(frame_path.read_bytes()[:4], bytes([10, 20, 30, 255]))

            await _send_json(self.writer, {"id": "stop", "method": "stopSession"})
            await _read_until_response(self.reader, "stop")

        stopped_metadata = json.loads((self.session.camera_frame_dir / "latest.json").read_text())
        self.assertEqual(stopped_metadata["state"], "stopped")
        self.assertGreater(stopped_metadata["sequence"], metadata["sequence"])

    async def test_start_session_rejects_virtual_camera_without_frame_dir(self) -> None:
        server = BackendServer(self.session.root / "unused.sock", self.session.root / "unused-app-support")
        avatar_dir = server.avatars_dir / "me"
        avatar_dir.mkdir(parents=True)
        (avatar_dir / "metadata.json").write_text("{}")
        with self.assertRaises(backend_module.ProtocolError) as raised:
            await server._start_session(mock.Mock(), {"avatarId": "me", "virtualCamera": True})
        self.assertEqual(raised.exception.code, "cameraFrameFeedUnavailable")

    async def test_stop_session_signals_preview_worker(self) -> None:
        avatar_dir = self.session.app_support / "avatars" / "me"
        avatar_dir.mkdir(parents=True)
        (avatar_dir / "metadata.json").write_text("{}")
        stopped = threading.Event()

        def fake_run_camera(opts):
            while not opts.stop_event.is_set():
                time.sleep(0.01)
            stopped.set()
            return {"displayed": 0}

        with mock.patch.object(backend_module, "run_camera", side_effect=fake_run_camera):
            await _send_json(self.writer, {"id": "start", "method": "startSession", "params": {"avatarId": "me"}})
            await _read_until_response(self.reader, "start")
            await _send_json(self.writer, {"id": "stop", "method": "stopSession"})
            stop_messages = await _read_until_response(self.reader, "stop")

        self.assertTrue(stopped.is_set())
        self.assertEqual(stop_messages[-1]["result"]["state"], "stopped")

    async def test_generated_fps_must_be_positive(self) -> None:
        avatar_dir = self.session.app_support / "avatars" / "me"
        avatar_dir.mkdir(parents=True)
        (avatar_dir / "metadata.json").write_text("{}")
        await _send_json(self.writer, {"id": "start", "method": "startSession", "params": {"avatarId": "me", "generatedFps": 0.0}})
        response = await _read_json(self.reader)
        self.assertEqual(response["id"], "start")
        self.assertEqual(response["error"]["code"], "invalidParams")

    async def test_shutdown_closes_server(self) -> None:
        await _send_json(self.writer, {"id": "6", "method": "shutdown"})
        response = await _read_json(self.reader)
        self.assertEqual(response, {"id": "6", "result": {"ok": True}})
        await asyncio.wait_for(self.session.task, timeout=2)


class SocketPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_non_socket_path_is_not_unlinked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            socket_path = root / "backend.sock"
            socket_path.write_text("do not delete")
            server = BackendServer(socket_path, root / "app-support")
            with self.assertRaisesRegex(RuntimeError, "not a Unix socket"):
                await server.start()
            self.assertEqual(socket_path.read_text(), "do not delete")


class CLITests(unittest.TestCase):
    def test_backend_subcommand_requires_socket_and_app_support(self) -> None:
        args = build_parser().parse_args(["backend", "--socket", "/tmp/ms.sock", "--app-support", "/tmp/ms-app"])
        self.assertEqual(args.command, "backend")
        self.assertEqual(args.socket, Path("/tmp/ms.sock"))
        self.assertEqual(args.app_support, Path("/tmp/ms-app"))
        self.assertIsNone(args.camera_frame_dir)

    def test_list_devices_cli_still_prints_human_readable_output(self) -> None:
        stdout = io.StringIO()
        with mock.patch("meeting_survivor.cli.list_audio_devices", return_value="human devices"), contextlib.redirect_stdout(stdout):
            main(["list-devices"])
        self.assertEqual(stdout.getvalue(), "human devices\n")


if __name__ == "__main__":
    unittest.main()
