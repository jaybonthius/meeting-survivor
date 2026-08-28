from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .audio import list_audio_devices
from .avatar import prepare_avatar
from .backend import run_backend
from .live import RunOptions, run_camera
from .models import MODEL_REPOS


def _device(value: str):
    try:
        return int(value)
    except ValueError:
        return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meeting-survivor", description="Local talking-head preview for OBS/Teams")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list-devices", help="print audio input/output devices")

    backend = sub.add_parser("backend", help="run structured local IPC backend")
    backend.add_argument("--socket", type=Path, required=True, help="Unix domain socket path")
    backend.add_argument("--app-support", type=Path, required=True, help="app-owned state directory")

    prep = sub.add_parser("prepare", help="prepare an avatar from a 720p/25fps source video")
    prep.add_argument("video", type=Path)
    prep.add_argument("--avatar-dir", type=Path)
    prep.add_argument("--precision", choices=sorted(MODEL_REPOS), default="q8")
    prep.add_argument("--weights-dir", type=Path)
    prep.add_argument("--download-model", action="store_true", help="download MLX weights if missing")
    prep.add_argument("--skip-latents", action="store_true", help="extract frames only; run computes latents lazily")
    prep.add_argument("--max-seconds", type=float, default=10.0, help="cap source clip length; default 10")
    prep.add_argument("--bbox-shift", type=int, default=0, help="MuseTalk crop tuning: positive usually increases mouth motion")
    prep.add_argument("--extra-margin", type=int, default=10, help="pixels added below v1.5 crop; use 0-40")
    prep.add_argument("--parsing-mode", choices=["jaw", "raw", "neck"], default="jaw")
    prep.add_argument("--left-cheek-width", type=int, default=90)
    prep.add_argument("--right-cheek-width", type=int, default=90)

    run = sub.add_parser("run", help="run live preview and delayed mic audio")
    run.add_argument("--avatar-dir", type=Path, required=True)
    run.add_argument("--precision", choices=sorted(MODEL_REPOS), default="q8")
    run.add_argument("--weights-dir", type=Path)
    run.add_argument("--input-device", type=_device, help="physical microphone device id or name")
    run.add_argument("--output-device", type=_device, help="BlackHole output device id or name")
    run.add_argument("--delay-ms", type=int, default=400)
    run.add_argument("--generated-fps", type=float, choices=[12.5, 25.0], default=25.0)
    run.add_argument("--vad-threshold", type=float, default=0.012)
    run.add_argument("--audio-window-seconds", type=float, default=1.2)
    run.add_argument("--wav-input", type=Path, help="test seam: drive the same loop from a wav file")
    run.add_argument("--output-video", type=Path)
    run.add_argument("--output-wav", type=Path)
    run.add_argument("--duration-seconds", type=float)
    run.add_argument("--no-preview", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s %(levelname)s %(message)s")

    try:
        if args.command == "list-devices":
            print(list_audio_devices())
            return

        if args.command == "backend":
            run_backend(args.socket, args.app_support)
            return

        if args.command == "prepare":
            avatar = prepare_avatar(
                video_path=args.video,
                avatar_dir=args.avatar_dir,
                precision=args.precision,
                weights_dir=args.weights_dir,
                download_model=args.download_model,
                skip_latents=args.skip_latents,
                max_seconds=args.max_seconds,
                bbox_shift=args.bbox_shift,
                extra_margin=args.extra_margin,
                parsing_mode=args.parsing_mode,
                left_cheek_width=args.left_cheek_width,
                right_cheek_width=args.right_cheek_width,
            )
            print(avatar)
            return

        if args.command == "run":
            if not args.wav_input and args.output_device is None:
                raise SystemExit("--output-device is required for live mic mode; run list-devices first")
            opts = RunOptions(
                avatar_dir=args.avatar_dir,
                precision=args.precision,
                weights_dir=args.weights_dir,
                input_device=args.input_device,
                output_device=args.output_device,
                delay_ms=args.delay_ms,
                generated_fps=args.generated_fps,
                vad_threshold=args.vad_threshold,
                audio_window_seconds=args.audio_window_seconds,
                wav_input=args.wav_input,
                output_video=args.output_video,
                output_wav=args.output_wav,
                duration_seconds=args.duration_seconds,
                no_preview=args.no_preview,
            )
            stats = run_camera(opts)
            print(stats)
            return
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    raise SystemExit(f"unknown command: {args.command}")
