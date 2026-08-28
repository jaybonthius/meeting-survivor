from __future__ import annotations

import json
import logging
from pathlib import Path

MODEL_REPOS = {
    "q8": "mlx-community/MuseTalk-1.5-q8",
    "fp16": "mlx-community/MuseTalk-1.5-fp16",
    "q4": "mlx-community/MuseTalk-1.5-q4",
}


def default_weights_dir(precision: str) -> Path:
    return Path("models") / f"MuseTalk-1.5-{precision}"


def has_weights(path: Path) -> bool:
    return all((path / name).exists() for name in (
        "config.json",
        "unet.safetensors",
        "vae.safetensors",
        "whisper_encoder.safetensors",
    ))


def ensure_weights(precision: str, weights_dir: Path | None, allow_download: bool) -> Path:
    dest = weights_dir or default_weights_dir(precision)
    if has_weights(dest):
        return dest
    if not allow_download:
        raise FileNotFoundError(
            f"MuseTalk {precision} MLX weights not found at {dest}. "
            f"Run prepare with --download-model or pass --weights-dir."
        )

    from huggingface_hub import snapshot_download

    repo_id = MODEL_REPOS[precision]
    logging.info("downloading %s to %s", repo_id, dest)
    snapshot_download(
        repo_id=repo_id,
        local_dir=str(dest),
        local_dir_use_symlinks=False,
        allow_patterns=["config.json", "*.safetensors", "README.md", "LICENSE*"],
    )
    if not has_weights(dest):
        raise FileNotFoundError(f"Downloaded snapshot at {dest} is missing required files")
    return dest


def load_pipeline(weights_dir: Path):
    from musetalk_mlx.pipeline_mlx import MuseTalkPipeline

    logging.info("loading MuseTalk MLX weights from %s", weights_dir)
    return MuseTalkPipeline.from_pretrained_mlx(weights_dir)


def write_pin_file(path: Path, precision: str, weights_dir: Path) -> None:
    data = {
        "musetalk_mlx_repo": "https://github.com/xocialize/musetalk-mlx",
        "musetalk_mlx_revision": "c6eb30ebd1ed4d043983209813370153de9346bf",
        "model_repo": MODEL_REPOS[precision],
        "weights_dir": str(weights_dir),
        "note": "Model files are downloaded during prepare only; run should work offline after this.",
    }
    (path / "model-pins.json").write_text(json.dumps(data, indent=2) + "\n")
