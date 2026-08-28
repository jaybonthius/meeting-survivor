from __future__ import annotations

import json
import logging
import time
import threading
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from rich.progress import track

from .models import ensure_weights, load_pipeline, write_pin_file
from .upstream import ensure_prep_weights, import_upstream_utils

RESIZED_FACE = 256
COORD_PLACEHOLDER = [0.0, 0.0, 0.0, 0.0]
ProgressCallback = Callable[[str, int, int], None]


class AvatarPreparationCancelled(Exception):
    pass


def _safe_name(path: Path) -> str:
    return path.stem.replace(" ", "_")


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise AvatarPreparationCancelled("operation cancelled")


def _emit_progress(progress_callback: ProgressCallback | None, stage: str, current: int, total: int) -> None:
    if progress_callback is not None:
        progress_callback(stage, current, total)


def _extract_frames(
    video_path: Path,
    frames_dir: Path,
    limit: int,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    show_progress: bool = True,
) -> tuple[list[np.ndarray], list[str]]:
    cap = cv2.VideoCapture(str(video_path))
    frames: list[np.ndarray] = []
    rel_paths: list[str] = []
    try:
        for idx in track(range(limit), description="Extracting frames", disable=not show_progress):
            _raise_if_cancelled(cancel_event)
            ok, frame = cap.read()
            if not ok:
                break
            name = f"frame_{idx:06d}.png"
            cv2.imwrite(str(frames_dir / name), frame)
            frames.append(frame)
            rel_paths.append(f"frames/{name}")
            _emit_progress(progress_callback, "extractFrames", idx + 1, limit)
    finally:
        cap.release()
    return frames, rel_paths


def _landmarks_and_boxes(
    frames: list[np.ndarray],
    bbox_shift: int,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    show_progress: bool = True,
) -> list[list[int]]:
    import_upstream_utils(allow_download=False)
    from face_detection import FaceAlignment, LandmarksType
    from rtmlib import Wholebody

    root = Path.cwd()
    dw = root / "models" / "dwpose"
    pose = Wholebody(
        det=str(dw / "yolox_l.onnx"),
        pose=str(dw / "dw-ll_ucoco_384.onnx"),
        pose_input_size=(288, 384),
        backend="onnxruntime",
        device="cpu",
    )
    fa = FaceAlignment(LandmarksType._2D, flip_input=False, device="cpu")

    boxes: list[list[int]] = []
    total = len(frames)
    for idx, frame in track(list(enumerate(frames)), description="Detecting face landmarks", disable=not show_progress):
        _raise_if_cancelled(cancel_event)
        kpts, _ = pose(frame)
        bbox = fa.get_detections_for_batch(np.asarray([frame]))[0]
        if bbox is None or len(kpts) == 0:
            boxes.append(COORD_PLACEHOLDER.copy())
            logging.warning("no face landmarks for frame %s", idx)
            _emit_progress(progress_callback, "landmarks", idx + 1, total)
            continue
        face_landmark = kpts[0][23:91].astype(np.int32)
        half_face_coord = face_landmark[29].copy()
        half_face_coord[1] = half_face_coord[1] + bbox_shift
        half_face_dist = np.max(face_landmark[:, 1]) - half_face_coord[1]
        upper_bond = max(0, half_face_coord[1] - half_face_dist)
        x1, y1, x2, y2 = (
            int(np.min(face_landmark[:, 0])),
            int(upper_bond),
            int(np.max(face_landmark[:, 0])),
            int(np.max(face_landmark[:, 1])),
        )
        if y2 - y1 <= 0 or x2 - x1 <= 0 or x1 < 0:
            boxes.append([int(v) for v in bbox])
        else:
            boxes.append([x1, y1, x2, y2])
        _emit_progress(progress_callback, "landmarks", idx + 1, total)
    return boxes


def _valid_box(box: list[int | float]) -> bool:
    return list(box) != COORD_PLACEHOLDER and box[2] > box[0] and box[3] > box[1]


def crop_resize(frame: np.ndarray, box: list[int | float]) -> np.ndarray:
    x1, y1, x2, y2 = [int(v) for v in box]
    return cv2.resize(frame[y1:y2, x1:x2], (RESIZED_FACE, RESIZED_FACE), interpolation=cv2.INTER_LANCZOS4)


def composite_face(
    frame: np.ndarray,
    generated_face: np.ndarray,
    box: list[int | float],
    mask: np.ndarray | None = None,
    mask_box: list[int] | None = None,
) -> np.ndarray:
    if not _valid_box(box):
        return frame
    x1, y1, x2, y2 = [int(v) for v in box]
    face = cv2.resize(generated_face.astype(np.uint8), (x2 - x1, y2 - y1), interpolation=cv2.INTER_LANCZOS4)
    if mask is not None and mask_box is not None:
        import_upstream_utils(allow_download=False)
        from musetalk.utils.blending import get_image_blending

        return get_image_blending(frame.copy(), face, [x1, y1, x2, y2], mask, mask_box)
    out = frame.copy()
    out[y1:y2, x1:x2] = face
    return out


def prepare_avatar(
    video_path: Path,
    avatar_dir: Path | None,
    precision: str,
    weights_dir: Path | None,
    download_model: bool,
    skip_latents: bool,
    max_seconds: float | None,
    bbox_shift: int = 0,
    extra_margin: int = 10,
    parsing_mode: str = "jaw",
    left_cheek_width: int = 90,
    right_cheek_width: int = 90,
    progress_callback: ProgressCallback | None = None,
    cancel_event: threading.Event | None = None,
    show_progress: bool = True,
) -> Path:
    video_path = video_path.expanduser().resolve()
    if not video_path.exists():
        raise FileNotFoundError(video_path)
    avatar_dir = avatar_dir or Path("avatars") / _safe_name(video_path)
    frames_dir = avatar_dir / "frames"
    crops_dir = avatar_dir / "crops"
    masks_dir = avatar_dir / "masks"
    for directory in (avatar_dir, frames_dir, crops_dir, masks_dir):
        directory.mkdir(parents=True, exist_ok=True)

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    if width != 1280 or height != 720:
        logging.warning("expected 1280x720 source; got %sx%s", width, height)
    if abs(fps - 25.0) > 0.5:
        logging.warning("expected 25 fps source; got %.3f", fps)
    limit = total
    if max_seconds:
        limit = min(limit, int(max_seconds * max(fps, 25)))

    _raise_if_cancelled(cancel_event)
    ensure_prep_weights(allow_download=download_model)
    import_upstream_utils(allow_download=download_model)

    frames, frame_paths = _extract_frames(video_path, frames_dir, limit, progress_callback, cancel_event, show_progress)
    if not frames:
        raise RuntimeError("No frames extracted")
    logging.info("extracted %s avatar frames", len(frames))

    boxes = _landmarks_and_boxes(frames, bbox_shift=bbox_shift, progress_callback=progress_callback, cancel_event=cancel_event, show_progress=show_progress)
    for i, (box, frame) in enumerate(zip(boxes, frames)):
        if not _valid_box(box):
            continue
        box[3] = min(int(box[3]) + extra_margin, frame.shape[0])
        boxes[i] = [int(v) for v in box]

    crop_paths: list[str] = []
    for idx, (frame, box) in track(list(enumerate(zip(frames, boxes))), description="Writing face crops", disable=not show_progress):
        _raise_if_cancelled(cancel_event)
        if not _valid_box(box):
            _emit_progress(progress_callback, "crops", idx + 1, len(frames))
            continue
        crop = crop_resize(frame, box)
        name = f"crop_{idx:06d}.png"
        cv2.imwrite(str(crops_dir / name), crop)
        crop_paths.append(f"crops/{name}")
        _emit_progress(progress_callback, "crops", idx + 1, len(frames))

    masks: list[np.ndarray | None] = []
    mask_boxes: list[list[int] | None] = []
    if not skip_latents:
        import torch
        torch.load = __import__("functools").partial(torch.load, weights_only=False)
        from musetalk.utils.blending import get_image_prepare_material
        from musetalk.utils.face_parsing import FaceParsing

        fp = FaceParsing(left_cheek_width=left_cheek_width, right_cheek_width=right_cheek_width)
        for idx, (frame, box) in track(list(enumerate(zip(frames, boxes))), description="Preparing blend masks", disable=not show_progress):
            _raise_if_cancelled(cancel_event)
            if not _valid_box(box):
                masks.append(None)
                mask_boxes.append(None)
                _emit_progress(progress_callback, "masks", idx + 1, len(frames))
                continue
            mask, crop_box = get_image_prepare_material(frame, box, fp=fp, mode=parsing_mode)
            name = f"mask_{idx:06d}.png"
            cv2.imwrite(str(masks_dir / name), mask)
            masks.append(mask)
            mask_boxes.append([int(v) for v in crop_box])
            _emit_progress(progress_callback, "masks", idx + 1, len(frames))

    metadata = {
        "source_video": str(video_path),
        "created_at": time.time(),
        "width": width,
        "height": height,
        "source_fps": fps,
        "output_fps": 25,
        "frame_count": len(frames),
        "face_size": RESIZED_FACE,
        "frames": frame_paths,
        "boxes": boxes,
        "mask_boxes": mask_boxes,
        "bbox_shift": bbox_shift,
        "extra_margin": extra_margin,
        "parsing_mode": parsing_mode,
        "left_cheek_width": left_cheek_width,
        "right_cheek_width": right_cheek_width,
        "prep": "upstream MuseTalk S3FD/DWPose coords plus BiSeNet masks",
    }
    (avatar_dir / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")

    if not skip_latents:
        resolved_weights = ensure_weights(precision, weights_dir, allow_download=download_model)
        pipe = load_pipeline(resolved_weights)
        latents = []
        for i, (frame, box) in track(list(enumerate(zip(frames, boxes))), description="Encoding MuseTalk latents", disable=not show_progress):
            _raise_if_cancelled(cancel_event)
            if not _valid_box(box):
                _emit_progress(progress_callback, "latents", i + 1, len(frames))
                continue
            crop = crop_resize(frame, box)
            latent = pipe.get_latents_for_unet(crop)
            latents.append(np.array(latent))
            _emit_progress(progress_callback, "latents", i + 1, len(frames))
        if not latents:
            raise RuntimeError("No valid face crops found")
        np.save(avatar_dir / "latents.npy", np.concatenate(latents, axis=0))
        write_pin_file(avatar_dir, precision, resolved_weights)
        logging.info("wrote latents.npy")
    else:
        logging.warning("skipped latent/mask generation; run will not have the proper MuseTalk blend cache")

    logging.info("prepared avatar at %s", avatar_dir)
    return avatar_dir


def _cycle(items: list):
    return items + items[::-1]


def load_avatar(avatar_dir: Path):
    meta_path = avatar_dir / "metadata.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"Missing avatar metadata: {meta_path}")
    meta = json.loads(meta_path.read_text())
    frames = [cv2.imread(str(avatar_dir / rel), cv2.IMREAD_COLOR) for rel in meta["frames"]]
    if any(f is None for f in frames):
        raise RuntimeError(f"Avatar cache has unreadable frames: {avatar_dir}")

    valid_indexes = [i for i, box in enumerate(meta["boxes"]) if _valid_box(box)]
    frames = [frames[i] for i in valid_indexes]
    boxes = [meta["boxes"][i] for i in valid_indexes]
    masks = []
    mask_boxes = []
    for i in valid_indexes:
        mask_path = avatar_dir / "masks" / f"mask_{i:06d}.png"
        masks.append(cv2.imread(str(mask_path), cv2.IMREAD_GRAYSCALE) if mask_path.exists() else None)
        mb = meta.get("mask_boxes", [])
        mask_boxes.append(mb[i] if i < len(mb) else None)
    crops = [crop_resize(frame, box) for frame, box in zip(frames, boxes)]

    latents = None
    latent_path = avatar_dir / "latents.npy"
    if latent_path.exists():
        raw_latents = np.load(latent_path)
        latents = np.concatenate([raw_latents, raw_latents[::-1]], axis=0)

    meta = dict(meta)
    meta["boxes"] = _cycle(boxes)
    meta["mask_boxes"] = _cycle(mask_boxes)
    return meta, _cycle(frames), _cycle(crops), latents, _cycle(masks)
