from __future__ import annotations

import asyncio
import contextlib
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
from .camera_transport import CameraFrameWriter
from .live import RunOptions, run_camera
from .models import MODEL_REPOS
from .protocol import PROTOCOL_VERSION, ProtocolError, error_response, event, parse_request, result_response


class BackendServer:
    def __init__(self, socket_path: Path, app_support: Path, camera_frame_dir: Path | None = None):
        self.socket_path = socket_path.expanduser().resolve()
        self.app_support = app_support.expanduser().resolve()
        self.camera_frame_dir = camera_frame_dir.expanduser().resolve() if camera_frame_dir is not None else None
        self.avatars_dir = self.app_support / "avatars"
        self.preview_dir = self.app_support / "preview"
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
        self._session_stop: threading.Event | None = None
        self._session_thread: threading.Thread | None = None
        self._session_generation = 0
        self._preview_sequence = 0
        self._preview_lock = threading.Lock()
        self._latest_preview_frame: dict[str, Any] | None = None
        self._preview_sender_task: asyncio.Task | None = None
        self._latest_session_stats = self._empty_session_stats()
        self._camera_frame_writer: CameraFrameWriter | None = None
        self._owns_socket = False

    async def start(self) -> None:
        self.app_support.mkdir(parents=True, exist_ok=True)
        self.avatars_dir.mkdir(parents=True, exist_ok=True)
        self.preview_dir.mkdir(parents=True, exist_ok=True)
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
            await self._stop_session_runtime()
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
        if virtual_camera and self.camera_frame_dir is None:
            raise ProtocolError("cameraFrameFeedUnavailable", "camera frame directory is not configured")

        await self._stop_session_runtime()
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
                "startedAt": time.time(),
            }
        )
        self._latest_session_stats = self._empty_session_stats()
        self._start_session_runtime(writer)
        state = self._session_state()
        await self._write_message(writer, event("sessionState", **state))
        await self._write_message(writer, event("sessionStats", **self._session_stats()))
        return state

    async def _stop_session(self, writer: asyncio.StreamWriter) -> dict[str, Any]:
        await self._stop_session_runtime()
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
        if self._session["state"] == "running":
            await self._stop_session_runtime()
            self._start_session_runtime(writer)
        state = self._session_state()
        await self._write_message(writer, event("sessionState", **state))
        return state

    async def _set_audio_delay(self, writer: asyncio.StreamWriter, params: dict[str, Any]) -> dict[str, Any]:
        if "audioDelayMs" not in params:
            raise ProtocolError("invalidParams", "audioDelayMs is required")
        self._session["audioDelayMs"] = self._int_param(params, "audioDelayMs", 0, minimum=0)
        if self._session["state"] == "running":
            await self._stop_session_runtime()
            self._start_session_runtime(writer)
        state = self._session_state()
        await self._write_message(writer, event("sessionState", **state))
        return state

    def _start_session_runtime(self, writer: asyncio.StreamWriter) -> None:
        active_avatar_id = self._session.get("activeAvatarId")
        if not isinstance(active_avatar_id, str):
            return
        generation = self._session_generation + 1
        self._session_generation = generation
        stop_event = threading.Event()
        self._session_stop = stop_event
        loop = asyncio.get_running_loop()
        self._preview_sender_task = asyncio.create_task(self._send_preview_frames(writer, generation, stop_event))
        camera_frame_writer = CameraFrameWriter(self.camera_frame_dir) if self._session.get("virtualCamera") and self.camera_frame_dir else None
        self._camera_frame_writer = camera_frame_writer
        if camera_frame_writer is not None:
            camera_frame_writer.clear()

        def frame_callback(frame: Any) -> None:
            if stop_event.is_set() or generation != self._session_generation:
                return
            if camera_frame_writer is not None:
                try:
                    camera_frame_writer.write_bgr(frame)
                except Exception:
                    logging.exception("camera frame feed write failed")
            encoded = self._encode_preview_frame(frame, generation)
            if encoded is not None:
                with self._preview_lock:
                    self._latest_preview_frame = encoded

        def stats_callback(stats: dict[str, Any]) -> None:
            if stop_event.is_set() or generation != self._session_generation:
                return
            self._latest_session_stats = {**self._empty_session_stats(), **stats, "state": self._session["state"]}
            asyncio.run_coroutine_threadsafe(
                self._write_message(writer, event("sessionStats", **self._latest_session_stats)),
                loop,
            )

        def worker() -> None:
            try:
                run_camera(
                    RunOptions(
                        avatar_dir=self.avatars_dir / active_avatar_id,
                        precision=self._session["precision"],
                        input_device=self._session.get("inputDeviceId"),
                        output_device=self._session.get("outputDeviceId") if self._session.get("virtualMicrophone") else None,
                        delay_ms=self._session["audioDelayMs"],
                        generated_fps=self._session["generatedFps"],
                        no_preview=True,
                        stop_event=stop_event,
                        frame_callback=frame_callback,
                        stats_callback=stats_callback,
                    )
                )
            except Exception as exc:
                logging.exception("session runtime failed")
                asyncio.run_coroutine_threadsafe(self._session_runtime_failed(writer, generation, exc), loop)

        self._session_thread = threading.Thread(target=worker, daemon=True, name="MeetingSurvivorSession")
        self._session_thread.start()

    async def _stop_session_runtime(self) -> None:
        if self._session_stop is not None:
            self._session_stop.set()
            self._session_stop = None
        if self._preview_sender_task is not None:
            self._preview_sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._preview_sender_task
            self._preview_sender_task = None
        if self._session_thread is not None and self._session_thread.is_alive():
            await asyncio.to_thread(self._session_thread.join, 2.0)
        self._session_thread = None
        if self._camera_frame_writer is not None:
            await asyncio.to_thread(self._camera_frame_writer.clear)
            self._camera_frame_writer = None
        with self._preview_lock:
            self._latest_preview_frame = None

    async def _session_runtime_failed(self, writer: asyncio.StreamWriter, generation: int, exc: Exception) -> None:
        if generation != self._session_generation or self._session["state"] != "running":
            return
        self._session["state"] = "stopped"
        self._session["startedAt"] = None
        if self._session_stop is not None:
            self._session_stop.set()
            self._session_stop = None
        if self._preview_sender_task is not None:
            self._preview_sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._preview_sender_task
            self._preview_sender_task = None
        if self._camera_frame_writer is not None:
            await asyncio.to_thread(self._camera_frame_writer.clear)
            self._camera_frame_writer = None
        await self._write_message(writer, event("sessionState", **self._session_state()))
        await self._write_message(writer, event("sessionStats", **self._session_stats()))
        await self._write_message(writer, event("error", severity="recoverable", message=str(exc) or exc.__class__.__name__))

    async def _send_preview_frames(self, writer: asyncio.StreamWriter, generation: int, stop_event: threading.Event) -> None:
        sent_sequence = 0
        frame_period = 1.0 / 25.0
        while not stop_event.is_set() and generation == self._session_generation:
            await asyncio.sleep(frame_period)
            with self._preview_lock:
                frame = self._latest_preview_frame
            if frame is None or frame["previewGeneration"] != generation or frame["previewSequence"] == sent_sequence:
                continue
            sent_sequence = frame["previewSequence"]
            await self._write_message(writer, event("previewFrame", **frame))

    def _encode_preview_frame(self, frame: Any, generation: int) -> dict[str, Any] | None:
        try:
            import cv2

            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if not ok:
                return None
            self.preview_dir.mkdir(parents=True, exist_ok=True)
            self._preview_sequence += 1
            sequence = self._preview_sequence
            path = self.preview_dir / f"preview-{sequence % 3}.jpg"
            tmp_path = path.with_suffix(".tmp.jpg")
            tmp_path.write_bytes(encoded.tobytes())
            tmp_path.replace(path)
            return {
                "previewSequence": sequence,
                "previewGeneration": generation,
                "previewFormat": "jpeg",
                "previewWidth": int(frame.shape[1]),
                "previewHeight": int(frame.shape[0]),
                "previewTimestamp": time.time(),
                "previewPath": str(path),
            }
        except Exception:
            logging.exception("preview frame encode failed")
            return None

    def _session_state(self) -> dict[str, Any]:
        return dict(self._session)

    def _session_stats(self) -> dict[str, Any]:
        return {**self._latest_session_stats, "state": self._session["state"]}

    def _empty_session_stats(self) -> dict[str, Any]:
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


async def run_backend_async(socket_path: Path, app_support: Path, camera_frame_dir: Path | None = None) -> None:
    server = BackendServer(socket_path, app_support, camera_frame_dir)
    await server.start()


def run_backend(socket_path: Path, app_support: Path, camera_frame_dir: Path | None = None) -> None:
    asyncio.run(run_backend_async(socket_path, app_support, camera_frame_dir))
