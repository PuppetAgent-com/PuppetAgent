---
name: puppetagent
description: Use a human as a recording device. Push a script to PuppetAgent Server — the performer's phone picks it up automatically, auto-scrolls as they speak (Vosk offline STT), records on front camera, and uploads the finished video back for post-processing (voice clone, background, lip sync). The full loop — script to processed video — requires zero human initiation.
version: 1.0.0
emoji: 🎭
user-invocable: false
---

# PuppetAgent

The AI agent writes the script. The human looks into the camera and speaks it.
PuppetAgent handles everything else.

Homepage: https://puppetagent.com

## How it works

```
Agent POSTs script → Server queues it
                    → Android app polls every 5s → script loads on phone
                    → Performer reads, Vosk STT auto-scrolls
                    → Recording stops → app uploads video automatically
                    → Post-processing: voice clone + background + lip sync
                    → Final video in MinIO storage
```

No human needs to tap anything to start. No human needs to do anything after recording ends.

## Required env var

```
PUPPETAGENT_SERVER_URL   # e.g. https://your-server.example.com
```

## Endpoints

### Push a script (main action)

```
POST {PUPPETAGENT_SERVER_URL}/scripts/{script_id}/select
Content-Type: application/json

{
  "script_id": "crypto-2026-03-16",
  "title": "Why Bitcoin hit 100k",
  "lines": [
    "Let's talk about what just happened.",
    "Bitcoin crossed six figures for the first time.",
    ...
  ],
  "influencer": "emma"
}
```

The performer's phone picks it up within one 5-second poll cycle.
Valid influencer values: `emma`, `sarah`

### Check what's queued

```
GET {PUPPETAGENT_SERVER_URL}/next_script
→ { script_id, title, body, updated_at }   or  404 if nothing queued
```

### List all pending scripts

```
GET {PUPPETAGENT_SERVER_URL}/scripts
→ [{ script_id, title, preview, is_current, influencer }, ...]
```

### Delete a script

```
POST {PUPPETAGENT_SERVER_URL}/scripts/{script_id}/delete
```

### Health check

```
GET {PUPPETAGENT_SERVER_URL}/health
→ { status: "ok" }
```

## After recording

The app uploads automatically when the performer stops recording.
Post-processing pipeline on your-server handles: XTTS voice clone → SDXL background → face swap → lip sync.
No agent action required. The processed video lands in MinIO and a Telegram notification is sent.

## Example — push a script with curl

```bash
curl -s -X POST "$PUPPETAGENT_SERVER_URL/scripts/my-script-001/select" \
  -H "Content-Type: application/json" \
  -d '{
    "script_id": "my-script-001",
    "title": "Today on crypto",
    "lines": ["Line one.", "Line two.", "Line three."],
    "influencer": "emma"
  }'
```

## Script writing guidelines

- Lines should be 1–2 sentences each (natural speech chunks)
- 40–50 lines = ~3–4 minute video
- Avoid bullet lists or markdown — the app renders plain text
- Write conversational, not formal — the performer reads exactly what you write
- Start with a hook. End with a CTA.
