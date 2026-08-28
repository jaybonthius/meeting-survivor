from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import mlx.core as mx
import numpy as np
import soundfile as sf

from .audio import LiveAudio, read_wav_16k, write_delayed_wav
from .avatar import composite_face, crop_resize, load_avatar
from .models import ensure_weights, load_pipeline


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


def _audio_chunk_from_pcm16k(pipe, pcm16: np.ndarray, fps: int = 25):
    from musetalk_mlx.whisper.audio2feature import get_whisper_chunk
    from musetalk_mlx.whisper.log_mel import log_mel_spectrogram

    if len(pcm16) < int(0.25 * 16000):
        pcm16 = np.pad(pcm16, (int(0.25 * 16000) - len(pcm16), 0))
    mel = log_mel_spectrogram(mx.array(pcm16.astype(np.float32)))
    stacked = pipe.whisper_encoder(mel)
    chunks = get_whisper_chunk(stacked, len(pcm16), fps=fps)
    return chunks[-1:]


def _rms(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples))))


def _load_or_make_latent(pipe, latents, crops, idx: int):
    if latents is not None:
        return mx.array(np.asarray(latents[idx:idx + 1]))
    return pipe.get_latents_for_unet(crops[idx])


def run_camera(opts: RunOptions) -> dict:
    meta, frames, crops, latents, masks = load_avatar(opts.avatar_dir)
    weights = ensure_weights(opts.precision, opts.weights_dir, allow_download=False)
    pipe = load_pipeline(weights)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    frame_period = 1.0 / 25.0
    generation_stride = 2 if opts.generated_fps <= 12.5 else 1
    jobs: queue.Queue = queue.Queue(maxsize=1)
    results: queue.Queue = queue.Queue(maxsize=4)
    stats = {"displayed": 0, "generated": 0, "missed": 0, "dropped_jobs": 0, "max_queue": 0}

    def worker():
        while not stop.is_set():
            try:
                job = jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            frame_idx, audio16 = job
            started = time.monotonic()
            try:
                dtype = getattr(pipe, "_dtype", mx.float32)
                latent = _load_or_make_latent(pipe, latents, crops, frame_idx).astype(dtype)
                chunk = _audio_chunk_from_pcm16k(pipe, audio16).astype(dtype)
                face = pipe.generate_faces(latent, chunk)[0]
                rendered = composite_face(
                    frames[frame_idx],
                    face,
                    meta["boxes"][frame_idx],
                    masks[frame_idx],
                    meta["mask_boxes"][frame_idx],
                )
                try:
                    results.put_nowait((frame_idx, rendered, time.monotonic() - started))
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
    if opts.wav_input:
        wav16, wav_rate = read_wav_16k(opts.wav_input)
        if opts.output_wav:
            write_delayed_wav(opts.wav_input, opts.output_wav, opts.delay_ms)
    else:
        audio = LiveAudio(opts.input_device, opts.output_device, opts.delay_ms)
        audio.start()
        logging.info("audio input_rate=%s output_rate=%s delay_ms=%s", audio.input_rate, audio.output_rate, opts.delay_ms)

    writer = None
    if opts.output_video:
        opts.output_video.parent.mkdir(parents=True, exist_ok=True)
        writer = cv2.VideoWriter(
            str(opts.output_video),
            cv2.VideoWriter_fourcc(*"mp4v"),
            25.0,
            (int(meta.get("width", 1280)), int(meta.get("height", 720))),
        )

    if not opts.no_preview:
        cv2.namedWindow("meeting-survivor", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("meeting-survivor", int(meta.get("width", 1280)), int(meta.get("height", 720)))

    gate = SpeechGate(opts.vad_threshold)
    latest_generated = None
    latest_render_time = 0.0
    started_at = time.monotonic()
    next_tick = started_at
    last_log = started_at

    try:
        while not stop.is_set():
            now = time.monotonic()
            if now < next_tick:
                time.sleep(min(0.005, next_tick - now))
                continue
            elapsed = now - started_at
            if opts.duration_seconds and elapsed >= opts.duration_seconds:
                break

            source_idx = stats["displayed"] % len(frames)
            frame = frames[source_idx]

            if wav16 is not None:
                pos = int(elapsed * wav_rate)
                start = max(0, pos - int(opts.audio_window_seconds * wav_rate))
                audio16 = wav16[start:pos]
                rms = _rms(wav16[max(0, pos - int(0.08 * wav_rate)):pos])
            else:
                audio16 = audio.recent_16k(opts.audio_window_seconds) if audio else np.zeros(0, dtype=np.float32)
                rms = audio.rms() if audio else 0.0

            speaking = gate.update(rms)
            if not speaking:
                latest_generated = None
            while True:
                try:
                    result_idx, rendered, render_time = results.get_nowait()
                    if speaking:
                        latest_generated = rendered
                    latest_render_time = render_time
                    stats["generated"] += 1
                except queue.Empty:
                    break

            should_generate = speaking and (stats["displayed"] % generation_stride == 0)
            if should_generate:
                try:
                    jobs.put_nowait((source_idx, audio16.copy()))
                except queue.Full:
                    stats["dropped_jobs"] += 1

            if speaking and latest_generated is not None:
                out = latest_generated
            else:
                out = frame

            if writer:
                writer.write(out)
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
                fps = stats["displayed"] / max(0.001, time.monotonic() - started_at)
                logging.info(
                    "preview_fps=%.1f generated=%s queue=%s missed=%s dropped=%s last_render_ms=%.0f rms=%.4f speaking=%s",
                    fps,
                    stats["generated"],
                    jobs.qsize(),
                    stats["missed"],
                    stats["dropped_jobs"],
                    latest_render_time * 1000,
                    rms,
                    speaking,
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
