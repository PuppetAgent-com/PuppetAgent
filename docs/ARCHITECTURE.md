# PuppetAgent — Architecture

Video production management dashboard and API. Handles job lifecycle from raw
upload through Stage 1 (face swap + audio) and Stage 2 (lip sync + polish),
with live progress streaming and a browser-based review interface.

---

## System diagram

```
Browser
  │  https://puppetagent.com/          (basic auth)
  │  https://puppetagent.com/videos/*  (no auth — UUID security)
  ▼
Cloudflare Tunnel
  ▼
Traefik (Docker, proxy network)
  ├── /videos/* → minio-video:9000   (strip /videos prefix, forward to MinIO)
  └── /*        → host:9005          (Flask studio app)
       ▼
  Flask app (systemd on host, ~/PuppetAgent/)
  ├── SQLite job DB (~/PuppetAgent/data/puppetagent.db)
  └── Spawns video-processor.py as subprocess
       ▼
  video-processor.py (~/scripts/, conda envs, GPU tools)
  ├── FaceFusion   (conda env: facefusion)
  ├── MuseTalk     (conda env: musetalk)
  ├── XTTS         (venv: ~/venvs/xtts/)
  └── DeepFilterNet, Whisper, MediaPipe
```

### Why Flask runs on the host, not in Docker

`video-processor.py` uses GPU tools installed in conda environments on the host
(FaceFusion, MuseTalk, XTTS). Running these inside Docker would require GPU
passthrough, conda inside Docker, and complex volume mounts — significant added
complexity for no benefit at current scale.

The pattern mirrors Ollama: the processing tools install once on the host, the
web layer (Flask) talks to them. For self-hosters, this is the only manual step.

### Why /videos has no basic auth

Browser `<video src="...">` tags cannot send `Authorization` headers. Any auth
on the video path would silently block playback. Security instead comes from
UUIDs in filenames (128-bit entropy, not guessable) which are only revealed
inside the auth-protected dashboard.

---

## Routing (Traefik)

Config: `~/docker/traefik/dynamic/puppetagent.yml` (file provider, hot-reloaded)

```
Request: GET /videos/video-production/preview/<uuid>-preview.mp4
  Traefik strips: /videos
  MinIO receives: GET /video-production/preview/<uuid>-preview.mp4
  Response: 206 Partial Content (range requests supported — video seeking works)

Request: GET /api/jobs
  Traefik: check basicAuth → forward to 172.18.0.1:9005
  Flask responds: JSON job list
```

**Equivalent nginx config** (for self-hosters not using Traefik):
```nginx
server {
    listen 80;
    server_name puppetagent.example.com;

    location /videos/ {
        rewrite ^/videos/(.*)$ /$1 break;
        proxy_pass http://minio:9000;
        proxy_set_header Host minio:9000;
    }

    location / {
        auth_basic "PuppetAgent Studio";
        auth_basic_user_file /etc/nginx/.htpasswd;
        proxy_pass http://localhost:9005;
    }
}
```

---

## File layout (production)

```
~/PuppetAgent/
├── app.py                    Flask app
├── static/
│   ├── index.html            Dashboard SPA
│   ├── app.js                Vanilla JS — job cards, SSE, upload modal
│   └── style.css             Dark theme
└── data/
    └── puppetagent.db        SQLite WAL — jobs + job_logs tables

~/docker/traefik/dynamic/
└── puppetagent.yml           Traefik file-provider config (hot-reloaded)

~/.config/systemd/user/
└── puppetagent.service       systemd user service
```

---

## Job lifecycle

```
POST /api/upload  →  status: uploaded
                 ↓  (auto_start=1)
             stage1_running   (video-processor.py --preview-only)
                 ↓
           awaiting_review    (preview in MinIO, SSE done)
                 ↓  (POST /api/jobs/<id>/review with rating ≥ 1)
             stage2_running   (video-processor.py --resume)
                 ↓
               done           (final in MinIO)

Any stage → error   (subprocess non-zero exit)
awaiting_review → rejected   (POST /api/jobs/<id>/reject)
```

---

## MinIO storage layout

Bucket: `video-production` (public-read policy)

```
video-production/
├── raw/     <job_id>-raw.mp4       uploaded footage (kept for reprocessing)
├── preview/ <job_id>-preview.mp4  Stage 1 output (~5 min to produce)
└── final/   <job_id>-final.mp4    Stage 2 output (~20 min to produce)
```

Access pattern: `https://puppetagent.com/videos/video-production/<prefix>/<file>`

---

## Progress streaming

`video-processor.py` writes `/tmp/<job_id>-progress.json` at each pipeline step:

```json
{"step": "face_swap", "pct": 67, "msg": "FaceFusion running", "stage": 1, "ts": "..."}
```

The `/api/jobs/<id>/progress` SSE endpoint polls this file every 2 seconds and
pushes updates to the browser. The frontend uses `EventSource` to receive them
and update progress bars and step chips in real time.

Stage 1 steps and approximate percentages:
```
start(0) → auto_edit(5-12) → voice_enhance(14-22) → face_swap(25-88) → overlay(92) → done(100)
```

Stage 2 steps:
```
start(0) → lip_sync(5-50) → gaze_correct(52-65) → bg_replace(67-85) → amix(87-95) → done(100)
```

---

## Configuration system

Both `app.py` and `video-processor.py` read all machine-specific paths from a single
config file. No hardcoded `/home/...` paths remain in the code.

### Config file location (in priority order)

1. Path in `PUPPETAGENT_CONF` env var
2. `~/.puppetagent/puppetagent.conf` — recommended for host installs
3. `/etc/puppetagent/puppetagent.conf` — system-wide installs

### Available config keys

| Key | Default | Purpose |
|-----|---------|---------|
| `PUPPETAGENT_CONDA_BASE` | `/home/YOUR_USERNAME/miniconda3` | Root of conda/miniconda install |
| `PUPPETAGENT_XTTS_PYTHON` | `/home/YOUR_USERNAME/venvs/xtts/bin/python` | Python in XTTS venv |
| `PUPPETAGENT_MUSETALK_DIR` | `/home/YOUR_USERNAME/MuseTalk` | MuseTalk clone directory |
| `PUPPETAGENT_WAV2LIP_DIR` | `/home/YOUR_USERNAME/Wav2Lip` | Wav2Lip clone directory |
| `PUPPETAGENT_FACEFUSION_DIR` | `/home/YOUR_USERNAME/facefusion` | FaceFusion clone directory |
| `PUPPETAGENT_SCRIPTS_DIR` | `/home/YOUR_USERNAME/scripts` | Helper scripts directory |
| `PUPPETAGENT_MUSETALK_ENV` | `musetalk` | Conda env name for MuseTalk |
| `PUPPETAGENT_WAV2LIP_ENV` | `wav2lip` | Conda env name for Wav2Lip |
| `PUPPETAGENT_FACEFUSION_ENV` | `facefusion` | Conda env name for FaceFusion |
| `PUPPETAGENT_INFLUENCER_BASE` | `/home/YOUR_USERNAME/influencers` | Per-influencer asset directory |
| `PUPPETAGENT_FACEFUSION_API` | `http://localhost:7860` | FaceFusion server URL (optional) |
| `PUPPETAGENT_MINIO_ENDPOINT` | `localhost:9004` | MinIO endpoint |
| `PUPPETAGENT_MINIO_ACCESS` | `minio_admin` | MinIO access key |
| `PUPPETAGENT_MINIO_SECRET` | `changeme` | MinIO secret key |
| `PUPPETAGENT_MINIO_BUCKET` | `video-production` | MinIO bucket name |
| `PUPPETAGENT_MINIO_PUBLIC` | `https://puppetagent.com/videos` | Public video base URL |
| `PUPPETAGENT_PROCESSOR_SCRIPT` | `~/scripts/video-processor.py` | Path to video-processor.py |
| `PUPPETAGENT_PORT` | `9005` | Port for Flask studio app |
