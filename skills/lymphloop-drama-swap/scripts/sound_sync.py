#!/usr/bin/env python3
"""Which sound goes under a generated picture (user, 2026-11-11: "后半段总是有一些口型对不上").

Seedance native edit keeps the reference's mouth movement frame for frame, but the sound it generates drifts: on
2026-11-11 Nuflos G05 dropped the first line and spoke everything else 1.0 s early, Metabolae G02 spoke 0.2 s early,
while the mouths in both were identical to the source frame by frame. The recut itself was exact (0 ms on every piece).
So:
- a segment with no rewritten line takes the SOURCE sound under the generated picture (original voices, exact sync);
- a segment with a rewritten line keeps the generated sound, and its drift against the source is measured so the agent
  can rebuild the sound with `swap.py splice --audio ... --audio-window ...` when it is off.
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import subprocess
from pathlib import Path
import numpy as np
import media_pipeline as media

SR = 16000
HOP = 160            # 10 ms
DRIFT_OK_MS = 80     # about two frames: below this a viewer does not see the lips slip


def envelope(path, start_s, dur_s):
    """Log speech-band energy every 10 ms."""
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-ss', f'{start_s:.4f}', '-t', f'{dur_s:.4f}', '-i', str(path), '-vn', '-ac', '1',
                          '-ar', str(SR), '-af', 'highpass=f=250,lowpass=f=3400', '-f', 's16le', '-'], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32)
    n = len(a) // HOP
    if n == 0: return np.zeros(0, np.float32)
    return np.log1p(np.sqrt((a[:n * HOP].reshape(n, HOP) ** 2).mean(axis=1)))


def lag_ms(source_env, cand_env, max_ms=1500):
    """(lag, correlation): negative lag = the candidate speaks EARLIER than the source by that many ms."""
    n = min(len(source_env), len(cand_env))
    if n < 50: return 0, 0.0
    s = source_env[:n] - source_env[:n].mean(); c = cand_env[:n] - cand_env[:n].mean()
    if s.std() < 1e-6 or c.std() < 1e-6: return 0, 0.0
    s /= s.std(); c /= c.std()
    best = (-2.0, 0)
    reach = min(max_ms // 10, n // 2)                 # a short clip cannot show a lag longer than half of itself
    for k in range(-reach, reach + 1):
        v = float((c[k:] * s[:n - k]).mean()) if k >= 0 else float((c[:n + k] * s[-k:]).mean())
        if v > best[0]: best = (v, k)
    return best[1] * 10, round(best[0], 3)


def drift(master, start_s, candidate, dur_s):
    """How far the candidate's speech is from the source's at the same picture time."""
    ms, corr = lag_ms(envelope(master, start_s, dur_s), envelope(candidate, 0.0, dur_s))
    return {'ms': ms, 'corr': corr, 'ok': abs(ms) <= DRIFT_OK_MS or corr < 0.25}


def with_source_sound(candidate, master, start_s, out):
    """The candidate's picture untouched (stream copy) with the source sound of the same moments."""
    candidate, out = Path(candidate), Path(out)
    dur = float(media.probe(str(candidate))['video_duration'])
    media.ff(['-y', '-i', str(candidate), '-ss', f'{start_s:.6f}', '-t', f'{dur:.6f}', '-i', str(master), '-map', '0:v:0', '-map', '1:a:0',
              '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', '-t', f'{dur:.6f}', '-movflags', '+faststart', str(out)])
    return out
