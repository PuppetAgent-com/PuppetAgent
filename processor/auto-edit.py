#!/usr/bin/env python3
"""
auto-edit.py — Edit a teleprompter recording for TikTok delivery.

Steps:
  1. Transcribe with Whisper (base model, word timestamps)
  2. Remove repeated takes (>=75% text similarity within 90s window)
  3. Compress silences > 0.4s down to 0.4s
  4. Re-encode with ffmpeg trim+concat

Saves transcript JSON containing the TEXT of the kept (edited) content,
so the XTTS clone step can skip re-transcription.

Usage:
  python3 auto-edit.py <input.mp4> <output.mp4> [transcript.json]
"""

import sys, json, re, difflib, subprocess, tempfile
from pathlib import Path

MAX_SILENCE   = 0.4    # gaps > this get compressed to this duration (seconds)
REPEAT_THRESH = 0.75   # SequenceMatcher ratio threshold for "repeated take"
REPEAT_WINDOW = 90.0   # only compare against segments ending within last N seconds
LEAD_IN       = 0.3    # buffer to keep before first segment (seconds)


def ffmpeg(*args):
    r = subprocess.run(["ffmpeg"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{r.stderr[-600:]}")
    return r


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


def main():
    if len(sys.argv) < 3:
        sys.exit("Usage: auto-edit.py <input.mp4> <output.mp4> [transcript.json]")

    video_in       = sys.argv[1]
    video_out      = sys.argv[2]
    transcript_out = sys.argv[3] if len(sys.argv) > 3 else None

    # ── Extract mono 16kHz WAV for Whisper ──────────────────────────────────
    audio_tmp = Path(tempfile.mktemp(suffix=".wav"))
    print(f"Extracting audio from {Path(video_in).name}...")
    ffmpeg("-y", "-i", video_in, "-vn", "-ar", "16000", "-ac", "1", str(audio_tmp))

    # ── Whisper transcription ────────────────────────────────────────────────
    print("Transcribing with Whisper (base model)...")
    import whisper
    model  = whisper.load_model("base")
    result = model.transcribe(str(audio_tmp), word_timestamps=True, language="en")
    audio_tmp.unlink(missing_ok=True)

    segments  = result.get("segments", [])
    if not segments:
        print("Whisper found no speech — copying input unchanged")
        import shutil; shutil.copy2(video_in, video_out)
        if transcript_out:
            Path(transcript_out).write_text(json.dumps({"text": "", "segments": []}))
        return

    video_dur = segments[-1]["end"]
    print(f"Original: {video_dur:.1f}s, {len(segments)} Whisper segments")

    # ── Detect and remove repeated takes ────────────────────────────────────
    keep   = []
    recent = []   # (normalized_text, end_time) most-recent last
    n_cut  = 0

    for seg in segments:
        norm = normalize(seg["text"])
        if len(norm) < 8:
            # Too short to be a meaningful take — always keep
            keep.append(seg)
            recent.append((norm, seg["end"]))
            continue

        is_repeat = False
        for prev_norm, prev_end in reversed(recent):
            if seg["start"] - prev_end > REPEAT_WINDOW:
                break
            ratio = difflib.SequenceMatcher(None, norm, prev_norm).ratio()
            if ratio >= REPEAT_THRESH:
                is_repeat = True
                print(f"  [CUT repeat] {seg['start']:.1f}s  \"{seg['text'][:55]}\"  "
                      f"(sim={ratio:.2f})")
                n_cut += 1
                break

        if not is_repeat:
            keep.append(seg)
        recent.append((norm, seg["end"]))

    print(f"Removed {n_cut} repeated takes — keeping {len(keep)}/{len(segments)} segments")

    # ── Build time intervals (compress long silences) ────────────────────────
    intervals = []
    for seg in keep:
        s = max(0.0, seg["start"] - LEAD_IN) if not intervals else seg["start"]
        e = seg["end"]
        if not intervals:
            intervals.append([s, e])
        else:
            gap = s - intervals[-1][1]
            if gap <= MAX_SILENCE:
                intervals[-1][1] = e        # extend — silence is short enough
            else:
                intervals.append([s, e])    # new interval — skip the gap

    out_dur = sum(e - s for s, e in intervals)
    print(f"Output: {out_dur:.1f}s  ({100*out_dur/video_dur:.0f}% of original, "
          f"{len(intervals)} intervals)")

    # ── Save edited transcript (text of kept segments only) ──────────────────
    if transcript_out:
        edited_text = " ".join(seg["text"].strip() for seg in keep)
        Path(transcript_out).write_text(json.dumps({
            "text":          edited_text,
            "segments":      keep,
            "full_text":     result.get("text", ""),
            "original_segs": len(segments),
            "kept_segs":     len(keep),
        }))
        print(f"Transcript saved → {transcript_out}  ({len(edited_text)} chars)")

    # ── Build ffmpeg filter_complex ──────────────────────────────────────────
    n     = len(intervals)
    parts = []
    for i, (s, e) in enumerate(intervals):
        parts.append(f"[0:v]trim=start={s:.4f}:end={e:.4f},setpts=PTS-STARTPTS[v{i}];")
        parts.append(f"[0:a]atrim=start={s:.4f}:end={e:.4f},asetpts=PTS-STARTPTS[a{i}];")
    # ffmpeg concat requires interleaved: [v0][a0][v1][a1]...
    va_labels = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{va_labels}concat=n={n}:v=1:a=1[outv][outa]")

    # Write to temp file to avoid shell arg length limits
    fc_tmp = Path(tempfile.mktemp(suffix=".txt"))
    fc_tmp.write_text("".join(parts))

    print(f"Re-encoding with ffmpeg ({n} intervals)...")
    ffmpeg("-y", "-i", video_in,
           "-filter_complex_script", str(fc_tmp),
           "-map", "[outv]", "-map", "[outa]",
           "-c:v", "libx264", "-crf", "18", "-preset", "fast",
           "-c:a", "aac", "-b:a", "192k",
           video_out)
    fc_tmp.unlink(missing_ok=True)

    size_mb = Path(video_out).stat().st_size / 1e6
    print(f"Done: {video_out}  ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
