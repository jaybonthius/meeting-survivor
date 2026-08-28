from __future__ import annotations

import logging
import os
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path

MUSE_TALK_REPO = "https://github.com/TMElyralab/MuseTalk.git"
MUSE_TALK_REVISION = "0a89dec45a0192b824e3cf4daf96c239440c5ed8"


@contextmanager
def pushd(path: Path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def project_root() -> Path:
    return Path.cwd()


def upstream_dir(root: Path | None = None) -> Path:
    return (root or project_root()) / "third_party" / "MuseTalk"


def ensure_upstream_source(allow_download: bool) -> Path:
    path = upstream_dir()
    if (path / "musetalk" / "utils" / "blending.py").exists():
        return path
    if not allow_download:
        raise FileNotFoundError(
            f"MuseTalk source not found at {path}. Run prepare with --download-model once."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.info("cloning MuseTalk source to %s", path)
    subprocess.run(["git", "clone", MUSE_TALK_REPO, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "checkout", MUSE_TALK_REVISION], check=True)
    return path


def _download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    logging.info("downloading %s", dest)
    urllib.request.urlretrieve(url, dest)


def ensure_prep_weights(allow_download: bool) -> None:
    root = project_root()
    dwpose = root / "models" / "dwpose"
    face_parse = root / "models" / "face-parse-bisent"
    needed = [
        dwpose / "yolox_l.onnx",
        dwpose / "dw-ll_ucoco_384.onnx",
        face_parse / "79999_iter.pth",
        face_parse / "resnet18-5c106cde.pth",
    ]
    if all(p.exists() for p in needed):
        return
    if not allow_download:
        missing = ", ".join(str(p) for p in needed if not p.exists())
        raise FileNotFoundError(f"MuseTalk prep weights missing: {missing}. Run prepare with --download-model once.")

    from huggingface_hub import hf_hub_download

    dwpose.mkdir(parents=True, exist_ok=True)
    face_parse.mkdir(parents=True, exist_ok=True)
    for filename in ("yolox_l.onnx", "dw-ll_ucoco_384.onnx"):
        dest = dwpose / filename
        if not dest.exists():
            downloaded = Path(hf_hub_download("yzd-v/DWPose", filename=filename, local_dir=str(dwpose)))
            if downloaded != dest and downloaded.exists():
                dest.write_bytes(downloaded.read_bytes())
    if not (face_parse / "79999_iter.pth").exists():
        hf_hub_download(
            "ManyOtherFunctions/face-parse-bisent",
            filename="79999_iter.pth",
            local_dir=str(face_parse),
        )
    if not (face_parse / "resnet18-5c106cde.pth").exists():
        _download_file(
            "https://download.pytorch.org/models/resnet18-5c106cde.pth",
            face_parse / "resnet18-5c106cde.pth",
        )


def import_upstream_utils(allow_download: bool) -> None:
    src = ensure_upstream_source(allow_download)
    utils = src / "musetalk" / "utils"
    for item in (src, utils):
        s = str(item)
        if s not in sys.path:
            sys.path.insert(0, s)
