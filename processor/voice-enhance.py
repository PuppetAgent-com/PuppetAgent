#!/usr/bin/env python3
"""
voice-enhance.py — Broadcast-quality voice processing for teleprompter recordings.

Pipeline:
  1. Extract audio from video (48kHz mono — DeepFilterNet native rate)
  2. DeepFilterNet v2 — neural noise suppression (room noise, AC, echo)
  3. ffmpeg broadcast chain:
       highpass(80Hz) → compress(3:1) → EQ presence boost → limiter
  4. Output clean 22050Hz mono WAV ready for overlay / MuseTalk

Usage:
  python3 voice-enhance.py <input_video_or_audio> <output.wav>
"""

import sys, subprocess, tempfile
from pathlib import Path


BROADCAST_CHAIN = ",".join([
    "highpass=f=80",                                          # remove rumble
    "rubberband=pitch=0.944",                                 # lower pitch 1 semitone (subtly deeper)
    "acompressor=threshold=-18dB:ratio=3:attack=5:release=100:makeup=4dB",  # broadcast compression
    "equalizer=f=200:width_type=o:width=2:g=-2",             # cut muddiness
    "equalizer=f=3000:width_type=o:width=1:g=3",             # boost presence / clarity
    "equalizer=f=8000:width_type=o:width=2:g=1.5",           # add air / brightness
    "alimiter=limit=0.9:level=false",                         # prevent clipping
])


def ffmpeg(*args):
    r = subprocess.run(["ffmpeg"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{r.stderr[-600:]}")


def main():
    if len(sys.argv) < 3:
        sys.exit("Usage: voice-enhance.py <input> <output.wav>")

    src     = sys.argv[1]
    out_wav = sys.argv[2]
    tmp     = Path(tempfile.mkdtemp())

    raw_wav     = str(tmp / "raw_48k.wav")
    denoised_wav = str(tmp / "denoised_48k.wav")

    # ── Step 1: Extract audio at 48kHz (DeepFilterNet native rate) ───────────
    print("Extracting audio at 48kHz...", flush=True)
    ffmpeg("-y", "-i", src,
           "-vn", "-ar", "48000", "-ac", "1",
           "-acodec", "pcm_s16le", raw_wav)

    # ── Step 2: DeepFilterNet noise suppression ──────────────────────────────
    print("Running DeepFilterNet noise suppression...", flush=True)
    try:
        from df.enhance import enhance, init_df, load_audio, save_audio
        model, df_state, _ = init_df()
        audio, _ = load_audio(raw_wav, sr=df_state.sr())
        enhanced = enhance(model, df_state, audio)
        save_audio(denoised_wav, enhanced, df_state.sr())
        print(f"  DeepFilterNet done → {Path(denoised_wav).stat().st_size // 1024} KB", flush=True)
    except Exception as e:
        print(f"  WARNING: DeepFilterNet failed ({e}) — using raw audio", flush=True)
        denoised_wav = raw_wav

    # ── Step 3: Broadcast processing chain ───────────────────────────────────
    print("Applying broadcast chain (compress + EQ + limit)...", flush=True)
    ffmpeg("-y", "-i", denoised_wav,
           "-af", BROADCAST_CHAIN,
           "-ar", "22050", "-ac", "1",
           "-acodec", "pcm_s16le", out_wav)

    # Cleanup
    for f in [raw_wav, denoised_wav]:
        Path(f).unlink(missing_ok=True)
    tmp.rmdir()

    size_kb = Path(out_wav).stat().st_size // 1024
    print(f"Done: {out_wav}  ({size_kb} KB)", flush=True)


if __name__ == "__main__":
    main()
