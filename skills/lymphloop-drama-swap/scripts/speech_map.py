#!/usr/bin/env python3
"""Sentence timing for a stretch of the working master, and beat-aware segmentation of a replace run.

Why: a generated segment is judged only on itself; if its in/out points cut a sentence or separate a product
shot from the line it illustrates, the recut can never line up again. So a segment is a block between two locks
(scenes.py: paragraph / shot locks, lock_blocks); a block is cut inside only when it is longer than 14.9 s, at
shot cuts in sentence pauses (semantic breaks first), never across a sentence. 14.9 s is a ceiling, not a target.

  speech_map.py transcribe RUN START_S END_S      # cached Gemini Flash sentence timestamps (absolute seconds)
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, json, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MODEL = 'gemini-3.8-flash'
PROMPT_VERSION = 'sentences-v1'
PROMPT = ('Transcribe every spoken sentence in this audio verbatim, one complete sentence (or clearly separate clause) per line, '
          'with start and end seconds measured from the start of this clip. Label the speaker briefly (woman A, man B, narrator). '
          'Mark kind as on_screen_dialogue or voice_over (narration from someone not visibly speaking; if unsure, on_screen_dialogue). '
          'Keep a sentence that continues after a short breath as one line. Do not guess unclear words; write [unclear].')
SCHEMA = {'type': 'object', 'properties': {'lines': {'type': 'array', 'items': {'type': 'object', 'properties': {
    'start_s': {'type': 'number'}, 'end_s': {'type': 'number'}, 'speaker': {'type': 'string'},
    'kind': {'type': 'string', 'enum': ['on_screen_dialogue', 'voice_over']}, 'text': {'type': 'string'}},
    'required': ['start_s', 'end_s', 'speaker', 'kind', 'text']}}}, 'required': ['lines']}

BEAT_GAP = 0.8      # a pause at least this long, with a shot cut inside it, ends a beat
TOLERANCE = 0.25    # ASR timestamps are good to roughly a quarter second
TIGHT_PAUSE = 0.35  # lines this close are one line still running (ASR splits "Six weeks. You're dancing…" with 0.28 s)
MIN_SEGMENT = 3.0   # shorter beats are merged into a neighbour when that still fits
MAX_SEGMENT = 14.9  # Wan rejects a reference of 15 s or more


def transcribe(run, master, start_s, end_s, margin=0.4):
    """Absolute-time sentences for [start_s, end_s) of the master; cached per audio sha + prompt version.
    In Antigravity native mode, uses local faster-whisper with pause-gap splitting (zero external Gemini API)."""
    import google_api as api
    import agent_native
    run = Path(run); out_dir = run/'plan'/'speech'; out_dir.mkdir(parents=True, exist_ok=True)
    a, b = max(0.0, start_s - margin), end_s + margin
    if agent_native.is_native():
        key = api.sha(api.json_bytes({'master': api.file_sha(master), 'a': round(a, 3), 'b': round(b, 3), 'engine': 'local-whisper-v1'}))
        cache = out_dir/f'{key[:24]}.json'
        if cache.is_file():
            return json.loads(cache.read_text())['sentences']
        sentences = agent_native.whisper_sentences(master, start_s=a, end_s=b, gap_s=0.7)
        sentences = [s for s in sentences if s['end'] > start_s and s['start'] < end_s]
        cache.write_text(json.dumps({'window': [a, b], 'model': 'faster-whisper-local', 'prompt_version': 'local-whisper-v1', 'sentences': sentences}, ensure_ascii=False, indent=1))
        return sentences
    # A review run transcribes several candidates at the same time window.  Include
    # the media hash: reusing a time-only WAV silently checks later candidates
    # against the first candidate's audio.
    wav = out_dir/f'{a:.3f}-{b:.3f}-{api.file_sha(master)[:16]}.wav'
    import media_pipeline
    ffmpeg = media_pipeline.binary('ffmpeg')
    if not wav.is_file():
        subprocess.run([ffmpeg, '-y', '-v', 'error', '-ss', f'{a:.3f}', '-t', f'{b - a:.3f}', '-i', str(master), '-vn', '-ac', '1', '-ar', '16000', str(wav)], check=True)
    key = api.sha(api.json_bytes({'audio': api.file_sha(wav), 'model': MODEL, 'v': PROMPT_VERSION}))
    cache = out_dir/f'{key[:24]}.json'
    if cache.is_file(): return json.loads(cache.read_text())['sentences']
    transport = api.Transport(api.required_key())
    audio = api.media_part(str(wav), str(run), transport)      # inline when small (2026-11-22: upload 429 with 25 films at once)
    payload = {'contents': [{'role': 'user', 'parts': [{'text': PROMPT}, audio]}],
               'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json', 'responseSchema': SCHEMA}}
    resp = transport.json(api.API + '/v1beta/models/' + MODEL + ':generateContent', method='POST', payload=payload, timeout=300)
    text = '\n'.join(p.get('text', '') for c in resp.get('candidates', []) for p in c.get('content', {}).get('parts', []) if not p.get('thought'))
    lines = json.loads(text)['lines']
    sentences = [{'start': round(a + l['start_s'], 3), 'end': round(a + l['end_s'], 3), 'speaker': l['speaker'], 'kind': l['kind'], 'text': l['text']}
                 for l in lines if l['end_s'] > l['start_s']]
    sentences = [s for s in sentences if s['end'] > start_s and s['start'] < end_s]
    cache.write_text(json.dumps({'window': [a, b], 'model': MODEL, 'prompt_version': PROMPT_VERSION, 'sentences': sentences}, ensure_ascii=False, indent=1))
    return sentences


CHUNK_S = 30.0      # longer stretches are transcribed in chunks: Flash timestamps drift ~0.5 s over a 160 s clip
CHUNK_PAD = 2.0


def transcribe_chunked(run, master, start_s, end_s, cuts=(), chunk=CHUNK_S, pad=CHUNK_PAD):
    """transcribe() for long paragraphs: ≤chunk-second cores ending on a hard cut where possible, each sent with
    `pad` seconds of audio either side; a sentence is kept from the chunk whose core holds its midpoint, so a line
    across a core edge is read whole once. Short ranges are one call, exactly as before."""
    if end_s - start_s <= chunk + 1e-6: return transcribe(run, master, start_s, end_s)
    cuts = sorted(c for c in cuts if start_s < c < end_s)
    edges = [start_s]
    while end_s - edges[-1] > chunk:
        inside = [c for c in cuts if edges[-1] + chunk / 2 <= c <= edges[-1] + chunk]
        edges.append(inside[-1] if inside else edges[-1] + chunk)
    edges.append(end_s)
    out = []
    for c0, c1 in zip(edges, edges[1:]):
        for z in transcribe(run, master, c0, c1, margin=pad):
            mid = (z['start'] + z['end']) / 2
            if c0 <= mid < c1 or (c1 == end_s and mid >= c1):
                if out and z['start'] < out[-1]['end'] and (min(z['end'], out[-1]['end']) - z['start']) > 0.5 * (z['end'] - z['start']): continue
                out.append(z)
    return sorted(out, key=lambda z: z['start'])


QUIET_EDGE = 0.5   # a quiet cut forgives a transcript edge this close, never a cut deep inside a sentence


def _crossing(x, sentences, tol, quiet=()):
    """Does a sentence run across time x? At a quiet cut (the soundtrack dips right on it) a sentence whose
    transcribed start or end is within QUIET_EDGE of x does not count: that is ASR timing error, not speech."""
    hits = [s for s in sentences if s['start'] + tol < x < s['end'] - tol]
    if hits and any(abs(x - q) < 1e-6 for q in quiet):
        hits = [s for s in hits if min(abs(x - s['start']), abs(s['end'] - x)) > QUIET_EDGE]
    return bool(hits)


def _gap_around(t, sentences, tol):
    """(previous sentence end, next sentence start) around time t, ignoring sentences that only graze t."""
    prev = [s['end'] for s in sentences if s['end'] <= t + tol]
    nxt = [s['start'] for s in sentences if s['start'] >= t - tol]
    return (max(prev) if prev else None), (min(nxt) if nxt else None)


SEMANTIC_PROMPT = ('These are consecutive spoken lines from one scene of a video ad, numbered. A long stretch must be split into two or more '
                   'clips, and each clip is regenerated on its own, so every clip has to start where a viewer would accept a fresh beat. '
                   'For each candidate split point (between line k and line k+1), say whether it is a good beat break: a new topic, a new '
                   'question, a change of speaker that starts a new exchange, or the move from story to product facts. A split inside a '
                   'question and its answer, inside a list, or between a product name and the sentence describing it is not good. '
                   'A reply that responds to what the other person just said (question then answer, apology then response, '
                   'claim then reaction) belongs to the same exchange: splitting between them is not good, even though the speaker changes.')
SEMANTIC_SCHEMA = {'type': 'object', 'properties': {'breaks': {'type': 'array', 'items': {'type': 'object', 'properties': {
    'after_line': {'type': 'integer'}, 'good': {'type': 'boolean'}, 'reason': {'type': 'string'}}, 'required': ['after_line', 'good', 'reason']}}},
    'required': ['breaks']}


def semantic_breaks(run, sentences, after_lines):
    """Ask Gemini Flash (or local pause/punctuation heuristics in Antigravity native mode) which candidate
    split points (after sentence index k) are real beat breaks. Cached."""
    import google_api as api
    import agent_native
    if not after_lines: return {}
    if agent_native.is_native():
        result = {}
        for idx in sorted(after_lines):
            if idx < 0 or idx >= len(sentences):
                continue
            cur = sentences[idx]
            nxt = sentences[idx + 1] if idx + 1 < len(sentences) else None
            txt = (cur.get('text') or '').strip()
            gap = (float(nxt['start']) - float(cur['end'])) if nxt else 1.0
            is_q = txt.endswith('?')
            good = (not is_q) and (gap >= 0.55 or txt.endswith('.') or txt.endswith('!'))
            reason = ('question awaiting answer' if is_q
                      else f'local beat break (gap={gap:.2f}s, punct={txt[-1:]!r})' if good
                      else f'tight continuation (gap={gap:.2f}s)')
            result[idx] = {'good': good, 'reason': reason}
        return result
    text = '\n'.join(f'{i}: [{s["speaker"]}] {s["text"]}' for i, s in enumerate(sentences))
    ask = SEMANTIC_PROMPT + '\n\nLines:\n' + text + '\n\nCandidate split points (after line): ' + ', '.join(map(str, sorted(after_lines)))
    out_dir = Path(run)/'plan'/'speech'; out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir/f"sem-{api.sha(api.json_bytes({'ask': ask, 'model': MODEL}))[:24]}.json"
    if cache.is_file(): return {int(k): v for k, v in json.loads(cache.read_text()).items()}
    transport = api.Transport(api.required_key())
    payload = {'contents': [{'role': 'user', 'parts': [{'text': ask}]}],
               'generationConfig': {'temperature': 0, 'responseMimeType': 'application/json', 'responseSchema': SEMANTIC_SCHEMA}}
    resp = transport.json(api.API + '/v1beta/models/' + MODEL + ':generateContent', method='POST', payload=payload, timeout=120)
    raw = '\n'.join(p.get('text', '') for c in resp.get('candidates', []) for p in c.get('content', {}).get('parts', []) if not p.get('thought'))
    result = {b['after_line']: {'good': b['good'], 'reason': b['reason']} for b in json.loads(raw)['breaks'] if b['after_line'] in after_lines}
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result


def forced_candidates(spans, sentences, groups, notes, tol=TOLERANCE, quiet=()):
    """Sentence-safe boundaries inside pieces that were split only because of the length ceiling, as
    {boundary index k: index of the last sentence before it}."""
    t = [spans[0][0]] + [spans[k][0] for k in range(1, len(spans))] + [spans[-1][1]]
    out = {}
    for (i, j), note in zip(groups, notes):
        if not note.get('forced_split'): continue
        lo, hi = note['parent']
        for k in range(lo + 1, hi):
            if _crossing(t[k], sentences, tol, quiet): continue
            before = [n for n, s in enumerate(sentences) if s['end'] <= t[k] + tol]
            if before: out[k] = before[-1]
    return out


def lock_blocks(spans, product, locks, sentences, max_s=MAX_SEGMENT, tol=TOLERANCE, quiet=()):
    """Which shots get regenerated (user rule, 2026-10-24): the video is cut into blocks at every LOCK (scenes.py:
    'paragraph' = a completely different environment, 'shot' = same place but no continuity). A block that contains a
    product shot is regenerated whole: its edges were explored outward until a lock, so reaction shots and the other
    speaker inside one continuous moment come with it. A line is never split: when a sentence runs across a SHOT lock
    the two blocks merge (if the result fits max_s); a PARAGRAPH lock is never crossed, a sentence across it is flagged.
    Product-free segments that segment_run later cuts out of a long block are not generated (see swap.cmd_plan).

    spans: [(start, end)] per shot; product: [bool]; locks: {shot index k: 'paragraph'|'shot'} for the cut before shot k.
    Returns [(i, j, notes)] shot ranges [i, j) with notes like ['sentence crosses the paragraph lock before S004'].
    """
    n = len(spans)
    if not n: return []
    t = [spans[0][0]] + [spans[k][0] for k in range(1, n)] + [spans[-1][1]]
    def crosses(x):
        if _crossing(x, sentences, tol, quiet): return True
        if any(abs(x - q) < 1e-6 for q in quiet): return False   # quiet right on the cut: not a breath inside a line
        p, q = _gap_around(x, sentences, tol)          # a cut in a breath-length gap still cuts the line
        return p is not None and q is not None and q - p < TIGHT_PAUSE and x - p < 1.0 and q - x < 1.0
    edges = [0] + sorted(k for k in locks if 0 < k < n) + [n]
    blocks = [[x, y, []] for x, y in zip(edges, edges[1:])]
    merged = [blocks[0]]
    for blk in blocks[1:]:
        k = blk[0]; prev = merged[-1]
        if crosses(t[k]):
            if locks.get(k) == 'shot' and t[blk[1]] - t[prev[0]] <= max_s:
                prev[1] = blk[1]; prev[2].append(f'a line runs across the shot lock at {t[k]:.2f} s: blocks merged'); continue
            blk[2].append(f'a line runs across the {locks.get(k)} lock at {t[k]:.2f} s: check the seam')
        merged.append(blk)
    return [(i, j, notes) for i, j, notes in merged if any(product[i:j])]


def split_long_shots(spans, sentences, max_s=MAX_SEGMENT, tol=TOLERANCE, transitions=None):
    """Pre-split any single shot longer than max_s so a segment can end inside it (user rule, 2026-10-23).

    Real hard cuts are rare to miss, but when one continuous shot is longer than the ceiling it is cut at a pause
    between two sentences (fewest cuts, then the longest pauses); recut later trims that pause ("the extra breath")
    so the seam plays as a deliberate hard cut. A silent or wall-to-wall-speech shot with no usable pause is cut
    evenly and flagged.
    Returns (pieces, origin, cuts): pieces = [(start, end)], origin = index of the shot each piece came from,
    cuts = {piece boundary index: {'kind': 'in_shot_pause'|'in_shot_even', 'pause': [p, q] | None}}.
    """
    pieces, origin, cuts = [], [], {}
    for idx, (a, b) in enumerate(spans):
        if b - a <= max_s:
            pieces.append((a, b)); origin.append(idx); continue
        inside = sorted((x for x in sentences if x['end'] > a + tol and x['start'] < b - tol), key=lambda x: x['start'])
        points = []   # (cut time, pause start, pause end)
        for x, y in zip(inside, inside[1:]):
            p, q = x['end'], max(x['end'], y['start'])
            # the strongest picture change inside the pause (a dissolve or a camera move ending), else its middle:
            # 2026-11-20 film 2210 S058 was cut at 433.4, the middle of the pause 431.6-435.2, inside a continuous wide
            # shot right after a dissolve ending at 431.6; the original bottle flashed back at the seam
            inside_pause = [(sc, t) for t, sc in (transitions or []) if p <= t <= q]
            c = max(inside_pause)[1] if inside_pause else (p + q) / 2
            if a + 0.5 < c < b - 0.5 and not any(z['start'] + tol < c < z['end'] - tol for z in sentences): points.append((c, p, q))
        best = {a: (0, 0.0, [])}   # position -> (cuts, -pause total, chosen points)
        for c, p, q in sorted(points) + [(b, None, None)]:
            opts = [(n + (c != b), neg - ((q - p) if p is not None else 0.0), path + ([(c, p, q)] if c != b else []))
                    for prev, (n, neg, path) in best.items() if prev < c and c - prev <= max_s + 1e-9]
            if opts: best[c] = min(opts)
        if b in best:
            chosen = [(c, 'in_shot_pause', [round(p, 3), round(q, 3)]) for c, p, q in best[b][2]]
        else:
            n = int(-(-(b - a) // max_s)); chosen = [(a + (b - a) * k / n, 'in_shot_even', None) for k in range(1, n)]
        edges = [a] + [c for c, _, _ in chosen] + [b]
        for k, (x, y) in enumerate(zip(edges, edges[1:])):
            if k: cuts[len(pieces)] = {'kind': chosen[k - 1][1], 'pause': chosen[k - 1][2]}
            pieces.append((x, y)); origin.append(idx)
    return pieces, origin, cuts


def segment_run(spans, sentences, max_s=MAX_SEGMENT, beat_gap=BEAT_GAP, tol=TOLERANCE, min_s=MIN_SEGMENT, preferred=None, virtual=None, quiet=(), drop_beats=()):
    """Split one run of consecutive replace shots into segments.

    spans: [(start_s, end_s)] per shot, contiguous. sentences: [{'start','end',...}] absolute seconds.
    Returns (groups, notes): groups = [(i, j)] shot index ranges [i, j); notes = one dict per group.
    Rules, in order: never cut at a boundary a sentence crosses; cut at every beat (a pause ≥ beat_gap that
    contains a shot boundary, choosing the last boundary before the next sentence so a silent product shot stays
    with the line before it); merge beats shorter than min_s into a neighbour when the result fits; only then
    split anything still over max_s with the fewest extra cuts, preferring boundaries in `preferred` (semantic
    beat breaks), then the longest pause.
    """
    preferred = set(preferred or ()); virtual = set(virtual or ())   # virtual = in-shot cut points from split_long_shots
    n = len(spans)
    if n == 0: return [], []
    t = [spans[0][0]] + [spans[k][0] for k in range(1, n)] + [spans[-1][1]]   # t[k] = boundary before shot k
    quiet = set(quiet or ())
    def crosses(x): return _crossing(x, sentences, tol, quiet)
    def gap_len(k):
        p, q = _gap_around(t[k], sentences, tol)
        if p is None or q is None: return 0.0
        return max(0.0, q - p)
    admissible = [k for k in range(1, n) if not crosses(t[k])]
    # beat cuts: one per qualifying pause, the last admissible boundary before the next sentence starts
    beats = {}
    for k in admissible:
        if k in virtual: continue          # an in-shot point is used only when the ceiling forces it
        p, q = _gap_around(t[k], sentences, tol)
        if p is None or q is None or q - p < beat_gap or t[k] > q + tol: continue
        beats[(p, q)] = max(beats.get((p, q), 0), k)
    beats = {pq: k for pq, k in beats.items() if k not in set(drop_beats)}   # a pause Flash called mid-exchange is no boundary at all
    cuts = sorted(set(beats.values()))
    # merge too-short pieces through their weaker side
    def pieces(cs): b = [0] + cs + [n]; return list(zip(b[:-1], b[1:]))
    changed = True
    while changed and cuts:
        changed = False
        for idx, (i, j) in enumerate(pieces(cuts)):
            if t[j] - t[i] >= min_s: continue
            options = []
            if idx > 0: options.append((gap_len(cuts[idx - 1]), cuts[idx - 1], t[j] - t[pieces(cuts)[idx - 1][0]]))
            if idx < len(cuts): options.append((gap_len(cuts[idx]), cuts[idx], t[pieces(cuts)[idx + 1][1]] - t[i]))
            options = [o for o in options if o[2] <= max_s]
            if options:
                cuts.remove(min(options)[1]); changed = True; break
    # split anything still too long: fewest extra cuts, preferring the longest pauses, never crossing speech if avoidable
    final, notes = [], []
    for i, j in pieces(cuts):
        if t[j] - t[i] <= max_s or j - i == 1:
            final.append((i, j)); notes.append({}); continue
        inner = list(range(i + 1, j))
        # inside one pause, prefer its LAST hard cut, so a silent product shot stays with the line before it
        not_last = {k for k in inner if any(k2 > k and k2 in admissible and _gap_around(t[k2], sentences, tol) == _gap_around(t[k], sentences, tol)
                                            and _gap_around(t[k], sentences, tol)[1] is not None and t[k2] <= _gap_around(t[k], sentences, tol)[1] + tol for k2 in inner)}
        best = {i: (0, 0.0, [])}   # boundary -> (cost, -pause, path)
        for k in inner + [j]:
            cands = []
            for prev, (cost, neg, path) in best.items():
                if prev >= k or t[k] - t[prev] > max_s + 1e-6: continue
                step = 0 if k == j else (1 if k in admissible else 1000) + (0 if k in preferred else 0.25) + (0.5 if k in virtual else 0) + (0.01 if k in not_last else 0)
                cands.append((cost + step, neg - (gap_len(k) if k != j else 0.0), path + ([k] if k != j else [])))
            if cands: best[k] = min(cands)
        if j not in best:   # a single shot longer than max_s inside: fall back to shot-level pieces
            path = inner
        else:
            path = best[j][2]
        b = [i] + path + [j]
        for x, y in zip(b[:-1], b[1:]):
            note = {'forced_split': True, 'parent': (i, j)}
            if x != i and crosses(t[x]): note['warning'] = ('cut inside a sentence by the Gemini sentence times (they run early by up to ~0.5 s); '
                'check it with local word times (words.py): if the cut falls in a pause between two words, it is fine -- otherwise '
                'move it to a hard cut that does (plan/lock_overrides.json {"S###": "shot"}) and run plan again')
            final.append((x, y)); notes.append(note)
    for (i, j), note in zip(final, notes):
        note.update(start=t[i], end=t[j], duration=round(t[j] - t[i], 3),
                    sentences=[s for s in sentences if s['end'] > t[i] + tol and s['start'] < t[j] - tol])
        if i > 0: note['cut_in'] = {'pause_s': round(gap_len(i), 3), 'beat': i in beats.values(), 'semantic_break': i in preferred,
                                    'crosses_speech': crosses(t[i]), **({'in_shot': True} if i in virtual else {})}
    return final, notes


def plan_run(run, spans, sentences, max_s=MAX_SEGMENT, virtual=None, quiet=()):
    """segment_run, then one semantic pass over every cut it made at a beat and every candidate for a forced split
    (user rule: segments follow complete meaning). A beat Flash calls a split inside one exchange (question and
    answer, apology and response) is dropped when the merged piece still fits; good breaks are preferred."""
    kw = dict(max_s=max_s, virtual=virtual, quiet=quiet)
    t = [spans[0][0]] + [spans[k][0] for k in range(1, len(spans))] + [spans[-1][1]]
    groups, notes = segment_run(spans, sentences, **kw)
    def line_before(k):
        before = [n for n, s in enumerate(sentences) if s['end'] <= t[k] + TOLERANCE]
        return before[-1] if before else None
    beat_ks = {i: line_before(i) for (i, j), n in zip(groups, notes) if (n.get('cut_in') or {}).get('beat') and line_before(i) is not None}
    cands = forced_candidates(spans, sentences, groups, notes, quiet=quiet)
    asked = {**cands, **beat_ks}
    if not asked: return groups, notes
    verdict = dict(semantic_breaks(run, sentences, set(asked.values())))
    drop = {k for k, line in beat_ks.items() if line in verdict and not verdict[line].get('good')}
    for _ in range(2):   # dropping beats can create new forced splits: ask about their candidates too
        preferred = {k for k, line in asked.items() if verdict.get(line, {}).get('good')}
        groups, notes = segment_run(spans, sentences, preferred=preferred, drop_beats=drop, **kw)
        more = forced_candidates(spans, sentences, groups, notes, quiet=quiet)
        missing = {line for line in more.values() if line not in verdict}
        asked.update(more)
        if not missing: break
        verdict.update(semantic_breaks(run, sentences, missing))
    for (i, j), n in zip(groups, notes):
        ci = n.get('cut_in'); line = asked.get(i)
        if ci and line is not None and line in verdict: ci['semantic_reason'] = verdict[line]['reason']
    return groups, notes


def word_check(sentences, rewrites):
    """Did the candidate say the new words and drop the old ones?  rewrites: [{'original','rewrite'}].
    Returns {'missing': new words not heard, 'leftover': replaced words still heard, 'heard': transcript}."""
    import re
    tok = lambda t: re.findall(r"[a-z]+", t.lower())
    heard = ' '.join(s['text'] for s in sentences); said = set(tok(heard)); said_join = ''.join(tok(heard))
    missing, leftover = set(), set()
    for r in rewrites:
        old, new = set(tok(r['original'])), set(tok(r['rewrite']))
        for w in new - old:
            if w not in said and w not in said_join: missing.add(w)
        for w in old - new:
            if w in said: leftover.add(w)
    return {'missing': sorted(missing), 'leftover': sorted(leftover), 'heard': heard}


def main():
    p = argparse.ArgumentParser(description=__doc__); s = p.add_subparsers(dest='command', required=True)
    q = s.add_parser('transcribe'); q.add_argument('run'); q.add_argument('start', type=float); q.add_argument('end', type=float); q.add_argument('--master')
    a = p.parse_args()
    run = Path(a.run); state = json.loads((run/'state.json').read_text())
    master = a.master or state['stages']['prepare']['working_master']['path']
    print(json.dumps(transcribe(run, master, a.start, a.end), ensure_ascii=False, indent=1))


if __name__ == '__main__': main()
