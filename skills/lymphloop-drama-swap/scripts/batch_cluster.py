#!/usr/bin/env python3
"""Fast visual-fingerprint clustering for a batch of videos using reuse.py 18x32 prints.

Groups videos that share the same main footage (e.g., different hook/ending variants)
so each cluster only pays/generates once for its representative film, and every variant
uses `swap.py reuse --from <rep_run>` at 0 generation cost.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as media  # noqa: E402
import reuse  # noqa: E402


def probe_and_print(video: Path, cache_dir: Path):
    info = media.probe(str(video))
    arr = reuse.prints(video, cache_dir)
    # Sample 12 anchor frames across the middle 20%..85% of the film for fast pairwise overlap test
    n = len(arr)
    idxs = [int(n * q) for q in np.linspace(0.20, 0.85, 12)] if n >= 20 else list(range(n))
    return {
        'id': video.stem,
        'path': str(video),
        'size_mb': round(video.stat().st_size / (1024 * 1024), 1),
        'duration_s': round(float(info.get('video_duration') or 0), 2),
        'fps': str(info.get('fps')),
        'width': int((info.get('video') or {}).get('width') or 0),
        'height': int((info.get('video') or {}).get('height') or 0),
        'frames': n,
        'anchors': idxs,
        'prints': arr,
    }


def shared_fraction(a: dict, b: dict) -> tuple[float, int | None]:
    """Estimate fraction of video B's middle anchors that match video A at a consistent frame offset."""
    pa, pb = a['prints'], b['prints']
    if len(pa) < 30 or len(pb) < 30:
        return 0.0, None
    # Compare at 1-in-5 frame stride on A for ultra-fast search (~2ms per pair)
    stride = 5
    pa_s = pa[::stride].astype(np.float32)
    pa_s -= pa_s.mean(axis=1, keepdims=True)
    votes = {}
    for bi in b['anchors']:
        v = pb[bi].astype(np.float32)
        v -= v.mean()
        d = np.abs(pa_s - v).mean(axis=1)
        j = int(np.argmin(d))
        if d[j] < 3.0:
            ai = j * stride
            off_bucket = round((bi - ai) / 10) * 10
            votes[off_bucket] = votes.get(off_bucket, 0) + 1
    if not votes:
        return 0.0, None
    best_bucket, count = max(votes.items(), key=lambda x: x[1])
    frac = count / max(1, len(b['anchors']))
    if frac < 0.35:
        return frac, None
    # Verify exact offset with reuse.locate on a 5-second slice in the middle of B
    mid = len(pb) // 2
    span = min(150, len(pb) // 4)
    res = reuse.locate(pb, pa, mid, mid + span, anchors=5)
    if res and res.get('ok'):
        return max(frac, 0.5), int(res['offset'])
    return frac, None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--dir', required=True, help='Folder with source videos')
    ap.add_argument('--donors', nargs='*', default=[], help='Already completed donor RUN directories')
    ap.add_argument('--cache', required=True, help='Cache directory for prints')
    ap.add_argument('--out', required=True, help='Output cluster manifest JSON')
    ap.add_argument('--workers', type=int, default=8)
    a = ap.parse_args()

    src_dir = Path(a.dir).resolve()
    cache_dir = Path(a.cache).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    videos = sorted(p for p in src_dir.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4', '.mov', '.mkv'))

    print(f'Fingerprinting {len(videos)} videos with {a.workers} workers...', flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.workers) as ex:
        items = list(ex.map(lambda v: probe_and_print(v, cache_dir), videos))

    # Load any existing completed donor masters too
    donor_items = []
    for d in a.donors:
        dp = Path(d).resolve()
        st = dp / 'state.json'
        if st.is_file():
            sdata = json.loads(st.read_text(encoding='utf-8'))
            vpath = Path(sdata['source'])
            if vpath.is_file():
                di = probe_and_print(vpath, cache_dir)
                di['donor_run'] = str(dp)
                donor_items.append(di)

    # Sort longest duration first so the most complete cut tends to become cluster representative
    order = sorted(range(len(items)), key=lambda i: (-items[i]['duration_s'], items[i]['id']))
    clusters = []
    assigned = {}

    # First check against existing completed donor RUNs
    for idx in order:
        it = items[idx]
        for di in donor_items:
            frac, off = shared_fraction(di, it)
            if off is not None:
                assigned[it['id']] = {'cluster': f"donor:{di['id']}", 'donor_run': di['donor_run'], 'offset': off, 'frac': round(frac, 2)}
                break

    for idx in order:
        it = items[idx]
        if it['id'] in assigned:
            continue
        placed = False
        for cl in clusters:
            rep = cl['rep_item']
            frac, off = shared_fraction(rep, it)
            if off is not None:
                cl['variants'].append({k: it[k] for k in ('id', 'path', 'duration_s', 'fps', 'width', 'height', 'size_mb')} | {'offset': off, 'match_frac': round(frac, 2)})
                assigned[it['id']] = cl['id']
                placed = True
                break
        if not placed:
            cid = f'C{len(clusters) + 1:02d}'
            clusters.append({
                'id': cid,
                'rep_item': it,
                'representative': {k: it[k] for k in ('id', 'path', 'duration_s', 'fps', 'width', 'height', 'size_mb')},
                'variants': [],
            })
            assigned[it['id']] = cid

    donor_matches = [
        {k: it[k] for k in ('id', 'path', 'duration_s', 'fps', 'width', 'height', 'size_mb')} | assigned[it['id']]
        for it in items if isinstance(assigned.get(it['id']), dict)
    ]
    out_data = {
        'total_videos': len(items),
        'matched_existing_donors': len(donor_matches),
        'representative_count': len(clusters),
        'variant_count': sum(len(c['variants']) for c in clusters),
        'donor_matches': donor_matches,
        'clusters': [
            {'id': c['id'], 'representative': c['representative'], 'variants': c['variants'], 'size': 1 + len(c['variants'])}
            for c in clusters
        ],
    }
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out_data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({
        'total_videos': out_data['total_videos'],
        'matched_existing_donors': out_data['matched_existing_donors'],
        'representative_count': out_data['representative_count'],
        'variant_count': out_data['variant_count'],
        'top_clusters': [(c['id'], c['representative']['id'], c['size'], c['representative']['duration_s']) for c in out_data['clusters'][:25]],
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
