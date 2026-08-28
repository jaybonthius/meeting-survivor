from __future__ import annotations

import asyncio
import json
import logging
import stat
import threading
import time
from itertools import count
from pathlib import Path
from typing import Any

from .audio import list_audio_devices_data
from .avatar import AvatarPreparationCancelled, prepare_avatar
from .models import MODEL_REPOS
from .protocol import PROTOCOL_VERSION, ProtocolError, error_response, event, parse_request, result_response


class BackendServer:
    def __init__(self, socket_path: Path, app_support: Path):
        self.socket_path = socket_path.expanduser().resolve()
        self.app_support = app_support.expanduser().resolve()
        self.avatars_dir = self.app_support / "avatars"
        self._server: asyncio.AbstractServer | None = None
        self._stop_event = asyncio.Event()
        self._writer_lock = asyncio.Lock()
        self._operation_counter = count(1)
        self._operations: dict[str, threading.Event] = {}
        self._session: dict[str, Any] = {
            "state": "stopped",
            "activeAvatarId": None,
            "inputDeviceId": None,
            "outputDeviceId": None,
            "virtualCamera": False,
            "virtualMicrophone": False,
            "precision": "q8",
            "generatedFps": 12.5,
            "audioDelayMs": 400,
            "startedAt": None,
        }
        self._owns_socket = False

    async def start(self) -> None:
        self.app_support.mkdir(parents=True, exist_ok=True)
        self.avatars_dir.mkdir(parents=True, exist_ok=True)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            if not stat.S_ISSOCK(self.socket_path.lstat().st_mode):
                raise RuntimeError(f"socket path exists and is not a Unix socket: {self.socket_path}")
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
        self._owns_socket = True
        await self._send_backend_ready()
        async with self._server:
            await self._stop_event.wait()
            self._server.close()
            await self._server.wait_closed()
        if self._owns_socket and self.socket_path.exists() and stat.S_ISSOCK(self.socket_path.lstat().st_mode):
            self.socket_path.unlink()

    async def _send_backend_ready(self) -> None:
        logging.info("backend ready socket=%s app_support=%s", self.socket_path, self.app_support)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks: set[asyncio.Task] = set()
        try:
            while not reader.at_eof() and not self._stop_event.is_set():
                line = await reader.readline()
                if not line:
                    break
                try:
                    raw = json.loads(line.decode("utf-8"))
                    request = parse_request(raw)
                except json.JSONDecodeError as exc:
                    await self._write_message(writer, error_response(None, ProtocolError("parseError", f"invalid JSON: {exc.msg}")))
                    continue
                except ProtocolError as exc:
                    request_id = raw.get("id") if isinstance(raw, dict) and isinstance(raw.get("id"), str) else None
                    await self._write_message(writer, error_response(request_id, exc))
                    continue
                task = asyncio.create_task(self._dispatch_request(writer, request.id, request.method, request.params))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _dispatch_request(self, writer: asyncio.StreamWriter, request_id: str, method: str, params: dict[str, Any]) -> None:
        try:
            result = await self._handle_method(writer, method, params)
            await self._write_message(writer, result_response(request_id, result))
            if method == "handshake":
                await self._write_message(
                    writer,
                    event("backendReady", protocolVersion=PROTOCOL_VERSION, appSupport=str(self.app_support)),
                )
            if method == "shutdown":
                writer.close()
        except Exception as exc:
            if not isinstance(exc, ProtocolError):
                logging.exception("request failed method=%s", method)
            await self._write_message(writer, error_response(request_id, exc))

    async def _handle_method(self, writer: asyncio.StreamWriter, method: str, params: dict[str, Any]) -> Any:
        if method == "handshake":
            return self._handshake(params)
        if method == "listAudioDevices":
            return {"devices": list_audio_devices_data()}
        if method == "listAvatars":
            return {"avatars": self._list_avatars()}
        if method == "prepareAvatar":
            return await self._prepare_avatar(writer, params)
        if method == "cancelOperation":
            return self._cancel_operation(params)
        if method == "startSession":
            return await self._start_session(writer, params)
        if method == "stopSession":
            return await self._stop_session(writer)
        if method == "getSessionState":
            return self._session_state()
        if method == "setActiveAvatar":
            return await self._set_active_avatar(writer, params)
        if method == "setAudioDelay":
            return await self._set_audio_delay(writer, params)
        if method == "shutdown":
            self._cancel_all_operations()
            self._stop_event.set()
            return {"ok": True}
        raise ProtocolError("methodNotFound", f"unknown method: {method}")

    def _handshake(self, params: dict[str, Any]) -> dict[str, Any]:
        version = params.get("protocolVersion")
        if version != PROTOCOL_VERSION:
            raise ProtocolError("incompatibleProtocol", f"protocolVersion {version!r} is not supported; expected {PROTOCOL_VERSION}")
        return {"protocolVersion": PROTOCOL_VERSION, "backend": "meeting-survivor", "ok": True}

    def _list_avatars(self) -> list[dict[str, Any]]:
        avatars: list[dict[str, Any]] = []
        if not self.avatars_dir.exists():
            return avatars
        for path in sorted(p for p in self.avatars_dir.iterdir() if p.is_dir()):
            metadata_path = path / "metadata.json"
            metadata: dict[str, Any] = {}
            status = "notPrepared"
            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text())
                    status = "ready"
                except json.JSONDecodeError:
                    status = "failed"
            avatars.append(
                {
                    "id": path.name,
                    "name": metadata.get("name") or path.name,
                    "path": str(path),
                    "status": status,
                    "sourceVideo": metadata.get("source_video"),
                    "createdAt": metadata.get("created_at"),
                    "frameCount": metadata.get("frame_count"),
                    "width": metadata.get("width"),
                    "height": metadata.get("height"),
                    "sourceFps": metadata.get("source_fps"),
                }
            )
        return avatars

    async def _prepare_avatar(self, writer: asyncio.StreamWriter, params: dict[str, Any]) -> dict[str, Any]:
        video_path = self._required_path(params, "videoPath")
        avatar_name = self._avatar_name(params.get("avatarName") or video_path.stem.replace(" ", "_"))
        precision = self._choice_param(params, "precision", set(MODEL_REPOS), "q8")
        download_model = self._bool_param(params, "downloadModel", False)
        skip_latents = self._bool_param(params, "skipLatents", False)
        max_seconds = self._optional_float_param(params, "maxSeconds", 10.0, minimum=0.0)
        bbox_shift = self._int_param(params, "bboxShift", 0)
        extra_margin = self._int_param(params, "extraMargin", 10, minimum=0)
        parsing_mode = self._choice_param(params, "parsingMode", {"jaw", "raw", "neck"}, "jaw")
        left_cheek_width = self._int_param(params, "leftCheekWidth", 90, minimum=1)
        right_cheek_width = self._int_param(params, "rightCheekWidth", 90, minimum=1)
        operation_id = f"op-{next(self._operation_counter)}"
        cancel_event = threading.Event()
        self._operations[operation_id] = cancel_event
        loop = asyncio.get_running_loop()

        def progress(stage: str, current: int, total: int) -> None:
            asyncio.run_coroutine_threadsafe(
                self._write_message(
                    writer,
                    event("prepareProgress", operationId=operation_id, stage=stage, current=current, total=total),
                ),
                loop,
            )

        avatar_dir = self.avatars_dir / avatar_name
        await self._write_message(writer, event("prepareProgress", operationId=operation_id, stage="starting", current=0, total=1))
        try:
            result_path = await asyncio.to_thread(
                prepare_avatar,
                video_path=video_path,
                avatar_dir=avatar_dir,
                precision=precision,
                weights_dir=self._optional_path(params, "weightsDir"),
                download_model=download_model,
                skip_latents=skip_latents,
                max_seconds=max_seconds,
                bbox_shift=bbox_shift,
                extra_margin=extra_margin,
                parsing_mode=parsing_mode,
                left_cheek_width=left_cheek_width,
                right_cheek_width=right_cheek_width,
                progress_callback=progress,
                cancel_event=cancel_event,
                show_progress=False,
            )
        except AvatarPreparationCancelled as exc:
            await self._write_message(writer, event("prepareFailed", operationId=operation_id, code="operationCancelled", message=str(exc)))
            raise ProtocolError("operationCancelled", str(exc)) from exc
        except Exception as exc:
            await self._write_message(writer, event("prepareFailed", operationId=operation_id, code="prepareFailed", message=str(exc)))
            raise
        finally:
            self._operations.pop(operation_id, None)
        result = {"operationId": operation_id, "avatarId": avatar_name, "avatarPath": str(result_path)}
        await self._write_message(writer, event("prepareCompleted", **result))
        return result

    async def _start_session(self, writer: asyncio.StreamWriter, params: dict[str, Any]) -> dict[str, Any]:
        avatar_id = params.get("avatarId") or self._session.get("activeAvatarId")
        if not isinstance(avatar_id, str) or not avatar_id:
            raise ProtocolError("invalidParams", "avatarId must be a prepared avatar id")
        avatar_id = self._require_ready_avatar(avatar_id)
        input_device_id = self._optional_string_param(params, "inputDeviceId")
        output_device_id = self._optional_string_param(params, "outputDeviceId")
        precision = self._choice_param(params, "precision", set(MODEL_REPOS), self._session["precision"])
        generated_fps = self._optional_float_param(params, "generatedFps", self._session["generatedFps"], minimum=0.0)
        audio_delay_ms = self._int_param(params, "audioDelayMs", self._session["audioDelayMs"], minimum=0)
        virtual_camera = self._bool_param(params, "virtualCamera", False)
        virtual_microphone = self._bool_param(params, "virtualMicrophone", False)

        self._session.update(
            {
                "state": "running",
                "activeAvatarId": avatar_id,
                "inputDeviceId": input_device_id,
                "outputDeviceId": output_device_id,
                "virtualCamera": virtual_camera,
                "virtualMicrophone": virtual_microphone,
                "precision": precision,
                "generatedFps": generated_fps,
                "audioDelayMs": audio_delay_ms,
                "startedAt": self._session.get("startedAt") or time.time(),
            }
        )
        state = self._session_state()
        await self._write_message(writer, event("sessionState", **state))
        await self._write_message(writer, event("sessionStats", **self._session_stats()))
        return state

    async def _stop_session(self, writer: asyncio.StreamWriter) -> dict[str, Any]:
        self._session["state"] = "stopped"
        self._session["startedAt"] = None
        state = self._session_state()
        await self._write_message(writer, event("sessionState", **state))
        await self._write_message(writer, event("sessionStats", **self._session_stats()))
        return state

    async def _set_active_avatar(self, writer: asyncio.StreamWriter, params: dict[str, Any]) -> dict[str, Any]:
        avatar_id = params.get("avatarId")
        if not isinstance(avatar_id, str) or not avatar_id:
            raise ProtocolError("invalidParams", "avatarId must be a prepared avatar id")
        self._session["activeAvatarId"] = self._require_ready_avatar(avatar_id)
        state = self._session_state()
        await self._write_message(writer, event("sessionState", **state))
        return state

    async def _set_audio_delay(self, writer: asyncio.StreamWriter, params: dict[str, Any]) -> dict[str, Any]:
        if "audioDelayMs" not in params:
            raise ProtocolError("invalidParams", "audioDelayMs is required")
        self._session["audioDelayMs"] = self._int_param(params, "audioDelayMs", 0, minimum=0)
        state = self._session_state()
        await self._write_message(writer, event("sessionState", **state))
        return state

    def _session_state(self) -> dict[str, Any]:
        return dict(self._session)

    def _session_stats(self) -> dict[str, Any]:
        return {
            "state": self._session["state"],
            "previewFps": 0.0,
            "generatedFps": 0.0,
            "queueDepth": 0,
            "droppedJobs": 0,
            "renderMs": 0.0,
        }

    def _require_ready_avatar(self, value: Any) -> str:
        avatar_id = self._avatar_name(value)
        matches = [avatar for avatar in self._list_avatars() if avatar["id"] == avatar_id]
        if not matches or matches[0]["status"] != "ready":
            raise ProtocolError("invalidParams", "avatarId must reference a prepared avatar")
        return avatar_id

    def _cancel_operation(self, params: dict[str, Any]) -> dict[str, Any]:
        operation_id = params.get("operationId")
        if not isinstance(operation_id, str) or not operation_id:
            raise ProtocolError("invalidParams", "operationId must be a non-empty string")
        cancel_event = self._operations.get(operation_id)
        if cancel_event is None:
            return {"operationId": operation_id, "cancelled": False, "reason": "notFound"}
        cancel_event.set()
        return {"operationId": operation_id, "cancelled": True}

    def _cancel_all_operations(self) -> None:
        for cancel_event in list(self._operations.values()):
            cancel_event.set()

    def _avatar_name(self, value: Any) -> str:
        if not isinstance(value, str) or not value:
            raise ProtocolError("invalidParams", "avatarName must be a non-empty string when provided")
        path = Path(value)
        if path.is_absolute() or path.name != value or value in {".", ".."}:
            raise ProtocolError("invalidParams", "avatarName must be a simple directory name")
        return value

    def _bool_param(self, params: dict[str, Any], name: str, default: bool) -> bool:
        value = params.get(name, default)
        if not isinstance(value, bool):
            raise ProtocolError("invalidParams", f"{name} must be a boolean")
        return value

    def _int_param(self, params: dict[str, Any], name: str, default: int, minimum: int | None = None) -> int:
        value = params.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ProtocolError("invalidParams", f"{name} must be an integer")
        if minimum is not None and value < minimum:
            raise ProtocolError("invalidParams", f"{name} must be >= {minimum}")
        return value

    def _optional_float_param(self, params: dict[str, Any], name: str, default: float | None, minimum: float | None = None) -> float | None:
        value = params.get(name, default)
        if value is None:
            return None
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ProtocolError("invalidParams", f"{name} must be a number")
        value = float(value)
        if minimum is not None and value <= minimum:
            raise ProtocolError("invalidParams", f"{name} must be > {minimum}")
        return value

    def _choice_param(self, params: dict[str, Any], name: str, choices: set[str], default: str) -> str:
        value = params.get(name, default)
        if not isinstance(value, str) or value not in choices:
            raise ProtocolError("invalidParams", f"{name} must be one of: {', '.join(sorted(choices))}")
        return value

    def _optional_string_param(self, params: dict[str, Any], name: str) -> str | None:
        value = params.get(name)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ProtocolError("invalidParams", f"{name} must be a non-empty string when provided")
        return value

    def _required_path(self, params: dict[str, Any], name: str) -> Path:
        raw = params.get(name)
        if not isinstance(raw, str) or not raw:
            raise ProtocolError("invalidParams", f"{name} must be a non-empty string")
        return Path(raw).expanduser().resolve()

    def _optional_path(self, params: dict[str, Any], name: str) -> Path | None:
        raw = params.get(name)
        if raw is None:
            return None
        if not isinstance(raw, str) or not raw:
            raise ProtocolError("invalidParams", f"{name} must be a non-empty string when provided")
        return Path(raw).expanduser().resolve()

    async def _write_message(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
        async with self._writer_lock:
            writer.write(payload)
            await writer.drain()


async def run_backend_async(socket_path: Path, app_support: Path) -> None:
    server = BackendServer(socket_path, app_support)
    await server.start()


def run_backend(socket_path: Path, app_support: Path) -> None:
    asyncio.run(run_backend_async(socket_path, app_support))
