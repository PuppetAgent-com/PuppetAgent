#!/usr/bin/env python3
"""
video-processor.py — Talking-head video production orchestrator.

Runs on your host machine. Two-stage pipeline:
  Stage 1 (--preview-only):  XTTS + FaceFusion + audio overlay (~5 min) → preview
  Stage 2 (--resume):        MuseTalk + gaze + bg + amix (~15-20 min) → final

Full single-pass (no flags):  All stages in sequence (~20-25 min)

Usage — Pipeline A (autonomous, text script):
    python3 ~/scripts/video-processor.py \
        --influencer emma \
        --script "Emma Howard reports on 3D concrete printing." \
        --job-id test-001 --output /tmp/test-001-final.mp4

Usage — Pipeline B (user video, voice clone):
    python3 ~/scripts/video-processor.py \
        --influencer emma --input-video /tmp/raw.mp4 \
        --clone-voice --job-id test-002 --output /tmp/test-002-final.mp4

Preview-only (Stage 1):
    python3 ~/scripts/video-processor.py \
        --influencer emma --input-video /tmp/raw.mp4 \
        --clone-voice --preview-only --job-id test-003 --output /tmp/test-003-preview.mp4

Resume from preview (Stage 2):
    python3 ~/scripts/video-processor.py \
        --influencer emma --resume \
        --job-id test-003 --output /tmp/test-003-final.mp4

Sentinels written to /tmp:
  <job>.preview_done  — Stage 1 complete
  <job>.done          — Stage 2 / full pipeline complete
  <job>.error         — Pipeline failed (contains error message)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────
# All paths are read from puppetagent.conf (or environment variables).
# Copy puppetagent.conf.example → ~/.puppetagent/puppetagent.conf and edit for your machine.
# Environment variables always override the config file (Docker-friendly).

def _load_config() -> dict:
    """Load config from ~/.puppetagent/puppetagent.conf or PUPPETAGENT_CONF env var."""
    conf_path = os.environ.get(
        "PUPPETAGENT_CONF",
        str(Path.home() / ".puppetagent" / "puppetagent.conf"),
    )
    # Also check /etc/puppetagent/puppetagent.conf as fallback
    fallback = "/etc/puppetagent/puppetagent.conf"
    cfg = {}
    for path in [conf_path, fallback]:
        if Path(path).exists():
            for line in Path(path).read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    cfg.setdefault(k.strip(), v.strip())   # first file wins
            break
    # Env vars override config file
    for k, v in os.environ.items():
        if k.startswith("PUPPETAGENT_"):
            cfg[k] = v
    return cfg


_cfg = _load_config()


def _c(key: str, default: str) -> str:
    """Get config value, env var takes precedence, falls back to default."""
    return _cfg.get(key, default)


# ── Paths (all overridable via puppetagent.conf or env vars) ───────────────────
_CONDA_BASE        = Path(_c("PUPPETAGENT_CONDA_BASE",      "/home/YOUR_USERNAME/miniconda3"))
INFLUENCER_BASE    = Path(_c("PUPPETAGENT_INFLUENCER_BASE", "/home/YOUR_USERNAME/influencers"))
MUSETALK_DIR       = Path(_c("PUPPETAGENT_MUSETALK_DIR",    "/home/YOUR_USERNAME/MuseTalk"))
SCRIPTS_DIR        = Path(_c("PUPPETAGENT_SCRIPTS_DIR",     "/home/YOUR_USERNAME/scripts"))
WAV2LIP_DIR        = Path(_c("PUPPETAGENT_WAV2LIP_DIR",     "/home/YOUR_USERNAME/Wav2Lip"))
FACEFUSION_DIR     = Path(_c("PUPPETAGENT_FACEFUSION_DIR",  "/home/YOUR_USERNAME/facefusion"))
XTTS_PYTHON        = _c("PUPPETAGENT_XTTS_PYTHON",          "/home/YOUR_USERNAME/venvs/xtts/bin/python")
MUSETALK_CONDA_ENV = _c("PUPPETAGENT_MUSETALK_ENV",         "musetalk")
WAV2LIP_CONDA_ENV  = _c("PUPPETAGENT_WAV2LIP_ENV",          "wav2lip")
FACEFUSION_ENV     = _c("PUPPETAGENT_FACEFUSION_ENV",        "facefusion")
FACEFUSION_API     = _c("PUPPETAGENT_FACEFUSION_API",        "http://localhost:7860")
AUTO_EDIT_SCRIPT   = SCRIPTS_DIR / "auto-edit.py"
VOICE_ENHANCE_SCRIPT = SCRIPTS_DIR / "voice-enhance.py"


# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg: str):
    print(f"[video-processor {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd: list, check=True, env=None, cwd=None, timeout=3600) -> subprocess.CompletedProcess:
    log(f"  $ {' '.join(str(c) for c in cmd[:6])}{'...' if len(cmd) > 6 else ''}")
    merged = {**os.environ, **(env or {})}
    r = subprocess.run(cmd, check=False, capture_output=True, text=True,
                       env=merged, cwd=cwd, timeout=timeout)
    if r.stdout.strip():
        for line in r.stdout.strip().splitlines()[-3:]:
            log(f"    {line}")
    if r.returncode != 0:
        log(f"  STDERR (last 500): {r.stderr.strip()[-500:]}")
        if check:
            raise RuntimeError(f"Command failed (exit {r.returncode}): {cmd[0]}")
    return r


CONDA = str(_CONDA_BASE / "bin" / "conda")

def conda_run(env_name: str, script: str, args: list, cwd=None,
              extra_env: dict = None, timeout=3600):
    cmd = [CONDA, "run", "-n", env_name, "--no-capture-output",
           "python3", script] + [str(a) for a in args]
    return run(cmd, cwd=cwd, env=extra_env, timeout=timeout)


def write_sentinel(job: str, kind: str, content: str = "ok"):
    Path(f"/tmp/{job}.{kind}").write_text(content)


def write_progress(job_id: str, step: str, pct: int, msg: str, stage: int = 1):
    """Write progress JSON for the backstage SSE stream."""
    data = {
        "step":  step,
        "pct":   pct,
        "msg":   msg,
        "stage": stage,
        "ts":    datetime.now().isoformat(),
    }
    with open(f"/tmp/{job_id}-progress.json", "w") as _pf:
        json.dump(data, _pf)


MAX_FRAMES_BEFORE_COMPRESS = 2400  # ~80s at 30fps — compress anything longer

def compress_for_processing(video_in: str, video_out: str):
    """Downsample long videos to 720p@15fps before FaceFusion/MuseTalk.
    Reduces 9000-frame 5-min videos to ~2800 frames — 3× faster processing."""
    log(f"  Pre-compressing to 720p@15fps: {Path(video_in).name}")
    run(["ffmpeg", "-y", "-i", video_in,
         "-vf", "scale=1280:720",
         "-r", "15",
         "-c:v", "libx264", "-crf", "26", "-preset", "fast",
         "-c:a", "aac", "-b:a", "128k",
         video_out])


def maybe_compress(video_in: str, job: str) -> str:
    """Return compressed path if video exceeds MAX_FRAMES_BEFORE_COMPRESS, else original."""
    import subprocess as _sp
    r = _sp.run(["ffprobe", "-v", "quiet", "-show_entries", "stream=nb_frames,r_frame_rate",
                 "-select_streams", "v:0", "-of", "default=noprint_wrappers=1", video_in],
                capture_output=True, text=True)
    frames = 0
    for line in r.stdout.splitlines():
        if line.startswith("nb_frames="):
            try: frames = int(line.split("=")[1])
            except: pass
    if frames == 0:  # nb_frames not in container — estimate from duration
        r2 = _sp.run(["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                      "-of", "default=noprint_wrappers=1", video_in],
                     capture_output=True, text=True)
        for line in r2.stdout.splitlines():
            if line.startswith("duration="):
                try: frames = int(float(line.split("=")[1]) * 30)
                except: pass
    if frames > MAX_FRAMES_BEFORE_COMPRESS:
        log(f"  Video has ~{frames} frames — compressing before processing")
        compressed = f"/tmp/{job}-compressed.mp4"
        compress_for_processing(video_in, compressed)
        return compressed
    return video_in


def auto_edit_video(video_in: str, video_out: str, transcript_json: str = None) -> str:
    """Remove silences and repeated takes from raw recording.

    Runs auto-edit.py (in xtts venv, which has Whisper).
    Saves transcript JSON so XTTS clone can skip re-transcription.
    Returns edited video path, or original if auto-edit fails.
    """
    log(f"Step 0: Auto-edit {Path(video_in).name} → {Path(video_out).name}")
    args = [video_in, video_out]
    if transcript_json:
        args.append(transcript_json)
    try:
        run([XTTS_PYTHON, str(AUTO_EDIT_SCRIPT)] + args, timeout=1800)
        if Path(video_out).exists() and Path(video_out).stat().st_size > 100_000:
            log(f"  Auto-edit done: {Path(video_out).stat().st_size // (1024*1024)} MB")
            return video_out
        log("  WARNING: Auto-edit output missing or too small — using original")
    except Exception as e:
        log(f"  WARNING: Auto-edit failed ({e}) — using original")
    return video_in


def overlay_audio(video_in: str, audio_in: str, output: str):
    """Combine face-swapped video with audio (no lip sync — for preview)."""
    run(["ffmpeg", "-y",
         "-i", video_in, "-i", audio_in,
         "-c:v", "copy", "-c:a", "aac",
         "-map", "0:v:0", "-map", "1:a:0",
         "-shortest", output])


def extract_audio(video_in: str, audio_out: str):
    """Extract audio from video to WAV, or write silence if no audio track."""
    r = run(["ffmpeg", "-y", "-i", video_in,
             "-vn", "-acodec", "pcm_s16le", "-ar", "22050", "-ac", "1",
             audio_out], check=False)
    if r.returncode != 0 or not Path(audio_out).exists() or \
       Path(audio_out).stat().st_size < 1000:
        log("  No audio in video — generating 5s silence")
        run(["ffmpeg", "-y", "-f", "lavfi",
             "-i", "anullsrc=r=22050:cl=mono", "-t", "5", audio_out])


# ── Step 1a: XTTS synthesis ───────────────────────────────────────────────────

XTTS_SYNTH_SCRIPT = '''\
import sys
from TTS.api import TTS
text, speaker_wav, out = sys.argv[1], sys.argv[2], sys.argv[3]
tts = TTS(model_name="tts_models/multilingual/multi-dataset/xtts_v2", gpu=False)
if speaker_wav and __import__("pathlib").Path(speaker_wav).exists():
    tts.tts_to_file(text=text, speaker_wav=speaker_wav, language="en", file_path=out)
else:
    # Fallback to built-in speaker when no sample available
    tts.tts_to_file(text=text, speaker="Damien Black", language="en", file_path=out)
    print("Used built-in speaker Damien Black (no voice-sample.wav found)")
print(f"XTTS done: {out}")
'''

XTTS_CLONE_SCRIPT = '''\
import sys, subprocess, json
from pathlib import Path
from TTS.api import TTS

video_in, speaker_wav, out = sys.argv[1], sys.argv[2], sys.argv[3]
transcript_json = sys.argv[4] if len(sys.argv) > 4 else None

transcript = ""

# Prefer pre-computed transcript from auto-edit (avoids double-transcription)
if transcript_json and Path(transcript_json).exists():
    try:
        data = json.loads(Path(transcript_json).read_text())
        transcript = data.get("text", "").strip()
        print(f"Using pre-computed transcript ({len(transcript)} chars): {transcript[:80]}...")
    except Exception as e:
        print(f"Warning: could not load transcript JSON: {e}")

if not transcript:
    # Fall back to Whisper transcription
    tmp = Path("/tmp/xtts_src.wav")
    subprocess.run(["ffmpeg","-y","-i",video_in,"-vn","-acodec","pcm_s16le",
                    "-ar","16000","-ac","1",str(tmp)], check=False, capture_output=True)
    try:
        import whisper
        model = whisper.load_model("base")
        transcript = model.transcribe(str(tmp))["text"].strip()
        print(f"Transcript ({len(transcript)} chars): {transcript[:80]}...")
    except Exception as e:
        print(f"Whisper unavailable: {e} — using placeholder text")
        transcript = "This is an important update I wanted to share with you today."

tts = TTS(model_name="tts_models/multilingual/multi-dataset/xtts_v2", gpu=False)
kw = {"speaker_wav": speaker_wav} if Path(speaker_wav).exists() else {"speaker": "Damien Black"}
tts.tts_to_file(text=transcript, language="en", file_path=out, **kw)
print(f"XTTS clone done: {out}")
'''


def xtts_synthesize(text: str, speaker_wav: str, output_wav: str):
    log(f"Step 1a: XTTS ({len(text)} chars) → {output_wav}")
    tmp_py = Path("/tmp/xtts_synth.py")
    tmp_py.write_text(XTTS_SYNTH_SCRIPT)
    run([XTTS_PYTHON, str(tmp_py), text, speaker_wav, output_wav], timeout=600)
    if not Path(output_wav).exists():
        raise RuntimeError(f"XTTS produced no output at {output_wav}")


def xtts_clone_voice(video_in: str, speaker_wav: str, output_wav: str,
                     transcript_json: str = None):
    log(f"Step 1b: XTTS voice clone {Path(video_in).name} → {output_wav}")
    tmp_py = Path("/tmp/xtts_clone.py")
    tmp_py.write_text(XTTS_CLONE_SCRIPT)
    cmd = [XTTS_PYTHON, str(tmp_py), video_in, speaker_wav, output_wav]
    if transcript_json:
        cmd.append(transcript_json)
    run(cmd, timeout=7200)
    if not Path(output_wav).exists():
        raise RuntimeError(f"XTTS clone produced no output at {output_wav}")


def pitch_shift_voice(video_in: str, output_wav: str, pitch: float = 0.82):
    """Extract and pitch-shift the original audio to sound older/deeper.

    Uses ffmpeg rubberband — shifts pitch without changing tempo.
    Preserves exact original timing so lips stay in sync with audio.

    pitch: ratio relative to original (0.82 ≈ 3.2 semitones lower = noticeably deeper)
    """
    log(f"Step 1b: Pitch-shift voice (pitch={pitch}) {Path(video_in).name} → {output_wav}")
    run(["ffmpeg", "-y", "-i", video_in,
         "-vn",
         "-af", f"rubberband=pitch={pitch}",
         "-acodec", "pcm_s16le", "-ar", "22050", "-ac", "1",
         output_wav])
    if not Path(output_wav).exists() or Path(output_wav).stat().st_size < 1000:
        raise RuntimeError(f"Pitch shift produced no output at {output_wav}")


def enhance_voice(video_in: str, output_wav: str):
    """DeepFilterNet noise suppression + broadcast EQ/compression chain.

    Removes room noise, applies studio-style processing.
    Preserves original timing exactly — lips stay in sync.
    Falls back to raw audio extraction if DeepFilterNet unavailable.
    """
    log(f"Step 1b: Voice enhance (DeepFilterNet + broadcast chain) → {output_wav}")
    run([XTTS_PYTHON, str(VOICE_ENHANCE_SCRIPT), video_in, output_wav], timeout=600)
    if not Path(output_wav).exists() or Path(output_wav).stat().st_size < 1000:
        raise RuntimeError(f"Voice enhance produced no output at {output_wav}")


# ── Step 2: FaceFusion ────────────────────────────────────────────────────────

def facefusion_swap(source_face: str, target_video: str, output_video: str,
                    preset: str = "good"):
    log(f"Step 2: FaceFusion [{preset}] → {output_video}")
    import urllib.request as _ur
    payload = json.dumps({"source": source_face, "target": target_video,
                          "output": output_video, "preset": preset,
                          "enhancer_blend": 80}).encode()
    req = _ur.Request(f"{FACEFUSION_API}/swap", data=payload,
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _ur.urlopen(req, timeout=7200) as resp:
            result = json.loads(resp.read())
        log(f"  FaceFusion done in {result.get('elapsed_s','?')}s")
    except Exception as e:
        # API down — fallback to direct CLI (no enhancer for fast/good presets in preview)
        enhance_cli = preset in ("best", "hq")
        log(f"  FaceFusion API error: {e} — CLI fallback (enhance={enhance_cli})")
        _facefusion_cli(source_face, target_video, output_video, enhance=enhance_cli)

    if not Path(output_video).exists():
        raise RuntimeError(f"FaceFusion output missing: {output_video}")


def _facefusion_cli(source: str, target: str, output: str, enhance: bool = False):
    """Fallback: run FaceFusion directly via its headless CLI.

    enhance=False (default) — face_swapper only, fast (~2 min/min-of-video on CPU).
    enhance=True            — add codeformer, much slower (used in Stage 2 full render).
    """
    py = str(_CONDA_BASE / "envs" / FACEFUSION_ENV / "bin" / "python")
    ff = str(FACEFUSION_DIR / "facefusion.py")
    processors = ["face_swapper", "face_enhancer"] if enhance else ["face_swapper"]
    extra = (["--face-enhancer-model", "codeformer",
               "--face-enhancer-blend", "80"] if enhance else [])
    run([py, ff, "headless-run",
         "--source-paths", source, "--target-path", target, "--output-path", output,
         "--processors", *processors,
         "--face-swapper-model", "hyperswap_1a_256",
         *extra,
         "--execution-providers", "cpu"],
        cwd=str(FACEFUSION_DIR),
        env={"HSA_OVERRIDE_GFX_VERSION": "11.0.0"},
        timeout=7200)


# ── Step 3: MuseTalk ──────────────────────────────────────────────────────────

def write_musetalk_config(config_path: str, video_path: str, audio_path: str):
    """Write MuseTalk YAML config without requiring PyYAML."""
    Path(config_path).write_text(
        f'task_0:\n  video_path: "{video_path}"\n  audio_path: "{audio_path}"\n'
    )


def musetalk_sync(video_in: str, audio_in: str, output_video: str, job: str):
    log(f"Step 3: MuseTalk lip sync → {output_video}")
    config_path = f"/tmp/{job}-musetalk.yaml"
    result_dir  = f"/tmp/{job}-musetalk-out"
    Path(result_dir).mkdir(parents=True, exist_ok=True)
    write_musetalk_config(config_path, video_in, audio_in)

    cmd = [CONDA, "run", "-n", MUSETALK_CONDA_ENV, "--no-capture-output",
           "python3", "-m", "scripts.inference",
           "--inference_config", config_path,
           "--result_dir", result_dir,
           "--unet_model_path", str(MUSETALK_DIR / "models/musetalk/musetalkV15/unet.pth"),
           "--unet_config",     str(MUSETALK_DIR / "models/musetalk/musetalkV15/config.json"),
           "--vae_type", "sd-vae",
           "--whisper_dir", str(MUSETALK_DIR / "models/whisper"),
           "--version", "v15", "--batch_size", "4",
           "--use_saved_coord", "--saved_coord"]  # cache face detection pkl for re-runs

    r = run(cmd, check=False, cwd=str(MUSETALK_DIR),
            env={"PYTHONPATH": str(MUSETALK_DIR)}, timeout=14400)  # 4h — CPU is slow

    # Locate output — MuseTalk saves to result_dir/task_0.mp4
    candidates = [Path(result_dir) / "task_0.mp4"] + \
                 list(Path(result_dir).glob("**/*.mp4"))
    found = [p for p in candidates if p.exists()]

    if not found:
        if r.returncode != 0:
            log("  MuseTalk failed — falling back to Wav2Lip")
            wav2lip_sync(video_in, audio_in, output_video)
            return
        raise RuntimeError(f"MuseTalk produced no MP4 in {result_dir}")

    shutil.copy2(str(sorted(found, key=lambda p: p.stat().st_mtime)[-1]), output_video)
    log(f"  MuseTalk done → {output_video}")


def wav2lip_sync(video_in: str, audio_in: str, output_video: str):
    """Fallback lip sync using Wav2Lip when MuseTalk is unavailable."""
    log("Step 3 (fallback): Wav2Lip lip sync")
    ckpt = str(WAV2LIP_DIR / "checkpoints/wav2lip_gan.pth")
    if not Path(ckpt).exists():
        ckpt = str(WAV2LIP_DIR / "checkpoints/wav2lip.pth")
    run([CONDA, "run", "-n", WAV2LIP_CONDA_ENV, "--no-capture-output",
         "python3", str(WAV2LIP_DIR / "inference.py"),
         "--checkpoint_path", ckpt,
         "--face", video_in, "--audio", audio_in,
         "--outfile", output_video, "--resize_factor", "1"],
        cwd=str(WAV2LIP_DIR), timeout=7200)
    if not Path(output_video).exists():
        raise RuntimeError(f"Wav2Lip fallback also failed — no output at {output_video}")
    log(f"  Wav2Lip done → {output_video}")


# ── Step 4: Gaze correction ───────────────────────────────────────────────────

def gaze_correct(video_in: str, video_out: str, strength: float = 0.6):
    log(f"Step 4: Gaze correction (strength={strength})")
    r = conda_run(WAV2LIP_CONDA_ENV, str(SCRIPTS_DIR / "gaze-correction.py"),
                  ["--input", video_in, "--output", video_out,
                   "--strength", str(strength)],
                  check=False if True else True,  # non-fatal
                  timeout=1800)
    # Non-fatal: if gaze correction fails, pass through input
    if not Path(video_out).exists():
        log("  WARNING: Gaze correction produced no output — passing through")
        shutil.copy2(video_in, video_out)


# ── Step 5: Background replacement ───────────────────────────────────────────

def bg_replace(video_in: str, background: str, video_out: str):
    log(f"Step 5: BG replacement → {video_out}")
    r = conda_run(WAV2LIP_CONDA_ENV, str(SCRIPTS_DIR / "bg-replacement.py"),
                  ["--input", video_in, "--background", background,
                   "--output", video_out],
                  check=False if True else True,  # non-fatal
                  timeout=1800)
    if not Path(video_out).exists():
        log("  WARNING: BG replacement produced no output — passing through")
        shutil.copy2(video_in, video_out)


# ── Step 6: Amix ambient ──────────────────────────────────────────────────────

def amix_ambient(video_in: str, ambient_wav: str, video_out: str,
                 vol: float = 0.08):
    log(f"Step 6: Amix ambient audio (vol={vol})")
    run(["ffmpeg", "-y", "-i", video_in, "-i", ambient_wav,
         "-filter_complex",
         f"[1:a]volume={vol}[amb];[0:a][amb]amix=inputs=2:duration=first[aout]",
         "-map", "0:v", "-map", "[aout]",
         "-c:v", "copy", "-c:a", "aac", "-shortest", video_out])


# ── Stage helpers ─────────────────────────────────────────────────────────────

def stage1(args, infl_dir, job, pipeline_a, pipeline_b):
    """Stage 1: XTTS + FaceFusion + audio overlay. Fast preview (~5 min)."""
    portrait      = str(infl_dir / "portrait.jpg")
    neutral_vid   = str(infl_dir / "neutral-loop.mp4")
    voice_sample  = str(infl_dir / "voice-sample.wav")
    voice_wav     = f"/tmp/{job}-voice.wav"
    swap_out      = f"/tmp/{job}-swapped.mp4"
    final_out     = args.output

    write_progress(job, "start", 0, "Stage 1 starting…", stage=1)

    if pipeline_a:
        write_progress(job, "voice_enhance", 5, "Synthesising voice (XTTS)…", stage=1)
        xtts_synthesize(args.script, voice_sample, voice_wav)
        target_video = neutral_vid
        if not Path(target_video).exists():
            sys.exit(f"ERROR: neutral-loop.mp4 not found at {target_video}")
        write_progress(job, "voice_enhance", 22, "Voice synthesis complete", stage=1)
    else:
        target_video    = args.input_video
        transcript_json = None

        # Step 0: Auto-edit — remove silences + repeated takes (Pipeline B only)
        if not args.no_auto_edit:
            write_progress(job, "auto_edit", 5, "Removing silences and repeated takes…", stage=1)
            edited_path     = f"/tmp/{job}-edited.mp4"
            transcript_json = f"/tmp/{job}-transcript.json"
            target_video    = auto_edit_video(args.input_video, edited_path, transcript_json)
            if not Path(transcript_json).exists():
                transcript_json = None  # auto-edit failed, will re-transcribe in XTTS
            write_progress(job, "auto_edit", 12, "Auto-edit complete", stage=1)

        if args.clone_voice:
            write_progress(job, "voice_enhance", 14, "Enhancing voice (DeepFilterNet)…", stage=1)
            # DeepFilterNet noise suppression + broadcast EQ/compression.
            # Preserves original timing exactly — lips stay in sync.
            enhance_voice(target_video, voice_wav)
            write_progress(job, "voice_enhance", 22, "Voice enhancement complete", stage=1)
        else:
            write_progress(job, "voice_enhance", 14, "Extracting audio…", stage=1)
            extract_audio(target_video, voice_wav)
            write_progress(job, "voice_enhance", 22, "Audio extracted", stage=1)

        # Compress long videos before FaceFusion (>80s → 720p@15fps)
        target_video = maybe_compress(target_video, job)

    write_progress(job, "face_swap", 25, "Starting FaceFusion…", stage=1)
    # Preview uses "fast" preset (no codeformer enhancer) — enhancer runs in Stage 2
    preview_preset = "fast" if args.preview_only else args.facefusion_preset
    facefusion_swap(portrait, target_video, swap_out, preview_preset)
    write_progress(job, "face_swap", 88, "Face swap complete", stage=1)

    write_progress(job, "overlay", 92, "Overlaying audio…", stage=1)
    # Overlay audio on face-swapped video (no lip sync — for preview)
    overlay_audio(swap_out, voice_wav, final_out)

    if not Path(final_out).exists():
        raise RuntimeError(f"Stage 1 output missing: {final_out}")

    write_progress(job, "done", 100, "Stage 1 complete", stage=1)
    write_sentinel(job, "preview_done")
    log(f"=== Stage 1 (preview) complete → {final_out} ===")
    log(f"    Size: {Path(final_out).stat().st_size // 1024} KB")
    log(f"    Sentinel: /tmp/{job}.preview_done")


def stage2(args, infl_dir, job):
    """Stage 2: MuseTalk + gaze + bg + amix. Continues from Stage 1 temp files."""
    # Log review feedback if provided
    if getattr(args, "review_file", None) and Path(args.review_file).exists():
        try:
            review = json.load(open(args.review_file))
            log(f"Review: {review.get('rating', 0)}/5 — {review.get('comment', '(no comment)')}")
        except Exception as e:
            log(f"Warning: could not read review file: {e}")

    voice_wav    = f"/tmp/{job}-voice.wav"
    swap_out     = f"/tmp/{job}-swapped.mp4"
    sync_out     = f"/tmp/{job}-synced.mp4"
    gaze_out     = f"/tmp/{job}-gaze.mp4"
    bg_out       = f"/tmp/{job}-bg.mp4"
    final_out    = args.output
    background   = str(infl_dir / "background.png")
    ambient_wav  = str(infl_dir / "ambient.wav")

    if not Path(swap_out).exists():
        sys.exit(f"ERROR: Stage 1 swap file not found: {swap_out}\n"
                 "Run Stage 1 first (--preview-only) before --resume")
    if not Path(voice_wav).exists():
        sys.exit(f"ERROR: Stage 1 voice file not found: {voice_wav}")

    write_progress(job, "start", 0, "Stage 2 starting…", stage=2)

    # Step 3: MuseTalk lip sync
    write_progress(job, "lip_sync", 5, "Starting MuseTalk lip sync…", stage=2)
    musetalk_sync(swap_out, voice_wav, sync_out, job)
    write_progress(job, "lip_sync", 50, "Lip sync complete", stage=2)

    # Step 4: Gaze correction
    if args.no_gaze:
        log("Step 4: Skipping gaze correction (--no-gaze)")
        gaze_out = sync_out
        write_progress(job, "gaze_correct", 55, "Gaze correction skipped", stage=2)
    else:
        write_progress(job, "gaze_correct", 52, "Correcting gaze…", stage=2)
        gaze_correct(sync_out, gaze_out, args.gaze_strength)
        write_progress(job, "gaze_correct", 65, "Gaze correction complete", stage=2)

    # Step 5: BG replacement
    if args.no_bg or not Path(background).exists():
        log(f"Step 5: {'Skipping (--no-bg)' if args.no_bg else 'No background.png — skipping'}")
        bg_out = gaze_out
        write_progress(job, "bg_replace", 68, "Background replacement skipped", stage=2)
    else:
        write_progress(job, "bg_replace", 67, "Replacing background…", stage=2)
        bg_replace(gaze_out, background, bg_out)
        write_progress(job, "bg_replace", 85, "Background replaced", stage=2)

    # Step 6: Amix ambient
    write_progress(job, "amix", 87, "Mixing ambient audio…", stage=2)
    if Path(ambient_wav).exists():
        amix_ambient(bg_out, ambient_wav, final_out)
    else:
        shutil.copy2(bg_out, final_out)

    if not Path(final_out).exists():
        raise RuntimeError(f"Stage 2 output missing: {final_out}")

    write_progress(job, "done", 100, "Stage 2 complete", stage=2)
    write_sentinel(job, "done")
    log(f"=== Stage 2 (full render) complete → {final_out} ===")
    log(f"    Size: {Path(final_out).stat().st_size // (1024*1024)} MB")
    log(f"    Sentinel: /tmp/{job}.done")

    # Cleanup stage 1 temp files
    for f in [sync_out]:
        if f not in (gaze_out, bg_out, final_out):
            Path(f).unlink(missing_ok=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Talking-head video production pipeline")
    ap.add_argument("--influencer",   required=True, choices=["emma", "sarah"])
    ap.add_argument("--job-id",       required=True)
    ap.add_argument("--output",       required=True)

    # Pipeline A
    ap.add_argument("--script",       help="Text to speak")
    ap.add_argument("--script-file",  help="File containing text to speak (avoids shell quoting)")

    # Pipeline B
    ap.add_argument("--input-video")
    ap.add_argument("--clone-voice",  action="store_true")

    # Stage control
    ap.add_argument("--preview-only", action="store_true",
                    help="Stage 1 only: FaceFusion + audio overlay (~5 min), no MuseTalk")
    ap.add_argument("--resume",       action="store_true",
                    help="Stage 2 only: continue from Stage 1 temp files (MuseTalk forward)")

    # Quality flags
    ap.add_argument("--facefusion-preset", default="good",
                    choices=["fast", "good", "best", "hq"])
    ap.add_argument("--gaze-strength", type=float, default=0.6)
    ap.add_argument("--no-bg",        action="store_true")
    ap.add_argument("--no-gaze",      action="store_true")
    ap.add_argument("--no-auto-edit", action="store_true",
                    help="Skip silence removal and repeated take detection (Pipeline B only)")
    ap.add_argument("--pitch", type=float, default=0.82,
                    help="Pitch ratio for voice shift (default 0.82 ≈ 3 semitones lower)")

    # Review feedback (written by telegram-video-handler after dashboard rating)
    ap.add_argument("--review-file",  help="Path to JSON file with rating/comment from reviewer")

    # Config check (self-hosting setup verification)
    ap.add_argument("--check-config", action="store_true",
                    help="Verify all configured paths exist and print config summary")

    args = ap.parse_args()

    # Config check mode — print summary and exit
    if args.check_config:
        ok = True
        checks = [
            ("CONDA",           CONDA),
            ("XTTS_PYTHON",     XTTS_PYTHON),
            ("MUSETALK_DIR",    str(MUSETALK_DIR)),
            ("WAV2LIP_DIR",     str(WAV2LIP_DIR)),
            ("FACEFUSION_DIR",  str(FACEFUSION_DIR)),
            ("SCRIPTS_DIR",     str(SCRIPTS_DIR)),
            ("AUTO_EDIT_SCRIPT",str(AUTO_EDIT_SCRIPT)),
            ("VOICE_ENHANCE_SCRIPT", str(VOICE_ENHANCE_SCRIPT)),
            ("INFLUENCER_BASE", str(INFLUENCER_BASE)),
        ]
        print("PuppetAgent config check:")
        for name, path in checks:
            exists = Path(path).exists()
            status = "✓" if exists else "✗ MISSING"
            print(f"  {status:12s} {name:25s} = {path}")
            if not exists:
                ok = False
        print(f"\nConfig source: {os.environ.get('PUPPETAGENT_CONF', Path.home() / '.puppetagent' / 'puppetagent.conf')}")
        print(f"FACEFUSION_API: {FACEFUSION_API}")
        sys.exit(0 if ok else 1)

    # Load script from file if given
    if args.script_file:
        args.script = Path(args.script_file).read_text().strip()

    infl_dir     = INFLUENCER_BASE / args.influencer
    portrait     = infl_dir / "portrait.jpg"
    voice_sample = infl_dir / "voice-sample.wav"
    job          = args.job_id

    pipeline_b = bool(args.input_video)
    pipeline_a = bool(args.script) and not pipeline_b

    if not args.resume and not pipeline_a and not pipeline_b:
        sys.exit("ERROR: provide --script (Pipeline A) or --input-video (Pipeline B)")

    if not args.resume and not portrait.exists():
        sys.exit(f"ERROR: portrait.jpg not found at {portrait}")

    log(f"=== Video pipeline: influencer={args.influencer}, job={job} ===")
    if args.preview_only:
        log("    Mode: Stage 1 (preview only — no MuseTalk)")
    elif args.resume:
        log("    Mode: Stage 2 (resume from Stage 1 temp files)")
    else:
        log(f"    Mode: Full pipeline ({'A autonomous' if pipeline_a else 'B user video'})")

    try:
        if args.preview_only:
            stage1(args, infl_dir, job, pipeline_a, pipeline_b)
        elif args.resume:
            stage2(args, infl_dir, job)
        else:
            # Full pipeline: stage1 then immediately stage2
            # Stage 1
            stage1(args, infl_dir, job, pipeline_a, pipeline_b)
            # Stage 2: output goes to final, stage1 wrote preview to args.output
            # Need separate output paths — use temp for stage1, final for stage2
            preview_out = args.output
            # Re-run stage2 with the swap/voice already written by stage1
            log("--- Continuing to Stage 2 (full render) ---")
            stage2(args, infl_dir, job)
            # stage2 writes to args.output (overwrites preview — final wins)
    except Exception as e:
        log(f"PIPELINE FAILED: {e}")
        write_sentinel(job, "error", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
