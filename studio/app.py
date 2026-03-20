import re
#!/usr/bin/env python3
"""
PuppetAgent Studio — Video production management API + dashboard.

Runs on your server at port 9005 as a systemd user service.
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
  GET    /api/jobs/<id>/cuts         → get intervals [[start,end],...] (for agents)
  PUT    /api/jobs/<id>/cuts         → set intervals (agent-adjusted cuts, used by Stage 2)
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
MINIO_PUBLIC         = _c("PUPPETAGENT_MINIO_PUBLIC",         "https://studio.puppetagent.com/videos")
TELEPROMPTER_URL     = _c("PUPPETAGENT_TELEPROMPTER_URL",     "http://YOUR_TELEPROMPTER_HOST:8080")

PROCESSOR_SCRIPT = _c("PUPPETAGENT_PROCESSOR_SCRIPT",
                       str(Path.home() / "scripts" / "video-processor.py"))
PROCESSOR_PYTHON = "/usr/bin/python3"          # video-processor.py uses only stdlib

UPLOAD_MAX_MB   = 500
app.config["MAX_CONTENT_LENGTH"] = UPLOAD_MAX_MB * 1024 * 1024

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
        # Migrations for columns added after initial schema
        for col, defval in [
            ("script",           "TEXT DEFAULT ''"),
            ("srt_key",          "TEXT DEFAULT ''"),
            ("caption_style",    "TEXT DEFAULT 'none'"),
            ("caption_x",        "TEXT DEFAULT '50'"),
            ("caption_y",        "TEXT DEFAULT '85'"),
            ("caption_size",     "TEXT DEFAULT 'medium'"),
            ("opt_face_model",   "TEXT DEFAULT 'hyperswap_1a'"),
            ("opt_face_enhancer","TEXT DEFAULT 'none'"),
            ("opt_face_mode",    "TEXT DEFAULT 'facefusion'"),
            ("opt_face_mask_blur","TEXT DEFAULT '0.3'"),
            ("opt_face_mask_type","TEXT DEFAULT 'box'"),
            ("opt_voice_conv",   "TEXT DEFAULT 'none'"),
            ("intervals_json",   "TEXT DEFAULT '[]'"),
            ("waveform_json",    "TEXT DEFAULT '[]'"),
            ("vosk_transcript",  "TEXT DEFAULT ''"),
            ("script_lines",     "TEXT DEFAULT '[]'"),
            ("tracks_json",      "TEXT DEFAULT '[]'"),
        ]:
            try:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {defval}")
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
        ("srt_url",     "srt_key"),
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
    # Write context file for auto-edit if teleprompter data is available
    vosk_text   = j.get("vosk_transcript", "") or ""
    script_lns  = j.get("script_lines", "[]") or "[]"
    if vosk_text or script_lns not in ("[]", ""):
        ctx = {"vosk_transcript": vosk_text, "script_lines": script_lns}
        ctx_path = str(TMP / f"{j['job_id']}-context.json")
        Path(ctx_path).write_text(json.dumps(ctx))
        cmd += ["--context-file", ctx_path]

    if j.get("opt_voice_enhance"):
        cmd.append("--clone-voice")
    if not j.get("opt_auto_edit"):
        cmd.append("--no-auto-edit")
    face_model = j.get("opt_face_model", "hyperswap_1a") or "hyperswap_1a"
    cmd += ["--face-model", face_model]
    face_enhancer = j.get("opt_face_enhancer", "none") or "none"
    cmd += ["--face-enhancer", face_enhancer]
    voice_conv = j.get("opt_voice_conv", "none") or "none"
    if voice_conv != "none":
        cmd += ["--voice-conv", voice_conv]
    face_mode = j.get("opt_face_mode", "facefusion") or "facefusion"
    cmd += ["--face-mode", face_mode]
    mask_blur = j.get("opt_face_mask_blur", "0.3") or "0.3"
    cmd += ["--face-mask-blur", str(mask_blur)]
    mask_type = j.get("opt_face_mask_type", "box") or "box"
    cmd += ["--face-mask-type", mask_type]
    return cmd


def _ensure_srt_local(job: dict):
    """Download SRT from MinIO to /tmp before Stage 2 if srt_key is set."""
    srt_key = job.get("srt_key")
    if not srt_key:
        return
    srt_local = TMP / f"{job['job_id']}-subs.srt"
    if srt_local.exists() and srt_local.stat().st_size > 0:
        return  # already present from Stage 1
    try:
        minio_client.fget_object(MINIO_BUCKET, srt_key, str(srt_local))
        log_line(job["job_id"], f"SRT fetched from MinIO for caption burn-in")
    except Exception as e:
        log_line(job["job_id"], f"WARNING: Could not fetch SRT from MinIO: {e}")


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
        cmd.append("--no-musetalk")
    caption_style = j.get("caption_style") or "none"
    cmd += ["--caption-style", caption_style]
    caption_x    = j.get("caption_x")    or "50"
    caption_y    = j.get("caption_y")    or "85"
    caption_size = j.get("caption_size") or "medium"
    cmd += ["--caption-x", str(caption_x),
            "--caption-y", str(caption_y),
            "--caption-size", caption_size]
    # Pass cut intervals to Stage 2 so it can re-cut raw video before face swap
    ivs_json = j.get("intervals_json")
    if ivs_json:
        try:
            ivs = json.loads(ivs_json)
            if ivs:
                cuts_path = str(TMP / f"{j['job_id']}-cuts.json")
                import pathlib as _pl
                _pl.Path(cuts_path).write_text(json.dumps(ivs))
                cmd += ["--cuts-file", cuts_path]
        except Exception:
            pass
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


def extract_waveform(video_path: str) -> list:
    """Return per-50ms RMS energy as list of 0..1 floats. Empty list on failure."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", video_path,
             "-af", "astats=length=0.05:metadata=1,"
                    "ametadata=print:key=lavfi.astats.Overall.RMS_level",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=180)
        samples = []
        for line in r.stderr.split("\n"):
            if "RMS_level=" in line:
                val = line.split("=")[-1].strip()
                try:
                    db = float(val)   # may be -inf for silence
                    samples.append(round(max(0.0, min(1.0, (db + 60.0) / 60.0)), 3))
                except (ValueError, OverflowError):
                    samples.append(0.0)
        return samples
    except Exception:
        return []


def _srt_fmt(sec: float) -> str:
    ms = int(sec * 1000)
    h, r = divmod(ms, 3_600_000)
    m, r = divmod(r, 60_000)
    s, ms = divmod(r, 1_000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _generate_srt_content(segments: list, intervals: list,
                          words_per_card: int = 6) -> str:
    """Remap Whisper segment timestamps through new intervals and produce SRT text."""
    offsets = []
    new_t = 0.0
    for (os_, oe) in intervals:
        offsets.append((os_, oe, new_t))
        new_t += oe - os_

    def remap(t):
        for os_, oe, ns in offsets:
            if os_ - 0.1 <= t <= oe + 0.1:
                return max(0.0, ns + (t - os_))
        return None

    cards, word_buf = [], []
    for seg in segments:
        words = seg.get("words", [])
        if not words:
            t_s, t_e = remap(seg["start"]), remap(seg["end"])
            if t_s is not None and t_e is not None and t_e > t_s:
                cards.append((t_s, t_e, seg["text"].strip()))
            continue
        for w in words:
            t_s = remap(w.get("start", seg["start"]))
            if t_s is None:
                continue
            t_e = remap(w.get("end", w.get("start", 0) + 0.25)) or t_s + 0.25
            word_buf.append((w["word"].strip(), t_s, t_e))
            if len(word_buf) >= words_per_card:
                cards.append((word_buf[0][1], word_buf[-1][2],
                               " ".join(ww[0] for ww in word_buf)))
                word_buf = []
    if word_buf:
        cards.append((word_buf[0][1], word_buf[-1][2],
                      " ".join(ww[0] for ww in word_buf)))

    lines = []
    for i, (s, e, txt) in enumerate(cards, 1):
        lines += [str(i), f"{_srt_fmt(s)} --> {_srt_fmt(e)}", txt, ""]
    return "\n".join(lines)


def _on_stage_complete(job_id: str, stage: int):
    """Upload output to MinIO and update job status after successful stage."""
    if stage == 1:
        preview_path = TMP / f"{job_id}-preview.mp4"
        if preview_path.exists() and preview_path.stat().st_size > 100_000:
            key = f"preview/{job_id}-preview.mp4"
            try:
                minio_upload(str(preview_path), key)
                update_fields = {"status": "awaiting_review", "preview_key": key}
                # Upload SRT if auto-edit generated one
                srt_path = TMP / f"{job_id}-subs.srt"
                if srt_path.exists() and srt_path.stat().st_size > 0:
                    srt_key = f"subs/{job_id}.srt"
                    try:
                        minio_client.fput_object(MINIO_BUCKET, srt_key, str(srt_path),
                                                 content_type="text/srt")
                        update_fields["srt_key"] = srt_key
                        log_line(job_id, f"SRT uploaded: {minio_public_url(srt_key)}")
                    except Exception as e:
                        log_line(job_id, f"WARNING: SRT upload failed: {e}")
                # Extract intervals from transcript.json if available
                transcript_path = TMP / f"{job_id}-transcript.json"
                if transcript_path.exists():
                    try:
                        td = json.loads(transcript_path.read_text())
                        ivs = td.get("intervals", [])
                        if ivs:
                            update_fields["intervals_json"] = json.dumps(ivs)
                            log_line(job_id, f"Saved {len(ivs)} cut intervals")
                    except Exception as ex:
                        log_line(job_id, f"WARNING: interval extract failed: {ex}")

                # Extract waveform from raw video
                raw_path = TMP / f"{job_id}-raw.mp4"
                if raw_path.exists():
                    log_line(job_id, "Extracting waveform…")
                    wf = extract_waveform(str(raw_path))
                    if wf:
                        update_fields["waveform_json"] = json.dumps(wf)
                        log_line(job_id, f"Waveform: {len(wf)} samples ({len(wf)*0.05:.1f}s)")

                update_job(job_id, **update_fields)
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
    influencer       = request.form.get("influencer", "emma")
    title            = request.form.get("title", "")
    chat_id          = request.form.get("chat_id", "")
    auto_start       = request.form.get("auto_start", "0") == "1"
    link_job_id      = request.form.get("job_id", "").strip()
    vosk_transcript  = request.form.get("vosk_transcript", "")
    script_lines_raw = request.form.get("script_lines", "[]")

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

        update_job(job_id, raw_key=raw_key, status="uploaded",
                   vosk_transcript=vosk_transcript,
                   script_lines=script_lines_raw)

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
    if vosk_transcript or script_lines_raw != "[]":
        update_job(job_id, vosk_transcript=vosk_transcript,
                   script_lines=script_lines_raw)

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
    _ensure_srt_local(job)
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
                j2 = get_job(job_id)
                _ensure_srt_local(j2)
                _run_in_thread(job_id, _build_stage2_cmd(j2), 2)

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
        "caption_style", "caption_x", "caption_y", "caption_size",
        "opt_face_model", "opt_face_enhancer", "opt_face_mode",
        "opt_face_mask_blur", "opt_face_mask_type", "opt_voice_conv",
    }
    fields = {k: v for k, v in data.items() if k in allowed}
    if fields:
        update_job(job_id, **fields)
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>/fork", methods=["POST"])
def api_fork(job_id):
    """Duplicate a job (same settings + input video) for re-running with tweaks."""
    src = get_job(job_id)
    if not src:
        abort(404, description="Job not found")
    new_id = f"{src['influencer'][:3]}-{uuid.uuid4().hex[:8]}"
    create_job_record(
        new_id, src["influencer"],
        title   = f"[fork] {src.get('title','')}",
        chat_id = src.get("chat_id", ""),
        raw_key = src.get("raw_key"),
        script  = src.get("script", ""),
        initial_status = "uploaded"
    )
    import shutil as _sh_fork
    src_raw = TMP / f"{job_id}-raw.mp4"
    dst_raw = TMP / f"{new_id}-raw.mp4"
    if src_raw.exists():
        _sh_fork.copy2(src_raw, dst_raw)
    opt_fields = {k: v for k, v in src.items() if k.startswith("opt_")}
    if opt_fields:
        update_job(new_id, **opt_fields)
    log_line(new_id, f"Forked from {job_id}")
    return jsonify({"job_id": new_id, "status": "uploaded"})


@app.route("/api/jobs/<job_id>/cuts", methods=["GET"])
def api_get_cuts(job_id):
    """Return the current cut intervals [[start,end],...] for this job."""
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    try:
        intervals = json.loads(job.get("intervals_json") or "[]")
    except Exception:
        intervals = []
    return jsonify({"ok": True, "intervals": intervals})


@app.route("/api/jobs/<job_id>/cuts", methods=["PUT"])
def api_put_cuts(job_id):
    """Update cut intervals [[start,end],...] without triggering a re-encode.
    Stage 2 will use these cuts to re-cut the raw video before face swap."""
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    data = request.get_json(force=True, silent=True) or {}
    intervals = data.get("intervals")
    if not isinstance(intervals, list):
        abort(400, description="intervals required: [[start, end], ...]")
    update_job(job_id, intervals_json=json.dumps(intervals))
    return jsonify({"ok": True, "intervals": intervals})


@app.route("/api/jobs/<job_id>/reject", methods=["POST"])
def api_reject(job_id):
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    update_job(job_id, status="rejected")
    log_line(job_id, "Job rejected by reviewer")
    return jsonify({"ok": True})

@app.route("/api/jobs/<job_id>/script", methods=["PATCH"])
def api_update_script(job_id):
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    data   = request.get_json(force=True)
    script = data.get("script", "")
    update_job(job_id, script=script)
    # Keep /tmp script file in sync so a re-run picks up the edit
    script_path = TMP / f"{job_id}-script.txt"
    script_path.write_text(script)
    log_line(job_id, "Script edited by reviewer")
    return jsonify({"ok": True})


@app.route("/api/jobs/<job_id>", methods=["DELETE"])
def api_delete_job(job_id):
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")

    # Kill any running process
    with _running_lock:
        proc = _running.pop(job_id, None)
    if proc:
        try:
            proc.kill()
        except Exception:
            pass

    # Delete MinIO objects
    for key_field in ("raw_key", "preview_key", "final_key"):
        key = job.get(key_field)
        if key:
            try:
                minio_client.remove_object(MINIO_BUCKET, key)
            except Exception:
                pass

    # Clean up /tmp files
    for pattern in [f"{job_id}-*.mp4", f"{job_id}-*.wav",
                    f"{job_id}-*.txt", f"{job_id}-progress.json"]:
        for f in TMP.glob(pattern):
            f.unlink(missing_ok=True)

    # Remove from DB
    con = get_db()
    con.execute("DELETE FROM job_logs WHERE job_id = ?", (job_id,))
    con.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
    con.commit()

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


@app.route("/api/jobs/<job_id>/srt")
def api_srt(job_id):
    """Proxy SRT file from MinIO and return it as a download."""
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    srt_key = job.get("srt_key")
    if not srt_key:
        abort(404, description="No SRT file for this job (run Stage 1 first)")
    srt_local = TMP / f"{job_id}-subs-download.srt"
    try:
        minio_client.fget_object(MINIO_BUCKET, srt_key, str(srt_local))
        content = srt_local.read_text(encoding="utf-8")
        srt_local.unlink(missing_ok=True)
    except Exception as e:
        abort(500, description=f"Could not fetch SRT from MinIO: {e}")
    return Response(
        content,
        mimetype="text/srt",
        headers={"Content-Disposition": f'attachment; filename="{job_id}.srt"'},
    )


@app.route("/api/jobs/<job_id>/waveform")
def api_waveform(job_id):
    """Return the waveform JSON array (50ms RMS samples, 0..1) for a job."""
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    return jsonify(json.loads(job.get("waveform_json") or "[]"))


@app.route("/api/jobs/<job_id>/reencode", methods=["POST"])
def api_reencode(job_id):
    """Re-trim the preview using a new set of intervals (no re-transcription)."""
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    with _running_lock:
        if job_id in _running:
            abort(409, description="Job is already running")

    data = request.get_json(force=True)
    intervals = data.get("intervals")
    if not intervals or not isinstance(intervals, list) or len(intervals) == 0:
        abort(400, description="intervals required: [[start, end], ...]")

    def _do_reencode():
        import tempfile as _tf
        try:
            update_job(job_id, status="stage1_running")
            log_line(job_id, f"Re-encode: {len(intervals)} intervals requested")

            # Ensure raw video is in /tmp
            raw_path = TMP / f"{job_id}-raw.mp4"
            if not raw_path.exists() or raw_path.stat().st_size < 100_000:
                rk = job.get("raw_key")
                if not rk:
                    raise RuntimeError("No raw_key — cannot re-encode")
                minio_client.fget_object(MINIO_BUCKET, rk, str(raw_path))
                log_line(job_id, "Raw video fetched from MinIO")

            # Build ffmpeg filter_complex
            n = len(intervals)
            parts = []
            for i, (s, e) in enumerate(intervals):
                parts.append(f"[0:v]trim=start={float(s):.4f}:end={float(e):.4f},setpts=PTS-STARTPTS[v{i}];")
                parts.append(f"[0:a]atrim=start={float(s):.4f}:end={float(e):.4f},asetpts=PTS-STARTPTS[a{i}];")
            va = "".join(f"[v{i}][a{i}]" for i in range(n))
            parts.append(f"{va}concat=n={n}:v=1:a=1[outv][outa]")

            fc_tmp = Path(_tf.mktemp(suffix=".txt"))
            fc_tmp.write_text("".join(parts))

            preview_path = TMP / f"{job_id}-preview.mp4"
            preview_path.unlink(missing_ok=True)

            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(raw_path),
                 "-filter_complex_script", str(fc_tmp),
                 "-map", "[outv]", "-map", "[outa]",
                 "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                 "-c:a", "aac", "-b:a", "128k",
                 str(preview_path)],
                capture_output=True, text=True, timeout=180)
            fc_tmp.unlink(missing_ok=True)

            if r.returncode != 0 or not preview_path.exists() or \
               preview_path.stat().st_size < 10_000:
                raise RuntimeError(f"ffmpeg re-encode failed:\n{r.stderr[-500:]}")

            log_line(job_id, f"Re-encoded OK — {preview_path.stat().st_size//1024}KB")

            # Upload new preview
            pkey = f"preview/{job_id}-preview.mp4"
            minio_upload(str(preview_path), pkey)

            update_fields = {
                "preview_key":    pkey,
                "intervals_json": json.dumps(intervals),
                "status":         "awaiting_review",
            }

            # Regenerate SRT from existing transcript if available
            transcript_path = TMP / f"{job_id}-transcript.json"
            if transcript_path.exists():
                try:
                    td = json.loads(transcript_path.read_text())
                    segs = td.get("segments", [])
                    if segs:
                        srt_content = _generate_srt_content(segs, intervals)
                        srt_path = TMP / f"{job_id}-subs.srt"
                        srt_path.write_text(srt_content, encoding="utf-8")
                        srt_key = f"subs/{job_id}.srt"
                        minio_client.fput_object(MINIO_BUCKET, srt_key, str(srt_path),
                                                 content_type="text/srt")
                        update_fields["srt_key"] = srt_key
                        log_line(job_id, "SRT regenerated with new intervals")
                except Exception as ex:
                    log_line(job_id, f"WARNING: SRT regen failed: {ex}")

            update_job(job_id, **update_fields)
            log_line(job_id, "Re-encode complete — preview updated")

        except Exception as ex:
            update_job(job_id, status="awaiting_review",
                       error_msg=f"Re-encode failed: {ex}")
            log_line(job_id, f"Re-encode FAILED: {ex}")

    threading.Thread(target=_do_reencode, daemon=True,
                     name=f"reencode-{job_id}").start()
    return jsonify({"ok": True, "status": "stage1_running"})


@app.route("/api/jobs/<job_id>/logs")
def api_logs(job_id):
    return jsonify(get_logs(job_id))


@app.route("/api/jobs/<job_id>/teleprompter", methods=["POST"])
def api_push_teleprompter(job_id):
    """Push this job's script to the teleprompter server so the phone picks it up."""
    import urllib.request as _ureq
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    script = job.get("script", "").strip()
    if not script:
        abort(400, description="Job has no script to push")
    script_id = f"{job['influencer']}-{job_id}"
    payload = json.dumps({
        "script_id":  script_id,
        "title":      job.get("title") or job_id,
        "body":       script,
        "influencer": job.get("influencer", "emma"),
        "set_current": True,
    }).encode()
    try:
        req = _ureq.Request(
            f"{TELEPROMPTER_URL}/scripts",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with _ureq.urlopen(req, timeout=5) as resp:
            json.loads(resp.read())
        log_line(job_id, f"Script pushed to teleprompter: {script_id}")
        return jsonify({"ok": True, "script_id": script_id})
    except Exception as e:
        abort(502, description=f"Teleprompter unreachable: {e}")


@app.route("/api/rvc-models")
def api_rvc_models():
    rvc_dir = Path.home() / "models/rvc"
    models = []
    if rvc_dir.exists():
        for pth in sorted(rvc_dir.glob("*/*.pth")):
            models.append(pth.parent.name)
    return jsonify(models)


@app.route("/api/rvc-models/upload", methods=["POST"])
def api_rvc_upload():
    """Upload a custom RVC .pth model file."""
    if "file" not in request.files:
        abort(400, description="No file provided")
    f = request.files["file"]
    name = (request.form.get("name") or f.filename or "").strip()
    name = re.sub(r"\.pth$", "", name, flags=re.I)
    name = re.sub(r"[^\w\-]", "_", name)[:64].strip("_")
    if not name:
        abort(400, description="Invalid model name")
    model_dir = Path.home() / "models/rvc" / name
    model_dir.mkdir(parents=True, exist_ok=True)
    dest = model_dir / f"{name}.pth"
    f.save(str(dest))
    return jsonify({"name": name, "status": "ok"})


# ─── B-roll track endpoints ────────────────────────────────────────────────────

@app.route("/api/jobs/<job_id>/tracks", methods=["GET"])
def api_list_tracks(job_id):
    """List all B-roll tracks attached to this job."""
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    tracks = json.loads(job.get("tracks_json") or "[]")
    for t in tracks:
        t["url"] = minio_public_url(t["minio_key"])
    return jsonify(tracks)


@app.route("/api/jobs/<job_id>/tracks", methods=["POST"])
def api_add_track(job_id):
    """Add a B-roll track.

    Two modes:
      A) multipart/form-data with field 'file'  — direct upload (dashboard)
      B) JSON body with field 'url'              — fetch from URL (agent-friendly)

    Optional params (form fields or JSON keys):
      display_mode  pip | overlay  (default: pip)
      x, y          0.0–1.0 normalized position  (default: 0.70, 0.05)
      w, h          0.0–1.0 normalized size       (default: 0.28, 0.28)
      start_t       seconds — when clip appears   (default: 0.0)
      end_t         seconds — when clip ends       (default: 9999)
      opacity       0.0–1.0                        (default: 1.0)
      border_radius px, cosmetic                   (default: 8)
    """
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")

    track_id  = uuid.uuid4().hex[:8]
    minio_key = f"broll/{job_id}-{track_id}.mp4"
    tmp_path  = TMP / f"{job_id}-broll-{track_id}.mp4"

    if "file" in request.files:
        params = request.form
        request.files["file"].save(str(tmp_path))
    else:
        import urllib.request as _ureq_broll
        data    = request.get_json(force=True) or {}
        params  = data
        src_url = data.get("url", "").strip()
        if not src_url:
            abort(400, description="Provide 'file' upload or JSON body with 'url'")
        try:
            with _ureq_broll.urlopen(src_url, timeout=60) as resp:
                tmp_path.write_bytes(resp.read())
        except Exception as e:
            abort(502, description=f"Could not fetch B-roll from URL: {e}")

    try:
        minio_upload(str(tmp_path), minio_key)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        abort(500, description=f"MinIO upload failed: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)

    tracks = json.loads(job.get("tracks_json") or "[]")
    entry = {
        "track_id":      track_id,
        "type":          "video",
        "minio_key":     minio_key,
        "display_mode":  str(params.get("display_mode", "pip")),
        "x":             float(params.get("x",            0.70)),
        "y":             float(params.get("y",            0.05)),
        "w":             float(params.get("w",            0.28)),
        "h":             float(params.get("h",            0.28)),
        "start_t":       float(params.get("start_t",      0.0)),
        "end_t":         float(params.get("end_t",        9999.0)),
        "opacity":       float(params.get("opacity",      1.0)),
        "border_radius": int(params.get("border_radius",  8)),
    }
    tracks.append(entry)
    update_job(job_id, tracks_json=json.dumps(tracks))
    log_line(job_id, f"B-roll track added: {track_id} ({entry['display_mode']})")
    return jsonify({
        "track_id":  track_id,
        "minio_key": minio_key,
        "url":       minio_public_url(minio_key),
    }), 201


@app.route("/api/jobs/<job_id>/tracks/<track_id>", methods=["PATCH"])
def api_update_track(job_id, track_id):
    """Update position/timing/mode for a B-roll track."""
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    data   = request.get_json(force=True)
    tracks = json.loads(job.get("tracks_json") or "[]")
    track  = next((t for t in tracks if t["track_id"] == track_id), None)
    if not track:
        abort(404, description="Track not found")
    for k in ("display_mode", "x", "y", "w", "h", "start_t", "end_t", "opacity", "border_radius"):
        if k in data:
            track[k] = data[k]
    update_job(job_id, tracks_json=json.dumps(tracks))
    return jsonify({"ok": True, "track": track})


@app.route("/api/jobs/<job_id>/tracks/<track_id>", methods=["DELETE"])
def api_delete_track(job_id, track_id):
    """Remove a B-roll track and delete its MinIO object."""
    job = get_job(job_id)
    if not job:
        abort(404, description="Job not found")
    tracks = json.loads(job.get("tracks_json") or "[]")
    track  = next((t for t in tracks if t["track_id"] == track_id), None)
    if not track:
        abort(404, description="Track not found")
    try:
        minio_client.remove_object(MINIO_BUCKET, track["minio_key"])
    except Exception:
        pass
    tracks = [t for t in tracks if t["track_id"] != track_id]
    update_job(job_id, tracks_json=json.dumps(tracks))
    log_line(job_id, f"B-roll track removed: {track_id}")
    return jsonify({"ok": True})


# ─── B-roll fetch API (async) ─────────────────────────────────────────────────
# Agents POST to start a fetch job; poll GET /api/broll/status/<token>.
# Called via: POST https://studio.puppetagent.com/api/broll/fetch
# Auth: HTTP Basic auth (configure in puppetagent.conf).

_broll_jobs: dict = {}   # token -> {status, manifest, log, error}


def _run_broll(token: str, cmd: list):
    import re as _re
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        matches = _re.findall(r'\[\s*\{.*?\}\s*\]', result.stdout, _re.DOTALL)
        manifest = json.loads(matches[-1]) if matches else []
        _broll_jobs[token] = {"status": "done", "manifest": manifest,
                              "log": result.stdout[-2000:]}
    except subprocess.TimeoutExpired:
        _broll_jobs[token] = {"status": "error", "error": "broll-fetch.py timed out (300s)"}
    except Exception as exc:
        _broll_jobs[token] = {"status": "error", "error": str(exc)}


@app.route("/api/broll/fetch", methods=["POST"])
def broll_fetch():
    """Start async broll fetch. Returns {token} immediately; poll /api/broll/status/<token>."""
    data = request.get_json(force=True, silent=True) or {}
    script = os.path.expanduser("~/scripts/broll-fetch.py")
    cmd = ["python3", script]

    query = data.get("query", "")
    if query:
        cmd += ["--query", query]
        sources = data.get("sources", "pexels,pixabay")
        count = int(data.get("count", 2))
        tag = data.get("tag", "")
        cmd += ["--sources", sources, "--count", str(count)]
        if tag:
            cmd += ["--tag", tag]

    youtube = data.get("youtube", "")
    if youtube:
        cmd += ["--youtube", youtube]
        if data.get("start"):
            cmd += ["--start", data["start"]]
        if data.get("end"):
            cmd += ["--end", data["end"]]

    if not query and not youtube:
        return jsonify({"error": "Provide 'query' or 'youtube' in request body"}), 400

    token = str(uuid.uuid4())
    _broll_jobs[token] = {"status": "running"}
    t = threading.Thread(target=_run_broll, args=(token, cmd), daemon=True)
    t.start()
    return jsonify({"ok": True, "token": token, "poll": f"/api/broll/status/{token}"})


@app.route("/api/broll/status/<token>", methods=["GET"])
def broll_status(token):
    """Poll for async broll result. Returns status=running|done|error."""
    job = _broll_jobs.get(token)
    if job is None:
        return jsonify({"error": "Unknown token"}), 404
    return jsonify(job)



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
