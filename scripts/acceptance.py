#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path

import cv2
import soundfile as sf


def main() -> int:
    parser = argparse.ArgumentParser(description="Small end-to-end harness for prerecorded WAV mode")
    parser.add_argument("--avatar-dir", type=Path, required=True)
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--seconds", type=float, default=60.0)
    parser.add_argument("--delay-ms", type=int, default=400)
    parser.add_argument("--out-dir", type=Path, default=Path("outputs/acceptance"))
    parser.add_argument("--generated-fps", choices=["12.5", "25.0"], default="25.0")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    video = args.out_dir / "preview.mp4"
    wav_out = args.out_dir / "delayed.wav"
    cmd = [
        sys.executable,
        "-m",
        "meeting_survivor",
        "run",
        "--avatar-dir",
        str(args.avatar_dir),
        "--wav-input",
        str(args.wav),
        "--output-video",
        str(video),
        "--output-wav",
        str(wav_out),
        "--duration-seconds",
        str(args.seconds),
        "--delay-ms",
        str(args.delay_ms),
        "--generated-fps",
        args.generated_fps,
        "--no-preview",
    ]
    completed = subprocess.run(cmd, check=True, text=True, capture_output=True)
    print(completed.stdout)
    stats = {}
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith("{") and line.endswith("}"):
            stats = ast.literal_eval(line)
            break

    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS)
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    audio, rate = sf.read(str(wav_out), dtype="float32", always_2d=False)
    source_audio, source_rate = sf.read(str(args.wav), dtype="float32", always_2d=False)
    delayed_seconds = len(audio) / rate
    expected_delay_seconds = len(source_audio) / source_rate + args.delay_ms / 1000.0

    expected_frames = int(round(args.seconds * 25))
    report = {
        "video": str(video),
        "video_fps": fps,
        "video_frames": frames,
        "expected_frames": expected_frames,
        "delayed_wav": str(wav_out),
        "delayed_wav_seconds": delayed_seconds,
        "expected_delayed_wav_seconds": expected_delay_seconds,
        "stats": stats,
    }
    (args.out_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))

    if abs(fps - 25.0) > 0.25:
        raise SystemExit("preview recording is not 25 fps")
    if abs(frames - expected_frames) > 2:
        raise SystemExit("preview frame count is not the expected fixed cadence")
    if abs(delayed_seconds - expected_delay_seconds) > 1 / 25:
        raise SystemExit("delayed audio length does not match configured delay within one video frame")
    if stats.get("max_queue", 0) > 1:
        raise SystemExit("render queue exceeded its fixed bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
