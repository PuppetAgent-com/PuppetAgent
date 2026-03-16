#!/usr/bin/env python3
"""
bg-replacement.py — Replace background in a video using MediaPipe selfie segmentation.

Usage:
    conda run -n wav2lip python3 bg-replacement.py \
        --input video.mp4 --background image.png --output result.mp4

Method:
    MediaPipe ImageSegmenter (selfie_multiclass_256x256 tflite model)
    Per-frame: extract person mask → alpha-composite over static background
    Model auto-downloaded to /tmp/ on first run.

Deps: mediapipe>=0.10, opencv-python, numpy (all in wav2lip conda env)
"""

import argparse
import sys
import urllib.request
from pathlib import Path

import cv2
import numpy as np

MODEL_URL  = "https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite"
MODEL_PATH = "/tmp/selfie_multiclass_256x256.tflite"

# Category index for "person" in the multiclass model
# 0=background, 1=hair, 2=body-skin, 3=face-skin, 4=clothes, 5=accessories
PERSON_CATS = {1, 2, 3, 4, 5}   # everything that is NOT background


def ensure_model():
    p = Path(MODEL_PATH)
    if not p.exists() or p.stat().st_size < 50_000:
        print(f"[bg-replace] Downloading segmentation model to {MODEL_PATH}...")
        urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)
        print("[bg-replace] Model downloaded.")


def build_segmenter():
    """Create MediaPipe ImageSegmenter (Tasks API v0.10+)."""
    import mediapipe as mp
    BaseOptions = mp.tasks.BaseOptions
    ImageSegmenter = mp.tasks.vision.ImageSegmenter
    ImageSegmenterOptions = mp.tasks.vision.ImageSegmenterOptions
    VisionRunningMode = mp.tasks.vision.RunningMode

    options = ImageSegmenterOptions(
        base_options=BaseOptions(model_asset_path=MODEL_PATH),
        running_mode=VisionRunningMode.VIDEO,
        output_category_mask=True,
    )
    return ImageSegmenter.create_from_options(options)


def replace_background(input_path: str, background_path: str, output_path: str):
    ensure_model()

    import mediapipe as mp

    cap = cv2.VideoCapture(input_path)
    if not cap.isOpened():
        print(f"[bg-replace] ERROR: cannot open {input_path}", file=sys.stderr)
        sys.exit(1)

    fps    = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w      = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h      = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Load and resize background
    bg_raw = cv2.imread(background_path)
    if bg_raw is None:
        print(f"[bg-replace] ERROR: cannot read background {background_path}", file=sys.stderr)
        sys.exit(1)
    bg = cv2.resize(bg_raw, (w, h))

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    tmp_out = str(Path(output_path).with_suffix(".tmp.mp4"))
    writer = cv2.VideoWriter(tmp_out, fourcc, fps, (w, h))

    segmenter = build_segmenter()
    frame_idx = 0
    timestamp_ms = 0

    print(f"[bg-replace] Processing {n_frames} frames at {fps:.1f} fps ...")

    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break

        # Convert BGR→RGB for MediaPipe
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        mp_image  = mp.Image(image_format=mp.ImageFormat.SRGB, data=frame_rgb)

        timestamp_ms = int(frame_idx * 1000 / fps)
        result = segmenter.segment_for_video(mp_image, timestamp_ms)

        cat_mask = result.category_mask.numpy_view()   # H×W uint8, category IDs

        # Build person mask: 1 where person, 0 where background
        person_mask = np.zeros((h, w), dtype=np.uint8)
        for cat_id in PERSON_CATS:
            person_mask[cat_mask == cat_id] = 255

        # Light morphological cleanup — close small holes, smooth edges
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        person_mask = cv2.morphologyEx(person_mask, cv2.MORPH_CLOSE, kernel)
        person_mask = cv2.GaussianBlur(person_mask, (7, 7), 0)

        # Alpha-composite: output = person * alpha + bg * (1-alpha)
        alpha = person_mask.astype(np.float32) / 255.0
        alpha3 = np.stack([alpha, alpha, alpha], axis=-1)

        frame_f  = frame_bgr.astype(np.float32)
        bg_f     = bg.astype(np.float32)
        composite = (frame_f * alpha3 + bg_f * (1.0 - alpha3)).astype(np.uint8)

        writer.write(composite)
        frame_idx += 1

        if frame_idx % 50 == 0:
            print(f"[bg-replace]   {frame_idx}/{n_frames} frames done")

    cap.release()
    writer.release()
    segmenter.close()

    # Re-mux with original audio using ffmpeg (no re-encode)
    print("[bg-replace] Re-muxing audio...")
    import subprocess
    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", tmp_out,
        "-i", input_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-map", "0:v:0",
        "-map", "1:a:0?",   # audio from original (optional — may not exist)
        "-shortest",
        output_path
    ], capture_output=True, text=True)

    Path(tmp_out).unlink(missing_ok=True)

    if r.returncode != 0:
        # Fallback: rename tmp output (no audio)
        import shutil
        shutil.move(tmp_out + ".bak" if Path(tmp_out + ".bak").exists() else tmp_out,
                    output_path)
        print(f"[bg-replace] WARNING: audio mux failed: {r.stderr[-200:]}")
    else:
        print(f"[bg-replace] Done → {output_path}")


def main():
    ap = argparse.ArgumentParser(description="Replace video background using MediaPipe segmentation")
    ap.add_argument("--input",      required=True, help="Input video path")
    ap.add_argument("--background", required=True, help="Background image path (.jpg/.png)")
    ap.add_argument("--output",     required=True, help="Output video path")
    args = ap.parse_args()

    if not Path(args.input).exists():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        sys.exit(1)
    if not Path(args.background).exists():
        print(f"ERROR: background not found: {args.background}", file=sys.stderr)
        sys.exit(1)

    replace_background(args.input, args.background, args.output)


if __name__ == "__main__":
    main()
