#!/usr/bin/env python3
"""
auto-edit.py — Edit a teleprompter recording for TikTok delivery.

Steps:
  1. Transcribe with Whisper (base model, word timestamps)
  2. Remove repeated takes — script-line-aware pass first, then text similarity fallback
  3. Compress silences > 0.4s (0.2s at known script-line transitions)
  4. Re-encode with ffmpeg trim+concat
  5. Generate SRT subtitle file with remapped timestamps (optional)

Teleprompter context (--context):
  JSON file with:
    vosk_transcript  — what Vosk heard on-device (rough guide to timing / word order)
    script_lines     — JSON array of the original teleprompter lines

  When script_lines is provided, repeat detection uses script-line alignment:
  multiple Whisper segments mapped to the same line → keep only the LAST take.
  Gaps between different script lines are compressed more aggressively (0.2s vs 0.4s).

Saves transcript JSON (including vosk_transcript + script_lines) for downstream steps.

Usage:
  auto-edit.py --input <in.mp4> --output <out.mp4> [--transcript t.json] [--srt s.srt]
               [--context context.json]
"""

import argparse, sys, json, re, difflib, subprocess, tempfile
from pathlib import Path

MAX_SILENCE        = 0.4    # within-line gap: compress to this (seconds)
INTER_LINE_SILENCE = 0.2    # gap at a known script-line boundary: compress to this
REPEAT_THRESH      = 0.75   # similarity ratio for fallback repeat detection
REPEAT_WINDOW      = 90.0   # only look back this far when checking for repeats
LEAD_IN            = 0.3    # buffer to keep before first segment


def ffmpeg(*args):
    r = subprocess.run(["ffmpeg"] + list(args), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{r.stderr[-600:]}")
    return r


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()


# ── Repeat detection ──────────────────────────────────────────────────────────

def _best_script_line(norm_seg: str, norm_lines: list) -> int:
    """Return index of best matching script line, -1 if below 0.35 threshold."""
    best_idx, best_r = -1, 0.35
    for i, nl in enumerate(norm_lines):
        r = difflib.SequenceMatcher(None, norm_seg, nl).ratio()
        if r > best_r:
            best_r, best_idx = r, i
    return best_idx


def dedup_segments(segments, script_lines=None):
    """
    Two-pass repeat detection.

    Pass 1 (script-line-aware, only when script_lines provided):
      Assign each Whisper segment to the closest script line (fuzzy match).
      For each script line, keep only the LAST take within REPEAT_WINDOW.
      Earlier takes of the same line are cut — the speaker restarted because
      they weren't happy, so the last take is the intended delivery.

    Pass 2 (similarity fallback):
      For segments not matched to any script line, compare text similarity
      against recent kept segments (original 0.75 ratio logic).

    Returns: (kept_segments, assignments_for_kept)
      assignments_for_kept: script-line index (-1 = unmatched) for each kept segment.
    """
    norm_lines = [normalize(l) for l in script_lines] if script_lines else []
    n = len(segments)

    # Assign each segment to a script line
    assignments = []
    for seg in segments:
        idx = _best_script_line(normalize(seg["text"]), norm_lines) if norm_lines else -1
        assignments.append(idx)

    # Pass 1: for each script line, find the last assigned segment within the window
    cut = [False] * n
    last_for_line = {}   # line_idx → segment index
    n_script_cut = 0

    for i, (seg, line_idx) in enumerate(zip(segments, assignments)):
        if line_idx == -1:
            continue
        prev_i = last_for_line.get(line_idx)
        if prev_i is not None:
            prev = segments[prev_i]
            if seg["start"] - prev["start"] <= REPEAT_WINDOW:
                cut[prev_i] = True
                n_script_cut += 1
                print(f"  [CUT script-line {line_idx}] {prev['start']:.1f}s "
                      f"→ retake at {seg['start']:.1f}s  \"{prev['text'][:45]}\"")
        last_for_line[line_idx] = i

    # Pass 2: similarity fallback for unmatched segments
    recent = []   # (norm_text, end_time) of segments we've kept so far
    n_sim_cut = 0

    for i, (seg, line_idx) in enumerate(zip(segments, assignments)):
        norm = normalize(seg["text"])
        if cut[i]:
            # Already marked — add to recent so similarity check can use it
            recent.append((norm, seg["end"]))
            continue

        if line_idx != -1:
            # Handled by pass 1 — just track
            recent.append((norm, seg["end"]))
            continue

        if len(norm) < 8:
            recent.append((norm, seg["end"]))
            continue

        is_repeat = False
        for prev_norm, prev_end in reversed(recent):
            if seg["start"] - prev_end > REPEAT_WINDOW:
                break
            if difflib.SequenceMatcher(None, norm, prev_norm).ratio() >= REPEAT_THRESH:
                is_repeat = True
                print(f"  [CUT sim-repeat] {seg['start']:.1f}s  \"{seg['text'][:55]}\"")
                n_sim_cut += 1
                break

        if is_repeat:
            cut[i] = True
        recent.append((norm, seg["end"]))

    total_cut = n_script_cut + n_sim_cut
    print(f"Removed {n_script_cut} script-line retakes + {n_sim_cut} similarity repeats "
          f"— keeping {n - total_cut}/{n} segments")

    kept_pairs = [(seg, assignments[i]) for i, seg in enumerate(segments) if not cut[i]]
    kept_segs    = [s for s, _ in kept_pairs]
    kept_assigns = [a for _, a in kept_pairs]
    return kept_segs, kept_assigns


# ── Interval building ─────────────────────────────────────────────────────────

def build_intervals(keep_segs, keep_assigns):
    """
    Build ffmpeg trim intervals from kept segments, compressing silence.

    Gaps between segments from different known script lines are compressed
    to INTER_LINE_SILENCE (0.2s) — cleaner cross-line transitions.
    All other gaps > MAX_SILENCE (0.4s) are compressed to MAX_SILENCE.
    """
    intervals = []
    for i, seg in enumerate(keep_segs):
        s = max(0.0, seg["start"] - LEAD_IN) if not intervals else seg["start"]
        e = seg["end"]
        if not intervals:
            intervals.append([s, e])
        else:
            gap = s - intervals[-1][1]
            # At a known line boundary, allow shorter silence (tighter cut)
            prev_line = keep_assigns[i - 1]
            curr_line = keep_assigns[i]
            at_boundary = (prev_line >= 0 and curr_line >= 0 and prev_line != curr_line)
            limit = INTER_LINE_SILENCE if at_boundary else MAX_SILENCE
            if gap <= limit:
                intervals[-1][1] = e
            else:
                intervals.append([s, e])
    return intervals


# ── SRT generation ────────────────────────────────────────────────────────────

def generate_srt(keep_segments: list, intervals: list, srt_path: str,
                 words_per_card: int = 6):
    """Generate SRT from Whisper word-level timestamps, remapped to the edited timeline."""
    offsets = []
    new_t = 0.0
    for (orig_s, orig_e) in intervals:
        offsets.append((orig_s, orig_e, new_t))
        new_t += orig_e - orig_s

    def remap(t: float):
        for orig_s, orig_e, new_s in offsets:
            if orig_s - 0.1 <= t <= orig_e + 0.1:
                return max(0.0, new_s + (t - orig_s))
        return None

    def fmt_time(sec: float) -> str:
        ms = int(sec * 1000)
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms  = divmod(rem, 1_000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    cards = []
    word_buf = []

    for seg in keep_segments:
        words = seg.get("words", [])
        if not words:
            t_s = remap(seg["start"])
            t_e = remap(seg["end"])
            if t_s is not None and t_e is not None and t_e > t_s:
                cards.append({"start": t_s, "end": t_e, "text": seg["text"].strip()})
            continue
        for w in words:
            t_s = remap(w.get("start", seg["start"]))
            if t_s is None:
                continue
            raw_end = w.get("end", w.get("start", 0) + 0.25)
            t_e = remap(raw_end) or t_s + 0.25
            word_buf.append({"word": w["word"].strip(), "t_start": t_s, "t_end": t_e})
            if len(word_buf) >= words_per_card:
                cards.append({
                    "start": word_buf[0]["t_start"],
                    "end":   word_buf[-1]["t_end"],
                    "text":  " ".join(ww["word"] for ww in word_buf),
                })
                word_buf = []

    if word_buf:
        cards.append({
            "start": word_buf[0]["t_start"],
            "end":   word_buf[-1]["t_end"],
            "text":  " ".join(ww["word"] for ww in word_buf),
        })

    if not cards:
        print("SRT: no cards generated — skipping SRT write")
        return

    lines = []
    for i, c in enumerate(cards, 1):
        lines += [str(i), f"{fmt_time(c['start'])} --> {fmt_time(c['end'])}", c["text"], ""]
    Path(srt_path).write_text("\n".join(lines), encoding="utf-8")
    print(f"SRT saved → {srt_path}  ({len(cards)} cards, {new_t:.1f}s)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Auto-edit teleprompter recording")
    ap.add_argument("--input",      "-i", required=True,  help="Input video")
    ap.add_argument("--output",     "-o", required=True,  help="Output edited video")
    ap.add_argument("--transcript",       default=None,   help="Path to save transcript JSON")
    ap.add_argument("--srt",              default=None,   help="Path to save SRT file")
    ap.add_argument("--context",          default=None,
                    help="JSON file with {vosk_transcript, script_lines} from teleprompter")
    args = ap.parse_args()

    video_in       = args.input
    video_out      = args.output
    transcript_out = args.transcript
    srt_out        = args.srt

    # Load teleprompter context if provided
    script_lines    = []
    vosk_transcript = ""
    if args.context:
        try:
            ctx = json.loads(Path(args.context).read_text())
            vosk_transcript = ctx.get("vosk_transcript", "")
            raw_lines = ctx.get("script_lines", "[]")
            if isinstance(raw_lines, str):
                raw_lines = json.loads(raw_lines)
            script_lines = [l for l in raw_lines if l.strip()]
            print(f"Context loaded: {len(script_lines)} script lines, "
                  f"{len(vosk_transcript)} chars Vosk transcript")
        except Exception as e:
            print(f"WARNING: Could not load context file ({e}) — continuing without it")

    # Extract mono 16kHz WAV for Whisper
    audio_tmp = Path(tempfile.mktemp(suffix=".wav"))
    print(f"Extracting audio from {Path(video_in).name}...")
    ffmpeg("-y", "-i", video_in, "-vn", "-ar", "16000", "-ac", "1", str(audio_tmp))

    # Whisper transcription
    print("Transcribing with Whisper (base model)...")
    import whisper
    model  = whisper.load_model("base")
    result = model.transcribe(str(audio_tmp), word_timestamps=True, language="en")
    audio_tmp.unlink(missing_ok=True)

    segments = result.get("segments", [])
    if not segments:
        print("Whisper found no speech — copying input unchanged")
        import shutil; shutil.copy2(video_in, video_out)
        if transcript_out:
            Path(transcript_out).write_text(json.dumps({"text": "", "segments": []}))
        return

    video_dur = segments[-1]["end"]
    print(f"Original: {video_dur:.1f}s, {len(segments)} Whisper segments")
    if script_lines:
        print(f"Using {len(script_lines)} script lines for smart dedup")

    # Dedup: script-line pass + similarity fallback
    keep, keep_assigns = dedup_segments(segments, script_lines or None)

    # Build intervals with script-aware silence compression
    intervals = build_intervals(keep, keep_assigns)
    out_dur = sum(e - s for s, e in intervals)
    print(f"Output: {out_dur:.1f}s  ({100*out_dur/video_dur:.0f}% of original, "
          f"{len(intervals)} intervals)")

    # Save transcript JSON (includes vosk_transcript + script_lines for downstream)
    if transcript_out:
        edited_text = " ".join(seg["text"].strip() for seg in keep)
        Path(transcript_out).write_text(json.dumps({
            "text":            edited_text,
            "segments":        keep,
            "full_text":       result.get("text", ""),
            "original_segs":   len(segments),
            "kept_segs":       len(keep),
            "intervals":       intervals,
            "vosk_transcript": vosk_transcript,
            "script_lines":    script_lines,
        }))
        print(f"Transcript saved → {transcript_out}  ({len(edited_text)} chars)")

    # Generate SRT
    if srt_out:
        try:
            generate_srt(keep, intervals, srt_out)
        except Exception as e:
            print(f"WARNING: SRT generation failed ({e}) — continuing without SRT")

    # Build ffmpeg filter_complex and re-encode
    n     = len(intervals)
    parts = []
    for i, (s, e) in enumerate(intervals):
        parts.append(f"[0:v]trim=start={s:.4f}:end={e:.4f},setpts=PTS-STARTPTS[v{i}];")
        parts.append(f"[0:a]atrim=start={s:.4f}:end={e:.4f},asetpts=PTS-STARTPTS[a{i}];")
    va_labels = "".join(f"[v{i}][a{i}]" for i in range(n))
    parts.append(f"{va_labels}concat=n={n}:v=1:a=1[outv][outa]")

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
