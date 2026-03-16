# PuppetAgent

**AI-powered video production pipeline** — automated face swap, voice cloning, lip sync, and post-production in a browser-based review dashboard.

Built for running AI influencer video workflows at scale. Self-hostable on any Linux machine with GPU.

---

## What it does

PuppetAgent Studio manages a two-stage video production pipeline:

- **Stage 1** — face swap, voice cloning, auto-edit, overlay composition → preview
- **Stage 2** — lip sync (MuseTalk), gaze correction, background replacement, audio mix → final

A browser dashboard lets you upload footage, monitor live progress (SSE), review previews with star ratings, and download final videos.

---

## Architecture

```
Browser
  │  https://puppetagent.com/          (basic auth)
  │  https://puppetagent.com/videos/*  (no auth — UUID security)
  ▼
Cloudflare Tunnel / Reverse Proxy
  ▼
Traefik (or nginx)
  ├── /videos/* → MinIO:9000   (strip /videos prefix)
  └── /*        → Studio:9005  (Flask app)
       ▼
  Flask app (studio/, systemd on host)
  ├── SQLite job DB (~/PuppetAgent/data/puppetagent.db)
  └── Spawns video-processor.py as subprocess
       ▼
  video-processor.py (processor/, conda envs, GPU)
  ├── FaceFusion   (conda env: facefusion)
  ├── MuseTalk     (conda env: musetalk)
  ├── XTTS         (venv: ~/venvs/xtts/)
  └── DeepFilterNet, Whisper, MediaPipe
```

---

## Quick start

### Requirements

| Component | What to install |
|-----------|----------------|
| Video processing | FaceFusion, MuseTalk, XTTS, DeepFilterNet (conda envs on host) |
| Studio app | Python 3.10+, `pip install flask minio` |
| Storage | MinIO (Docker) or any S3-compatible store |
| Reverse proxy | Traefik, nginx, or Caddy |

### Install

```bash
# 1. Clone
git clone https://github.com/PuppetAgent/PuppetAgent.git
cd PuppetAgent

# 2. Create venv and install studio deps
python3 -m venv ~/venvs/puppetagent
~/venvs/puppetagent/bin/pip install flask minio

# 3. Configure
cp puppetagent.conf.example ~/.puppetagent/puppetagent.conf
nano ~/.puppetagent/puppetagent.conf   # edit paths

# 4. Verify processor paths
python3 processor/video-processor.py --check-config

# 5. Start MinIO (Docker)
docker run -d --name minio-video -p 9004:9000 \
  -e MINIO_ROOT_USER=minio_admin \
  -e MINIO_ROOT_PASSWORD=yourpassword \
  -v ~/minio-video:/data \
  minio/minio server /data

# 6. Install and start the studio service
cp infra/puppetagent.service ~/.config/systemd/user/
cp studio/app.py ~/PuppetAgent/
cp -r studio/static ~/PuppetAgent/
mkdir -p ~/PuppetAgent/data
systemctl --user daemon-reload
systemctl --user enable --now puppetagent

# 7. Verify
curl http://localhost:9005/api/jobs
```

### Docker (studio only)

```bash
docker compose up -d
```

Note: `video-processor.py` and GPU tools cannot be containerized yet — they require
host conda environments. The `studio/` Flask app runs fine in Docker if you mount
`/tmp` (shared progress files) and set `PUPPETAGENT_PROCESSOR_SCRIPT` to the host path.

---

## Configuration

All settings read from `~/.puppetagent/puppetagent.conf` or environment variables.
See [`puppetagent.conf.example`](puppetagent.conf.example) for all options.

Key settings:

| Key | Default | Purpose |
|-----|---------|---------|
| `PUPPETAGENT_MINIO_ENDPOINT` | `localhost:9004` | MinIO endpoint |
| `PUPPETAGENT_MINIO_PUBLIC` | `https://puppetagent.com/videos` | Public video base URL |
| `PUPPETAGENT_INFLUENCER_BASE` | `/home/YOUR_USERNAME/influencers` | Per-influencer assets |
| `PUPPETAGENT_PROCESSOR_SCRIPT` | `~/scripts/video-processor.py` | Path to processor |

---

## Docs

- [Architecture](docs/ARCHITECTURE.md) — detailed system design
- [Self-hosting](docs/SELF-HOSTING.md) — step-by-step setup guide
- [API reference](docs/API.md) — HTTP endpoints

---

## License

Apache 2.0 — see [LICENSE](LICENSE)
