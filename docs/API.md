# PuppetAgent Studio — API Reference

All endpoints require basic auth (configured in Traefik) except `/videos/*`.

Base URL: `https://puppetagent.com` (or `http://localhost:9005` internally)

---

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | ✓ | Dashboard HTML |
| GET | `/api/jobs` | ✓ | List jobs |
| GET | `/api/jobs/<id>` | ✓ | Job detail + MinIO URLs |
| POST | `/api/upload` | ✓ | Upload raw video |
| POST | `/api/jobs/script` | ✓ | Create job from text script |
| POST | `/api/jobs/<id>/process` | ✓ | Start Stage 1 |
| POST | `/api/jobs/<id>/process2` | ✓ | Start Stage 2 |
| POST | `/api/jobs/<id>/review` | ✓ | Submit star rating |
| PATCH | `/api/jobs/<id>/options` | ✓ | Update feature flags |
| POST | `/api/jobs/<id>/reject` | ✓ | Reject job |
| GET | `/api/jobs/<id>/progress` | ✓ | SSE progress stream |
| GET | `/api/jobs/<id>/logs` | ✓ | Last 100 log lines |
| GET | `/videos/*` | ✗ | MinIO video proxy (no auth) |

Auth ✓ = basic auth. Auth ✗ = open (UUID-secured filenames).

---

## GET /api/jobs

Query params:
- `status` — filter by status (`uploaded`, `stage1_running`, `awaiting_review`, `stage2_running`, `done`, `error`, `rejected`)
- `limit` — max results (default: 50)

Response: JSON array of job objects (with enriched `raw_url`, `preview_url`, `final_url`).

---

## GET /api/jobs/\<id\>

Response:
```json
{
  "job_id": "chr-a1b2c3d4",
  "influencer": "emma",
  "title": "My video",
  "status": "awaiting_review",
  "raw_key": "raw/chr-a1b2c3d4-raw.mp4",
  "preview_key": "preview/chr-a1b2c3d4-preview.mp4",
  "final_key": null,
  "raw_url": "https://puppetagent.com/videos/video-production/raw/...",
  "preview_url": "https://puppetagent.com/videos/video-production/preview/...",
  "final_url": null,
  "opt_auto_edit": 1,
  "opt_voice_enhance": 1,
  "opt_face_swap": 1,
  "opt_lip_sync": 1,
  "opt_gaze_correct": 1,
  "opt_bg_replace": 0,
  "rating": 0,
  "comment": "",
  "is_running": false,
  "created_at": "2026-03-16T12:00:00",
  "updated_at": "2026-03-16T12:05:00"
}
```

---

## POST /api/upload

Multipart form:
- `video` — video file (MP4/MOV/WebM, max 500 MB)
- `influencer` — influencer name (default: `emma`)
- `title` — optional title string
- `chat_id` — optional Telegram chat ID for notifications
- `auto_start` — `1` to start Stage 1 immediately (default: `0`)

Response (201):
```json
{"job_id": "chr-a1b2c3d4", "status": "stage1_running", "raw_url": "..."}
```

---

## POST /api/jobs/script

JSON body:
```json
{
  "script": "Hello, I'm Emma. Today we're talking about...",
  "influencer": "emma",
  "title": "Video title",
  "chat_id": "123456",
  "auto_start": true
}
```

Response (201): `{"job_id": "...", "status": "stage1_running"}`

---

## POST /api/jobs/\<id\>/review

JSON body:
```json
{"rating": 4, "comment": "Looks good, approve for Stage 2"}
```

- `rating` 1–5: triggers Stage 2 if job is `awaiting_review`
- `rating` 0: saves comment only, no stage trigger

---

## PATCH /api/jobs/\<id\>/options

JSON body with any subset of:
```json
{
  "opt_auto_edit": 1,
  "opt_voice_enhance": 1,
  "opt_face_swap": 1,
  "opt_lip_sync": 1,
  "opt_gaze_correct": 1,
  "opt_bg_replace": 0,
  "title": "Updated title"
}
```

---

## GET /api/jobs/\<id\>/progress

SSE stream. Each event:
```
data: {"step": "face_swap", "pct": 67, "msg": "FaceFusion running", "stage": 1, "ts": "..."}
```

Steps (Stage 1): `auto_edit`, `voice_enhance`, `face_swap`, `overlay`
Steps (Stage 2): `lip_sync`, `gaze_correct`, `bg_replace`, `amix`

Stream ends when job reaches a terminal status or after 3 minutes of no progress.

---

## GET /api/jobs/\<id\>/logs

Response: JSON array of `{"ts": "...", "line": "..."}` objects (last 100 lines).

---

## Job statuses

| Status | Meaning |
|--------|---------|
| `uploaded` | Raw video received, not yet processed |
| `stage1_running` | Stage 1 processing (face swap, voice, auto-edit) |
| `awaiting_review` | Stage 1 done, preview ready in MinIO |
| `stage2_running` | Stage 2 processing (lip sync, gaze, bg) |
| `done` | Final video ready in MinIO |
| `error` | Processing failed (see `error_msg`) |
| `rejected` | Manually rejected by reviewer |
