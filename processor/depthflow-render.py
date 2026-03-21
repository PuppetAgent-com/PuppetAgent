#!/usr/bin/env python3
"""
depthflow-render.py — Render a parallax/3D-Ken-Burns video from a single image using DepthFlow.

Uses DepthAnything V1 (da1) as the depth estimator — fastest option (~6-11s per image),
sufficient quality for b-roll use.

Usage:
  python3 depthflow-render.py --image /path/to/image.jpg --slug my-topic
  python3 depthflow-render.py --image https://example.com/photo.jpg --slug ocean-yacht
  python3 depthflow-render.py --image URL --slug NAME [--time 5] [--height 720]

Output: uploads to MinIO video-production/broll/depthflow/{slug}.mp4
        prints JSON manifest to stdout
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# ── MinIO config (same as broll-fetch.py) ────────────────────────────────────
MINIO_ENDPOINT   = "http://localhost:9004"
MINIO_BUCKET     = "video-production"
MINIO_USER       = "your_minio_user"
MINIO_PASSWORD   = "your_minio_secret"
MINIO_PUBLIC_BASE = "http://your-server:9000"
DEPTHFLOW_PREFIX = "broll/depthflow"

# ── DepthFlow config ──────────────────────────────────────────────────────────
DEPTHFLOW_BIN = os.path.expanduser("~/venvs/depthflow/bin/depthflow")
DEPTHFLOW_ENV = {
    **os.environ,
    "ROCR_VISIBLE_DEVICES": "",      # force CPU inference (avoids gfx1103 ROCm crash)
    "WINDOW_BACKEND": "headless",    # no display needed
}


def init_minio():
    try:
        import boto3
        from botocore.client import Config
    except ImportError:
        print("ERROR: boto3 not installed. Run: pip3 install boto3", file=sys.stderr)
        sys.exit(1)
    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_USER,
        aws_secret_access_key=MINIO_PASSWORD,
        config=Config(signature_version="s3v4"),
        region_name="us-east-1",
    )


def download_image(url: str, dest: Path):
    """Download an image from a URL to dest path."""
    req = urllib.request.Request(url, headers={"User-Agent": "depthflow-render/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        f.write(r.read())


def render_depthflow(image_path: str, output_mp4: str, duration: int, height: int):
    """Run DepthFlow da1 estimator and render parallax video."""
    cmd = [
        DEPTHFLOW_BIN,
        "da1",
        "input", "-i", image_path,
        "main",
        "-o", output_mp4,
        "--time", str(duration),
        "--height", str(height),
    ]
    result = subprocess.run(cmd, env=DEPTHFLOW_ENV, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"DepthFlow failed (exit {result.returncode}):\n{result.stderr[-800:]}")
    return result.stdout


def upload_to_minio(client, local_path: str, object_key: str) -> str:
    client.upload_file(
        local_path,
        MINIO_BUCKET,
        object_key,
        ExtraArgs={"ContentType": "video/mp4"},
    )
    return f"{MINIO_PUBLIC_BASE}/{MINIO_BUCKET}/{object_key}"


def main():
    parser = argparse.ArgumentParser(description="Render parallax video from image using DepthFlow")
    parser.add_argument("--image", required=True, help="Image URL or local path")
    parser.add_argument("--slug",  required=True, help="Output filename slug (used as MinIO key)")
    parser.add_argument("--time",   type=int, default=5,   help="Video duration in seconds (default: 5)")
    parser.add_argument("--height", type=int, default=720, help="Output height in pixels (default: 720)")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)

        # ── 1. Acquire image ──────────────────────────────────────────────────
        if args.image.startswith("http://") or args.image.startswith("https://"):
            ext = Path(args.image.split("?")[0]).suffix or ".jpg"
            img_path = tmp / f"input{ext}"
            print(f"Downloading image → {img_path}", file=sys.stderr)
            download_image(args.image, img_path)
        else:
            img_path = Path(args.image)
            if not img_path.exists():
                print(f"ERROR: image not found: {img_path}", file=sys.stderr)
                sys.exit(1)

        # ── 2. Render with DepthFlow ──────────────────────────────────────────
        out_mp4 = tmp / f"{args.slug}.mp4"
        print(f"Rendering with DepthFlow da1 → {out_mp4}", file=sys.stderr)
        log = render_depthflow(str(img_path), str(out_mp4), args.time, args.height)
        print(log, file=sys.stderr)

        if not out_mp4.exists() or out_mp4.stat().st_size < 1024:
            print("ERROR: DepthFlow produced no output or empty file", file=sys.stderr)
            sys.exit(1)

        # ── 3. Upload to MinIO ─────────────────────────────────────────────────
        object_key = f"{DEPTHFLOW_PREFIX}/{args.slug}.mp4"
        print(f"Uploading → {object_key}", file=sys.stderr)
        client = init_minio()
        public_url = upload_to_minio(client, str(out_mp4), object_key)
        size_kb = out_mp4.stat().st_size // 1024

        # ── 4. Print manifest JSON to stdout ──────────────────────────────────
        manifest = [{
            "slug":   args.slug,
            "key":    object_key,
            "url":    public_url,
            "size_kb": size_kb,
            "duration": args.time,
            "height": args.height,
            "estimator": "da1",
        }]
        print(json.dumps(manifest))


if __name__ == "__main__":
    main()
