from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf


def list_audio_devices() -> str:
    return str(sd.query_devices())


def list_audio_devices_data() -> list[dict]:
    devices = sd.query_devices()
    host_apis = sd.query_hostapis()
    default_input, default_output = sd.default.device
    records = []
    for index, info in enumerate(devices):
        host_api_index = int(info.get("hostapi", -1))
        host_api = host_apis[host_api_index]["name"] if 0 <= host_api_index < len(host_apis) else None
        max_input = int(info.get("max_input_channels", 0))
        max_output = int(info.get("max_output_channels", 0))
        records.append(
            {
                "id": str(index),
                "index": index,
                "name": str(info.get("name", "")),
                "hostApi": host_api,
                "maxInputChannels": max_input,
                "maxOutputChannels": max_output,
                "defaultSampleRate": float(info.get("default_samplerate", 0.0)),
                "isInput": max_input > 0,
                "isOutput": max_output > 0,
                "isDefaultInput": index == default_input,
                "isDefaultOutput": index == default_output,
            }
        )
    return records


def sounddevice_device_identifier(device: str | int | None) -> str | int | None:
    if isinstance(device, str):
        stripped = device.strip()
        if stripped.isdecimal():
            return int(stripped)
    return device


def mono_float32(samples: np.ndarray) -> np.ndarray:
    arr = np.asarray(samples, dtype=np.float32)
    if arr.ndim == 2:
        arr = arr.mean(axis=1)
    return np.clip(arr, -1.0, 1.0)


def resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int = 16000) -> np.ndarray:
    samples = mono_float32(samples)
    if src_rate == dst_rate or len(samples) == 0:
        return samples.astype(np.float32, copy=False)
    duration = len(samples) / float(src_rate)
    out_len = max(1, int(round(duration * dst_rate)))
    x_old = np.linspace(0.0, duration, num=len(samples), endpoint=False)
    x_new = np.linspace(0.0, duration, num=out_len, endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


class SampleRing:
    def __init__(self, max_samples: int):
        self.max_samples = max_samples
        self._items: collections.deque[float] = collections.deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def append(self, samples: np.ndarray) -> None:
        with self._lock:
            self._items.extend(float(x) for x in samples)

    def tail(self, seconds: float, sample_rate: int) -> np.ndarray:
        count = int(seconds * sample_rate)
        with self._lock:
            data = list(self._items)[-count:]
        return np.asarray(data, dtype=np.float32)

    def rms_tail(self, seconds: float, sample_rate: int) -> float:
        data = self.tail(seconds, sample_rate)
        if len(data) == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(data))))


class DelayLine:
    def __init__(self, delay_seconds: float, sample_rate: int, max_extra_seconds: float = 2.0):
        self._lock = threading.Lock()
        self._sample_rate = sample_rate
        self._max_extra_seconds = max_extra_seconds
        delay_samples = int(delay_seconds * sample_rate)
        max_samples = delay_samples + int(max_extra_seconds * sample_rate)
        self._queue: collections.deque[float] = collections.deque(maxlen=max_samples)
        self._queue.extend([0.0] * delay_samples)

    def set_delay_seconds(self, delay_seconds: float) -> None:
        target_samples = max(0, int(delay_seconds * self._sample_rate))
        max_samples = target_samples + int(self._max_extra_seconds * self._sample_rate)
        with self._lock:
            if self._queue.maxlen != max_samples:
                self._queue = collections.deque(self._queue, maxlen=max_samples)
            current_samples = len(self._queue)
            if current_samples < target_samples:
                self._queue.extendleft(0.0 for _ in range(target_samples - current_samples))
            elif current_samples > target_samples:
                for _ in range(current_samples - target_samples):
                    self._queue.popleft()

    def push(self, samples: np.ndarray) -> None:
        with self._lock:
            self._queue.extend(float(x) for x in mono_float32(samples))

    def pop(self, frames: int) -> np.ndarray:
        with self._lock:
            out = [self._queue.popleft() if self._queue else 0.0 for _ in range(frames)]
        return np.asarray(out, dtype=np.float32)


@dataclass
class LiveAudio:
    input_device: str | int | None
    output_device: str | int | None
    delay_ms: int
    block_ms: int = 20
    rolling_seconds: float = 3.0

    def __post_init__(self):
        self.input_device = sounddevice_device_identifier(self.input_device)
        self.output_device = sounddevice_device_identifier(self.output_device)
        self.input_rate = int(sd.query_devices(self.input_device, "input")["default_samplerate"])
        if self.output_device is None:
            self.output_rate = self.input_rate
            self.output_block = 0
            self.delay = None
        else:
            self.output_rate = int(sd.query_devices(self.output_device, "output")["default_samplerate"])
            self.output_block = max(1, int(self.output_rate * self.block_ms / 1000))
            self.delay = DelayLine(self.delay_ms / 1000.0, self.output_rate)
        self.input_block = max(1, int(self.input_rate * self.block_ms / 1000))
        self.ring16 = SampleRing(int(16000 * self.rolling_seconds))
        self.started_at = time.monotonic()
        self._input_stream = None
        self._output_stream = None

    def start(self) -> None:
        def input_cb(indata, frames, time_info, status):
            if status:
                print(status, flush=True)
            mono = mono_float32(indata)
            if self.delay is not None:
                self.delay.push(resample_linear(mono, self.input_rate, self.output_rate))
            self.ring16.append(resample_linear(mono, self.input_rate, 16000))

        def output_cb(outdata, frames, time_info, status):
            if status:
                print(status, flush=True)
            mono = self.delay.pop(frames)
            if outdata.ndim == 2:
                outdata[:] = mono[:, None]
            else:
                outdata[:] = mono

        self._input_stream = sd.InputStream(
            device=self.input_device,
            channels=1,
            samplerate=self.input_rate,
            blocksize=self.input_block,
            dtype="float32",
            callback=input_cb,
        )
        if self.output_device is not None:
            self._output_stream = sd.OutputStream(
                device=self.output_device,
                channels=1,
                samplerate=self.output_rate,
                blocksize=self.output_block,
                dtype="float32",
                callback=output_cb,
            )
        self._input_stream.start()
        if self._output_stream is not None:
            self._output_stream.start()

    def stop(self) -> None:
        for stream in (self._input_stream, self._output_stream):
            if stream is not None:
                stream.stop()
                stream.close()

    def set_delay_ms(self, delay_ms: int) -> None:
        self.delay_ms = delay_ms
        if self.delay is not None:
            self.delay.set_delay_seconds(delay_ms / 1000.0)

    def recent_16k(self, seconds: float) -> np.ndarray:
        return self.ring16.tail(seconds, 16000)

    def rms(self) -> float:
        return self.ring16.rms_tail(0.08, 16000)


def read_wav_16k(path: Path) -> tuple[np.ndarray, int]:
    audio, rate = sf.read(str(path), dtype="float32", always_2d=False)
    return resample_linear(audio, int(rate), 16000), 16000


def write_delayed_wav(input_wav: Path, output_wav: Path, delay_ms: int) -> None:
    audio, rate = sf.read(str(input_wav), dtype="float32", always_2d=False)
    silence_shape = (int(rate * delay_ms / 1000),) if np.asarray(audio).ndim == 1 else (int(rate * delay_ms / 1000), np.asarray(audio).shape[1])
    delayed = np.concatenate([np.zeros(silence_shape, dtype=np.float32), np.asarray(audio, dtype=np.float32)], axis=0)
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_wav), delayed, rate)
