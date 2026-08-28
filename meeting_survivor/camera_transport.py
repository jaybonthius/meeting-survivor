from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

CAMERA_FRAME_WIDTH = 1280
CAMERA_FRAME_HEIGHT = 720
CAMERA_FRAME_BYTES_PER_PIXEL = 4
CAMERA_FRAME_BYTES_PER_ROW = CAMERA_FRAME_WIDTH * CAMERA_FRAME_BYTES_PER_PIXEL
CAMERA_FRAME_DIRNAME = "CameraFrames"
CAMERA_FRAME_METADATA = "latest.json"
CAMERA_FRAME_RING_SIZE = 3


class CameraFrameWriter:
    """Write newest camera frame into an app-group-safe latest-frame transport."""

    def __init__(self, frame_dir: Path, width: int = CAMERA_FRAME_WIDTH, height: int = CAMERA_FRAME_HEIGHT):
        self.frame_dir = frame_dir.expanduser().resolve()
        self.width = width
        self.height = height
        self.bytes_per_row = width * CAMERA_FRAME_BYTES_PER_PIXEL
        self.sequence = self._existing_sequence()

    def clear(self) -> None:
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        self.sequence += 1
        self._write_metadata({"sequence": self.sequence, "state": "stopped"})

    def write_bgr(self, frame: np.ndarray) -> dict[str, Any]:
        self.frame_dir.mkdir(parents=True, exist_ok=True)
        bgra = self._bgra_frame(frame)
        self.sequence += 1
        frame_name = f"frame-{self.sequence % CAMERA_FRAME_RING_SIZE}.bgra"
        frame_path = self.frame_dir / frame_name
        tmp_path = self.frame_dir / f".{frame_name}.tmp"
        tmp_path.write_bytes(bgra.tobytes())
        tmp_path.replace(frame_path)
        metadata = {
            "sequence": self.sequence,
            "state": "running",
            "frameFile": frame_name,
            "width": self.width,
            "height": self.height,
            "pixelFormat": "bgra8",
            "bytesPerRow": self.bytes_per_row,
        }
        self._write_metadata(metadata)
        return metadata

    def _bgra_frame(self, frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 2:
            resized = self._resize(frame)
            bgra = cv2.cvtColor(resized, cv2.COLOR_GRAY2BGRA)
        elif frame.ndim == 3 and frame.shape[2] == 3:
            resized = self._resize(frame)
            bgra = cv2.cvtColor(resized, cv2.COLOR_BGR2BGRA)
        elif frame.ndim == 3 and frame.shape[2] == 4:
            bgra = self._resize(frame)
        else:
            raise ValueError("camera frame must be grayscale, BGR, or BGRA")
        return np.ascontiguousarray(bgra, dtype=np.uint8)

    def _resize(self, frame: np.ndarray) -> np.ndarray:
        if frame.shape[0] == self.height and frame.shape[1] == self.width:
            return frame
        return cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_LINEAR)

    def _write_metadata(self, metadata: dict[str, Any]) -> None:
        metadata = {**metadata, "transportVersion": 1, "timestamp": time.time()}
        metadata_path = self.frame_dir / CAMERA_FRAME_METADATA
        tmp_path = self.frame_dir / f".{CAMERA_FRAME_METADATA}.tmp"
        tmp_path.write_text(json.dumps(metadata, separators=(",", ":"), sort_keys=True))
        tmp_path.replace(metadata_path)

    def _existing_sequence(self) -> int:
        metadata_path = self.frame_dir / CAMERA_FRAME_METADATA
        if not metadata_path.exists():
            return 0
        try:
            data = json.loads(metadata_path.read_text())
            sequence = data.get("sequence", 0)
            return sequence if isinstance(sequence, int) else 0
        except (OSError, json.JSONDecodeError):
            return 0
