#!/usr/bin/env python3
"""
PuppetAgent Studio — Video production management API + dashboard.

Runs on your host machine at port 9005 as a systemd user service.
Accessible internally (LAN) or via Traefik → puppetagent.com (with basic auth).

API:
  GET    /                           → serve index.html
  GET    /static/<file>              → static assets
  GET    /api/jobs                   → list jobs (?status=X&limit=N)
  GET    /api/jobs/<id>              → job detail + MinIO URLs
  POST   /api/upload                 → upload raw video → create job
  POST   /api/jobs/script            → create job from text script (agentic)
  POST   /api/jobs/<id>/process      → start Stage 1
  POST   /api/jobs/<id>/process2     → start Stage 2
  POST   /api/jobs/<id>/review       → submit rating (≥1 star → triggers Stage 2)
  PATCH  /api/jobs/<id>/options      → update feature flags / title
  POST   /api/jobs/<id>/reject       → reject job
  GET    /api/jobs/<id>/progress     → SSE progress stream (polls /tmp/<id>-progress.json)
  GET    /api/jobs/<id>/logs         → last 100 log lines
"""

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, abort, jsonify, request,
                   send_from_directory, stream_with_context)
from minio import Minio
from minio.error import S3Error

app = Flask(__name__, static_folder="static", static_url_path="/static")

# ─── Config ────────────────────────────────────────────────────────────────────
# All PUPPETAGENT_* values can be set in ~/.puppetagent/puppetagent.conf or as env vars.
# Env vars always win. Docker users can pass everything via docker-compose environment:.
# See puppetagent.conf.example for the full list of options.

def _load_config() -> dict:
    conf_path = os.environ.get(
        "PUPPETAGENT_CONF",
        str(Path.home() / ".puppetagent" / "puppetagent.conf"),
    )
    cfg = {}
    for path in [conf_path, "/etc/puppetagent/puppetagent.conf"]:
        if Path(path).exists():
            for line in Path(path).read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg.setdefault(k.strip(), v.strip())
            break
    for k, v in os.environ.items():
        if k.startswith("PUPPETAGENT_"):
            cfg[k] = v
    return cfg


def _c(key: str, default: str) -> str:
    return _cfg.get(key, default)


_cfg = _load_config()

DATA_DIR        = Path.home() / "PuppetAgent" / "data"
DB_PATH         = DATA_DIR / "puppetagent.db"
TMP             = Path("/tmp")

MINIO_ENDPOINT  = _c("PUPPETAGENT_MINIO_ENDPOINT", "localhost:9004")
MINIO_ACCESS    = _c("PUPPETAGENT_MINIO_ACCESS",   "minio_admin")
MINIO_SECRET    = _c("PUPPETAGENT_MINIO_SECRET",   "changeme")
MINIO_BUCKET    = _c("PUPPETAGENT_MINIO_BUCKET",   "video-production")
MINIO_PUBLIC    = _c("PUPPETAGENT_MINIO_PUBLIC",   "https://studio.puppetagent.com/videos")

PROCESSOR_SCRIPT = _c("PUPPETAGENT_PROCESSOR_SCRIPT",
                       str(Path.home() / "scripts" / "video-processor.py"))
PROCESSOR_PYTHON = "/usr/bin/python3"          # video-processor.py uses only stdlib

UPLOAD_MAX_MB   = 500

# ─── MinIO ─────────────────────────────────────────────────────────────────────

minio_client = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS,
                     secret_key=MINIO_SECRET, secure=False)


def ensure_bucket():
    try:
        if not minio_client.bucket_exists(MINIO_BUCKET):
            minio_client.make_bucket(MINIO_BUCKET)
            # Set public-read policy
            policy = json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"AWS": ["*"]},
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{MINIO_BUCKET}/*"]
                }]
            })
            minio_client.set_bucket_policy(MINIO_BUCKET, policy)
    except S3Error as e:
        print(f"Warning: MinIO bucket check failed: {e}")


def minio_upload(local_path: str, key: str):
    minio_client.fput_object(MINIO_BUCKET, key, local_path,
                              content_type="video/mp4")


def minio_public_url(key: str) -> str:
    return f"{MINIO_PUBLIC}/{MINIO_BUCKET}/{key}"


# ─── DB ────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
          job_id            TEXT PRIMARY KEY,
          influencer        TEXT NOT NULL DEFAULT 'emma',
          title             TEXT DEFAULT '',
          script            TEXT DEFAULT '',
          status            TEXT DEFAULT 'uploaded',
          raw_key           TEXT,
          preview_key       TEXT,
          final_key         TEXT,
          opt_auto_edit     INTEGER DEFAULT 1,
          opt_voice_enhance INTEGER DEFAULT 1,
          opt_face_swap     INTEGER DEFAULT 1,
          opt_lip_sync      INTEGER DEFAULT 1,
          opt_gaze_correct  INTEGER DEFAULT 1,
          opt_bg_replace    INTEGER DEFAULT 0,
          rating            INTEGER DEFAULT 0,
          comment           TEXT DEFAULT '',
          drive_link        TEXT DEFAULT '',
          chat_id           TEXT DEFAULT '',
          error_msg         TEXT DEFAULT '',
          created_at        TEXT,
          updated_at        TEXT
        );
        CREATE TABLE IF NOT EXISTS job_logs (
          id      INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id  TEXT REFERENCES jobs(job_id),
          ts      TEXT,
          line    TEXT
        );
        """)
        # Migrate existing DBs that predate the script column
        try:
            db.execute("ALTER TABLE jobs ADD COLUMN script TEXT DEFAULT ''")
        except Exception:
            pass  # column already exists


_db_lock = threading.Lock()


def _now():
    return datetime.utcnow().isoformat()


def create_job_record(job_id, influencer, title, chat_id, raw_key=None,
                      script='', initial_status='uploaded'):
    now = _now()
    with _db_lock, get_db() as db:
        db.execute("""
            INSERT INTO jobs (job_id, influencer, title, chat_id, raw_key,
                              script, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (job_id, influencer, title, chat_id, raw_key, script,
              initial_status, now, now))


def update_job(job_id: str, **fields):
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [job_id]
    with _db_lock, get_db() as db:
        db.execute(f"UPDATE jobs SET {cols} WHERE job_id=?", vals)


def log_line(job_id: str, line: str):
    with _db_lock, get_db() as db:
        db.execute("INSERT INTO job_logs (job_id, ts, line) VALUES (?, ?, ?)",
                   (job_id, _now(), line.rstrip()))


def get_job(job_id: str) -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_jobs(status=None, limit=50) -> list:
    with get_db() as db:
        if status:
            rows = db.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_logs(job_id: str, limit=100) -> list:
    with get_db() as db:
        rows = db.execute(
            "SELECT ts, line FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT ?",
            (job_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]


# ─── Job enrichment ────────────────────────────────────────────────────────────

def enrich_job(job: dict) -> dict:
    """Add MinIO public URLs for each key field."""
    for url_field, key_field in [
        ("raw_url",     "raw_key"),
        ("preview_url", "preview_key"),
        ("final_url",   "final_key"),
    ]:
        key = job.get(key_field)
        job[url_field] = minio_public_url(key) if key else None
    return job


# ─── Pipeline ──────────────────────────────────────────────────────────────────

_running: dict[str, subprocess.Popen] = {}   # job_id → Popen
_running_lock = threading.Lock()


def _build_stage1_cmd(job: dict) -> list:
    j = job
    raw_path     = str(TMP / f"{j['job_id']}-raw.mp4")
    preview_path = str(TMP / f"{j['job_id']}-preview.mp4")
    cmd = [
        PROCESSOR_PYTHON, PROCESSOR_SCRIPT,
        "--influencer", j["influencer"],
        "--job-id",     j["job_id"],
        "--output",     preview_path,
        "--input-video", raw_path,
        "--preview-only",
    ]
    if j.get("opt_voice_enhance"):
        cmd.append("--clone-voice")
    if not j.get("opt_auto_edit"):
        cmd.append("--no-auto-edit")
    if not j.get("opt_face_swap"):
        cmd.append("--no-face-swap")   # video-processor.py skips if flag present
    return cmd


def _build_stage2_cmd(job: dict) -> list:
    j = job
    final_path = str(TMP / f"{j['job_id']}-final.mp4")
    cmd = [
        PROCESSOR_PYTHON, PROCESSOR_SCRIPT,
        "--influencer", j["influencer"],
        "--job-id",     j["job_id"],
        "--output",     final_path,
        "--resume",
    ]
    if not j.get("opt_gaze_correct"):
        cmd.append("--no-gaze")
    if not j.get("opt_bg_replace"):
        cmd.append("--no-bg")
    if not j.get("opt_lip_sync"):
        cmd.append("--no-musetalk")    # video-processor.py skips MuseTalk if flag present
    return cmd


def _run_in_thread(job_id: str, cmd: list, stage: int):
    """Spawn video-processor.py in a background thread; capture logs and handle completion."""
    def _worker():
        update_job(job_id, status=f"stage{stage}_running", error_msg="")
        log_line(job_id, f"=== Stage {stage} started ===")
        log_line(job_id, f"CMD: {' '.join(cmd)}")
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            with _running_lock:
                _running[job_id] = proc
            for raw_line in proc.stdout:
                log_line(job_id, raw_line)
            proc.wait()
            with _running_lock:
                _running.pop(job_id, None)

            if proc.returncode == 0:
                _on_stage_complete(job_id, stage)
            else:
                update_job(job_id, status="error",
                           error_msg=f"Stage {stage} exited with code {proc.returncode}")
                log_line(job_id, f"=== Stage {stage} FAILED (exit {proc.returncode}) ===")
        except Exception as e:
            with _running_lock:
                _running.pop(job_id, None)
            update_job(job_id, status="error", error_msg=str(e))
            log_line(job_id, f"=== EXCEPTION: {e} ===")

    threading.Thread(target=_worker, daemon=True, name=f"stage{stage}-{job_id}").start()


def _on_stage_complete(job_id: str, stage: int):
    """Upload output to MinIO and update job status after successful stage."""
    if stage == 1:
        preview_path = TMP / f"{job_id}-preview.mp4"
        if preview_path.exists() and preview_path.stat().st_size > 100_000:
            key = f"preview/{job_id}-preview.mp4"
            try:
                minio_upload(str(preview_path), key)
                update_job(job_id, status="awaiting_review", preview_key=key)
                log_line(job_id, f"Preview uploaded: {minio_public_url(key)}")
            except Exception as e:
                update_job(job_id, status="error",
                           error_msg=f"MinIO upload failed: {e}")
        else:
            update_job(job_id, status="error",
                       error_msg="Stage 1 complete but preview file missing or too small")

    elif stage == 2:
        final_path = TMP / f"{job_id}-final.mp4"
        if final_path.exists() and final_path.stat().st_size > 100_000:
            key = f"final/{job_id}-final.mp4"
            try:
                minio_upload(str(final_path), key)
                update_job(job_id, status="done", final_key=key)
                log_line(job_id, f"Final uploaded: {minio_public_url(key)}")
                # Clean up /tmp files for this job
                for f in TMP.glob(f"{job_id}-*"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
            except Exception as e:
                update_job(job_id, status="error",
                           error_msg=f"MinIO upload failed: {e}")
        else:
            update_job(job_id, status="error",
                       error_msg="Stage 2 complete but final file missing or too small")


# ─── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/jobs", methods=["GET"])
def api_list_jobs():
    status = request.args.get("status")
    limit  = int(request.args.get("limit", 50))
    jobs   = [enrich_job(j) for j in list_jobs(status, limit)]
    return jsonify(jobs)


@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_get_job(job_id):
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    running = job_id in _running
    result  = enrich_job(job)
    result["is_running"] = running
    return jsonify(result)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Receive raw video upload → save to MinIO → create or update job record.

    If job_id is provided and that job has status='queued', the video is linked
    to the existing script job (performer path). Otherwise a new job is created.
    """
    influencer  = request.form.get("influencer", "emma")
    title       = request.form.get("title", "")
    chat_id     = request.form.get("chat_id", "")
    auto_start  = request.form.get("auto_start", "0") == "1"
    link_job_id = request.form.get("job_id", "").strip()

    if "video" not in request.files:
        abort(400, description="No video file provided")

    # Performer path: link video to an existing queued script job
    if link_job_id:
        existing = get_job(link_job_id)
        if not existing:
            abort(404, description=f"Job {link_job_id} not found")
        if existing["status"] != "queued":
            abort(409, description=f"Job {link_job_id} is not in queued status (status={existing['status']})")

        job_id   = link_job_id
        raw_key  = f"raw/{job_id}-raw.mp4"
        raw_path = str(TMP / f"{job_id}-raw.mp4")

        request.files["video"].save(raw_path)

        try:
            minio_upload(raw_path, raw_key)
        except Exception as e:
            Path(raw_path).unlink(missing_ok=True)
            abort(500, description=f"MinIO upload failed: {e}")

        # Restore script file to /tmp if it was cleaned up
        script_path = TMP / f"{job_id}-script.txt"
        if not script_path.exists() and existing.get("script"):
            script_path.write_text(existing["script"])

        update_job(job_id, raw_key=raw_key, status="uploaded")

        if auto_start:
            job = get_job(job_id)
            _run_in_thread(job_id, _build_stage1_cmd(job), 1)

        return jsonify({
            "job_id": job_id,
            "status": "stage1_running" if auto_start else "uploaded",
            "raw_url": minio_public_url(raw_key),
        }), 200

    # Standard path: create a new job from a raw video upload
    job_id   = f"{influencer[:3]}-{uuid.uuid4().hex[:8]}"
    raw_key  = f"raw/{job_id}-raw.mp4"
    raw_path = str(TMP / f"{job_id}-raw.mp4")

    request.files["video"].save(raw_path)

    try:
        minio_upload(raw_path, raw_key)
    except Exception as e:
        Path(raw_path).unlink(missing_ok=True)
        abort(500, description=f"MinIO upload failed: {e}")

    create_job_record(job_id, influencer, title, chat_id, raw_key)

    if auto_start:
        job = get_job(job_id)
        _run_in_thread(job_id, _build_stage1_cmd(job), 1)

    return jsonify({
        "job_id": job_id,
        "status": "stage1_running" if auto_start else "uploaded",
        "raw_url": minio_public_url(raw_key),
    }), 201


@app.route("/api/jobs/script", methods=["POST"])
def api_create_from_script():
    """Create job from text script — agentic Pipeline A path."""
    data       = request.get_json(force=True)
    influencer = data.get("influencer", "emma")
    script     = data.get("script", "").strip()
    title      = data.get("title", "")
    chat_id    = data.get("chat_id", "")
    auto_start = data.get("auto_start", True)

    if not script:
        abort(400, description="script is required")

    job_id      = f"{influencer[:3]}-{uuid.uuid4().hex[:8]}"
    script_path = str(TMP / f"{job_id}-script.txt")
    Path(script_path).write_text(script)

    initial_status = 'uploaded' if auto_start else 'queued'
    create_job_record(job_id, influencer, title, chat_id,
                      script=script, initial_status=initial_status)

    if auto_start:
        preview_path = str(TMP / f"{job_id}-preview.mp4")
        cmd = [
            PROCESSOR_PYTHON, PROCESSOR_SCRIPT,
            "--influencer",  influencer,
            "--job-id",      job_id,
            "--output",      preview_path,
            "--script-file", script_path,
            "--preview-only",
        ]
        _run_in_thread(job_id, cmd, 1)

    return jsonify({"job_id": job_id, "status": "stage1_running" if auto_start else "queued"}), 201


@app.route("/api/jobs/<job_id>/process", methods=["POST"])
def api_process_stage1(job_id):
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    with _running_lock:
        if job_id in _running:
            abort(409, description="Job already running")

    # Fetch raw from MinIO to /tmp if not already present
    raw_path = TMP / f"{job_id}-raw.mp4"
    if not raw_path.exists() and job.get("raw_key"):
        try:
            minio_client.fget_object(MINIO_BUCKET, job["raw_key"], str(raw_path))
        except S3Error as e:
            abort(500, description=f"Could not fetch raw video from MinIO: {e}")

    _run_in_thread(job_id, _build_stage1_cmd(job), 1)
    return jsonify({"job_id": job_id, "status": "stage1_running"})


@app.route("/api/jobs/<job_id>/process2", methods=["POST"])
def api_process_stage2(job_id):
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    with _running_lock:
        if job_id in _running:
            abort(409, description="Job already running")

    # Fetch preview from MinIO to /tmp as the swap source for Stage 2
    # (video-processor.py --resume reads /tmp/<job>-swapped.mp4 written by Stage 1)
    _run_in_thread(job_id, _build_stage2_cmd(job), 2)
    return jsonify({"job_id": job_id, "status": "stage2_running"})


@app.route("/api/jobs/<job_id>/review", methods=["POST"])
def api_review(job_id):
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")

    data    = request.get_json(force=True)
    rating  = max(0, min(5, int(data.get("rating", 0))))
    comment = data.get("comment", "")

    update_job(job_id, rating=rating, comment=comment)
    log_line(job_id, f"Review: {rating}/5 — {comment or '(no comment)'}")

    # Rating ≥ 1 on awaiting_review → trigger Stage 2
    if rating >= 1 and job["status"] == "awaiting_review":
        with _running_lock:
            if job_id not in _running:
                _run_in_thread(job_id, _build_stage2_cmd(get_job(job_id)), 2)

    return jsonify({"ok": True, "rating": rating})


@app.route("/api/jobs/<job_id>/options", methods=["PATCH"])
def api_update_options(job_id):
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")

    data = request.get_json(force=True)
    allowed = {
        "opt_auto_edit", "opt_voice_enhance", "opt_face_swap",
        "opt_lip_sync", "opt_gaze_correct", "opt_bg_replace", "title",
    }
    fields = {k: v for k, v in data.items() if k in allowed}
    if fields:
        update_job(job_id, **fields)
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/reject", methods=["POST"])
def api_reject(job_id):
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    update_job(job_id, status="rejected")
    log_line(job_id, "Job rejected by reviewer")
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/progress")
def api_progress(job_id):
    """SSE stream — polls /tmp/<job_id>-progress.json every 2s."""
    def _generate():
        last_pct = -1
        no_change = 0

        while no_change < 90:   # give up after 3 min with no change
            pf = TMP / f"{job_id}-progress.json"
            if pf.exists():
                try:
                    data = json.loads(pf.read_text())
                    pct  = data.get("pct", 0)
                    if pct != last_pct:
                        last_pct  = pct
                        no_change = 0
                        yield f"data: {json.dumps(data)}\n\n"
                    else:
                        no_change += 1
                except Exception:
                    no_change += 1
            else:
                # No progress file — check if job reached a terminal state
                job = get_job(job_id)
                if job and job["status"] in ("awaiting_review", "done", "error", "rejected"):
                    yield f"data: {json.dumps({'step': 'done', 'pct': 100, 'status': job['status']})}\n\n"
                    return
                no_change += 1

            time.sleep(2)

        yield f"data: {json.dumps({'step': 'timeout', 'pct': last_pct})}\n\n"

    return Response(
        stream_with_context(_generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/jobs/<job_id>/logs")
def api_logs(job_id):
    return jsonify(get_logs(job_id))


# ─── Error handlers ────────────────────────────────────────────────────────────

@app.errorhandler(400)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(500)
def handle_error(e):
    return jsonify({"error": str(e.description)}), e.code


# ─── Startup ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    try:
        ensure_bucket()
    except Exception as e:
        print(f"Warning: MinIO init failed: {e}")
    app.run(host="0.0.0.0", port=9005, threaded=True)
