from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

import cv2
import mlx.core as mx
import numpy as np
import soundfile as sf

from .audio import LiveAudio, read_wav_16k, write_delayed_wav
from .avatar import composite_face, crop_resize, load_avatar
from .models import ensure_weights, load_pipeline


@dataclass(frozen=True)
class LiveSessionConfig:
    avatar_dir: Path
    precision: str = "q8"
    delay_ms: int = 400
    generated_fps: float = 25.0
    vad_threshold: float = 0.012
    audio_window_seconds: float = 1.2

    @property
    def asset_key(self) -> tuple[Path, str]:
        return self.avatar_dir, self.precision


@dataclass(frozen=True)
class LiveSessionSnapshot:
    version: int
    config: LiveSessionConfig


class LiveSessionControl:
    def __init__(self, config: LiveSessionConfig):
        self._lock = threading.Lock()
        self._version = 0
        self._config = config

    def snapshot(self) -> LiveSessionSnapshot:
        with self._lock:
            return LiveSessionSnapshot(self._version, self._config)

    def update(
        self,
        *,
        avatar_dir: Path | None = None,
        precision: str | None = None,
        delay_ms: int | None = None,
        generated_fps: float | None = None,
        vad_threshold: float | None = None,
        audio_window_seconds: float | None = None,
    ) -> LiveSessionSnapshot:
        with self._lock:
            config = replace(
                self._config,
                avatar_dir=avatar_dir if avatar_dir is not None else self._config.avatar_dir,
                precision=precision if precision is not None else self._config.precision,
                delay_ms=delay_ms if delay_ms is not None else self._config.delay_ms,
                generated_fps=generated_fps if generated_fps is not None else self._config.generated_fps,
                vad_threshold=vad_threshold if vad_threshold is not None else self._config.vad_threshold,
                audio_window_seconds=audio_window_seconds if audio_window_seconds is not None else self._config.audio_window_seconds,
            )
            if config != self._config:
                self._version += 1
                self._config = config
            return LiveSessionSnapshot(self._version, self._config)


@dataclass
class RunOptions:
    avatar_dir: Path
    precision: str = "q8"
    weights_dir: Path | None = None
    input_device: str | int | None = None
    output_device: str | int | None = None
    delay_ms: int = 400
    generated_fps: float = 25.0
    vad_threshold: float = 0.012
    audio_window_seconds: float = 1.2
    wav_input: Path | None = None
    output_video: Path | None = None
    output_wav: Path | None = None
    duration_seconds: float | None = None
    no_preview: bool = False
    stop_event: threading.Event | None = None
    frame_callback: Callable[[np.ndarray], None] | None = None
    stats_callback: Callable[[dict], None] | None = None
    control: LiveSessionControl | None = None
    control_callback: Callable[[dict], None] | None = None


@dataclass
class LiveAssets:
    version: int
    config: LiveSessionConfig
    meta: dict
    frames: list[np.ndarray]
    crops: list[np.ndarray]
    latents: np.ndarray | None
    masks: list[np.ndarray | None]
    pipe: object

    @property
    def asset_key(self) -> tuple[Path, str]:
        return self.config.asset_key


class SpeechGate:
    def __init__(self, threshold: float, attack: int = 2, release: int = 8):
        self.threshold = threshold
        self.attack = attack
        self.release = release
        self.hot = 0
        self.cold = 0
        self.speaking = False

    def update(self, rms: float) -> bool:
        if rms >= self.threshold:
            self.hot += 1
            self.cold = 0
        else:
            self.cold += 1
            self.hot = 0
        if self.hot >= self.attack:
            self.speaking = True
        if self.cold >= self.release:
            self.speaking = False
        return self.speaking


def _audio_chunk_from_pcm16k(pipe, pcm16: np.ndarray, fps: int = 25, frames_from_end: int = 0):
    from musetalk_mlx.whisper.audio2feature import get_whisper_chunk
    from musetalk_mlx.whisper.log_mel import log_mel_spectrogram

    if len(pcm16) < int(0.25 * 16000):
        pcm16 = np.pad(pcm16, (int(0.25 * 16000) - len(pcm16), 0))
    mel = log_mel_spectrogram(mx.array(pcm16.astype(np.float32)))
    stacked = pipe.whisper_encoder(mel)
    chunks = get_whisper_chunk(stacked, len(pcm16), fps=fps)
    idx = max(0, chunks.shape[0] - 1 - frames_from_end)
    return chunks[idx:idx + 1]


def _rms(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples))))


def _load_or_make_latent(pipe, latents, crops, idx: int):
    if latents is not None:
        return mx.array(np.asarray(latents[idx:idx + 1]))
    return pipe.get_latents_for_unet(crops[idx])


def _load_live_assets(snapshot: LiveSessionSnapshot, weights_dir: Path | None, pipe: object | None = None) -> LiveAssets:
    meta, frames, crops, latents, masks = load_avatar(snapshot.config.avatar_dir)
    if pipe is None:
        weights = ensure_weights(snapshot.config.precision, weights_dir, allow_download=False)
        pipe = load_pipeline(weights)
    return LiveAssets(snapshot.version, snapshot.config, meta, frames, crops, latents, masks, pipe)


def _emit_control_status(callback: Callable[[dict], None] | None, status: str, snapshot: LiveSessionSnapshot, message: str | None = None) -> None:
    if callback is None:
        return
    payload = {
        "status": status,
        "version": snapshot.version,
        "avatarId": snapshot.config.avatar_dir.name,
        "precision": snapshot.config.precision,
        "generatedFps": snapshot.config.generated_fps,
        "audioDelayMs": snapshot.config.delay_ms,
        "vadThreshold": snapshot.config.vad_threshold,
        "audioWindowSeconds": snapshot.config.audio_window_seconds,
    }
    if message:
        payload["message"] = message
    try:
        callback(payload)
    except Exception:
        logging.exception("session control callback failed")


def _generation_stride(generated_fps: float) -> int:
    return 2 if generated_fps <= 12.5 else 1


def run_camera(opts: RunOptions) -> dict:
    initial_config = LiveSessionConfig(
        avatar_dir=opts.avatar_dir,
        precision=opts.precision,
        delay_ms=opts.delay_ms,
        generated_fps=opts.generated_fps,
        vad_threshold=opts.vad_threshold,
        audio_window_seconds=opts.audio_window_seconds,
    )
    control = opts.control or LiveSessionControl(initial_config)
    assets = _load_live_assets(control.snapshot(), opts.weights_dir)

    stop = opts.stop_event or threading.Event()
    if opts.stop_event is None and threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    frame_period = 1.0 / 25.0
    jobs: queue.Queue = queue.Queue(maxsize=1)
    results: queue.Queue = queue.Queue(maxsize=4)
    asset_loads: queue.Queue = queue.Queue()
    loading_snapshot: LiveSessionSnapshot | None = None
    stats = {"displayed": 0, "generated": 0, "missed": 0, "dropped_jobs": 0, "max_queue": 0}

    def worker():
        mx.set_default_device(mx.gpu)
        mx.set_default_stream(mx.new_stream(mx.gpu))
        while not stop.is_set():
            try:
                job = jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            frame_idx, audio_payload, preencoded, job_assets, delay_ms = job
            started = time.monotonic()
            try:
                dtype = getattr(job_assets.pipe, "_dtype", mx.float32)
                latent = _load_or_make_latent(job_assets.pipe, job_assets.latents, job_assets.crops, frame_idx).astype(dtype)
                if preencoded:
                    chunk = mx.array(audio_payload).astype(dtype)
                else:
                    frames_from_end = max(0, int(round(delay_ms / 1000.0 * 25)))
                    chunk = _audio_chunk_from_pcm16k(job_assets.pipe, audio_payload, frames_from_end=frames_from_end).astype(dtype)
                face = job_assets.pipe.generate_faces(latent, chunk)[0]
                try:
                    results.put_nowait((frame_idx, face, time.monotonic() - started, job_assets.version))
                except queue.Full:
                    pass
            except Exception:
                logging.exception("inference failed")
            finally:
                jobs.task_done()

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()

    audio = None
    wav16 = None
    wav_rate = 16000
    wav_chunks = None
    if opts.wav_input:
        wav16, wav_rate = read_wav_16k(opts.wav_input)
        wav_chunks = np.array(assets.pipe.encode_audio_from_wav(opts.wav_input, fps=25))
        logging.info("precomputed %s official MuseTalk audio chunks", wav_chunks.shape[0])
        if opts.output_wav:
            write_delayed_wav(opts.wav_input, opts.output_wav, initial_config.delay_ms)
    else:
        audio = LiveAudio(opts.input_device, opts.output_device, initial_config.delay_ms)
        audio.start()
        logging.info("audio input_rate=%s output_rate=%s delay_ms=%s", audio.input_rate, audio.output_rate, initial_config.delay_ms)

    writer = None
    if opts.output_video:
        opts.output_video.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(opts.output_video),
            cv2.VideoWriter_fourcc(*"mp4v"),
            25.0,
            (int(assets.meta.get("width", 1280)), int(assets.meta.get("height", 720))),
        )

    if not opts.no_preview:
        cv2.namedWindow("meeting-survivor", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("meeting-survivor", int(assets.meta.get("width", 1280)), int(assets.meta.get("height", 720)))

    def start_asset_load(snapshot: LiveSessionSnapshot) -> None:
        nonlocal loading_snapshot
        if loading_snapshot is not None:
            return
        loading_snapshot = snapshot
        logging.info("loading live assets avatar=%s precision=%s version=%s", snapshot.config.avatar_dir, snapshot.config.precision, snapshot.version)
        _emit_control_status(opts.control_callback, "loading", snapshot, f"Loading {snapshot.config.avatar_dir.name} ({snapshot.config.precision})")

        reusable_pipe = assets.pipe if snapshot.config.precision == assets.config.precision else None

        def loader() -> None:
            try:
                asset_loads.put((snapshot, _load_live_assets(snapshot, opts.weights_dir, reusable_pipe), None))
            except Exception as exc:
                asset_loads.put((snapshot, None, exc))

        threading.Thread(target=loader, daemon=True, name="MeetingSurvivorAssetLoader").start()

    gate = SpeechGate(initial_config.vad_threshold)
    latest_face = None
    latest_render_time = 0.0
    started_at = time.monotonic()
    next_tick = started_at
    last_log = started_at

    try:
        while not stop.is_set():
            while True:
                try:
                    loaded_snapshot, loaded_assets, load_error = asset_loads.get_nowait()
                except queue.Empty:
                    break
                if loading_snapshot and loaded_snapshot.version == loading_snapshot.version:
                    loading_snapshot = None
                desired = control.snapshot()
                if load_error is not None:
                    message = str(load_error) or load_error.__class__.__name__
                    logging.error("live asset load failed version=%s: %s", loaded_snapshot.version, message)
                    if desired.config.asset_key == loaded_snapshot.config.asset_key:
                        control.update(avatar_dir=assets.config.avatar_dir, precision=assets.config.precision)
                        _emit_control_status(opts.control_callback, "failed", loaded_snapshot, message)
                    continue
                if loaded_assets is not None and desired.config.asset_key == loaded_assets.asset_key:
                    loaded_assets.version = desired.version
                    loaded_assets.config = desired.config
                    assets = loaded_assets
                    latest_face = None
                    _emit_control_status(opts.control_callback, "applied", desired, f"Applied {desired.config.avatar_dir.name} ({desired.config.precision})")
                    logging.info("applied live assets avatar=%s precision=%s version=%s", assets.config.avatar_dir, assets.config.precision, assets.version)

            snapshot = control.snapshot()
            active_config = snapshot.config
            gate.threshold = active_config.vad_threshold
            if audio is not None and audio.delay_ms != active_config.delay_ms:
                audio.set_delay_ms(active_config.delay_ms)
            if active_config.asset_key != assets.asset_key and loading_snapshot is None:
                start_asset_load(snapshot)

            now = time.monotonic()
            if now < next_tick:
                time.sleep(min(0.005, next_tick - now))
                continue
            elapsed = now - started_at
            if opts.duration_seconds and elapsed >= opts.duration_seconds:
                break

            source_idx = stats["displayed"] % len(assets.frames)
            frame = assets.frames[source_idx]

            if wav16 is not None:
                pos = int(elapsed * wav_rate)
                start = max(0, pos - int(active_config.audio_window_seconds * wav_rate))
                audio16 = wav16[start:pos]
                rms = _rms(wav16[max(0, pos - int(0.08 * wav_rate)):pos])
            else:
                audio16 = audio.recent_16k(active_config.audio_window_seconds) if audio else np.zeros(0, dtype=np.float32)
                rms = audio.rms() if audio else 0.0

            speaking = gate.update(rms)
            if not speaking:
                latest_face = None
            while True:
                try:
                    result_idx, face, render_time, result_version = results.get_nowait()
                    if result_version == assets.version:
                        if speaking:
                            latest_face = face
                        latest_render_time = render_time
                        stats["generated"] += 1
                except queue.Empty:
                    break

            should_generate = speaking and (stats["displayed"] % _generation_stride(active_config.generated_fps) == 0)
            if should_generate:
                try:
                    if wav_chunks is not None:
                        chunk_idx = min(stats["displayed"], wav_chunks.shape[0] - 1)
                        jobs.put_nowait((source_idx, wav_chunks[chunk_idx:chunk_idx + 1], True, assets, active_config.delay_ms))
                    else:
                        jobs.put_nowait((source_idx, audio16.copy(), False, assets, active_config.delay_ms))
                except queue.Full:
                    stats["dropped_jobs"] += 1

            if speaking and latest_face is not None:
                out = composite_face(frame, latest_face, assets.meta["boxes"][source_idx], assets.masks[source_idx], assets.meta["mask_boxes"][source_idx])
            else:
                out = frame

            if writer:
                writer.write(out)
            if opts.frame_callback:
                try:
                    opts.frame_callback(out)
                except Exception:
                    logging.exception("preview frame callback failed")
            if not opts.no_preview:
                cv2.imshow("meeting-survivor", out)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            stats["displayed"] += 1
            stats["max_queue"] = max(stats["max_queue"], jobs.qsize())
            late = time.monotonic() - next_tick
            if late > frame_period:
                stats["missed"] += 1
            next_tick += frame_period
            if time.monotonic() - last_log >= 2.0:
                elapsed_for_stats = max(0.001, time.monotonic() - started_at)
                fps = stats["displayed"] / elapsed_for_stats
                generated_fps = stats["generated"] / elapsed_for_stats
                if opts.stats_callback:
                    try:
                        opts.stats_callback(
                            {
                                "previewFps": fps,
                                "generatedFps": generated_fps,
                                "queueDepth": jobs.qsize(),
                                "droppedJobs": stats["dropped_jobs"],
                                "renderMs": latest_render_time * 1000,
                            }
                        )
                    except Exception:
                        logging.exception("session stats callback failed")
                logging.info(
                    "preview_fps=%.1f generated=%s queue=%s missed=%s dropped=%s last_render_ms=%.0f rms=%.4f speaking=%s avatar=%s precision=%s target_generated_fps=%.1f delay_ms=%s",
                    fps,
                    stats["generated"],
                    jobs.qsize(),
                    stats["missed"],
                    stats["dropped_jobs"],
                    latest_render_time * 1000,
                    rms,
                    speaking,
                    assets.config.avatar_dir.name,
                    assets.config.precision,
                    active_config.generated_fps,
                    active_config.delay_ms,
                )
                last_log = time.monotonic()
    finally:
        stop.set()
        if audio:
            audio.stop()
        if writer:
            writer.release()
        cv2.destroyAllWindows()
    return stats
