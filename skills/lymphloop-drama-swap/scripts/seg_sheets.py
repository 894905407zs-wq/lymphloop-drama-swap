#!/usr/bin/env python3
"""A time-stamped contact sheet of every planned segment, cut from the original source at 2.5-4 frames a second (times
are segment seconds), with the shot windows, the lines and the rewrites printed next to it. Look at it before writing
the segment's prompt: the shot sheets show one shot each, this shows the segment as the model will get it.

  seg_sheets.py RUN [G01,G02]              -> RUN/frames/G##.jpg
  seg_sheets.py RUN [G01,G02] --generated  -> RUN/frames/G##-out.jpg, the selected (or a1-c1) clip in the same layout
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, json, math, subprocess
from fractions import Fraction
from pathlib import Path
import media_pipeline as media


def sheet(src, out, start_s, dur_s):
    rate = 4 if dur_s <= 8 else (3 if dur_s <= 11 else 2.5)
    rows = math.ceil(math.ceil(dur_s * rate) / 8)
    vf = f"fps={rate},scale=225:-2,drawtext=text='%{{pts\\:hms}}':x=4:y=4:fontsize=15:fontcolor=yellow:box=1:boxcolor=black,tile=8x{rows}"
    subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-y', '-ss', f'{start_s:.4f}', '-t', f'{dur_s:.4f}', '-i', str(src), '-vf', vf, '-frames:v', '1', str(out)],
                   stderr=subprocess.DEVNULL)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('run'); p.add_argument('ids', nargs='?'); p.add_argument('--generated', action='store_true')
    a = p.parse_args(argv)
    run = Path(a.run); only = set(a.ids.split(',')) if a.ids else None
    state = json.loads((run/'state.json').read_text()); plan = json.loads((run/'plan/segments.json').read_text())
    fps = float(Fraction(str(plan['fps']))); out = run/'frames'; out.mkdir(exist_ok=True)
    sel = json.loads((run/'plan/selections.json').read_text()) if (run/'plan/selections.json').is_file() else {}
    for s in plan['segments']:
        if only and s['id'] not in only: continue
        ss, dur = s['in_frame'] / fps, (s['out_frame'] - s['in_frame']) / fps
        if a.generated:
            clip = Path(sel[s['id']]['media']['path']) if (sel.get(s['id']) or {}).get('media') else run/'generation/results'/f"{s['id']}-a1-c1.mp4"
            if clip.is_file(): print(s['id'], sheet(clip, out/f"{s['id']}-out.jpg", 0.0, float(media.probe(str(clip))['video_duration'])), clip.name)
            continue
        print(f"\n## {s['id']} shots {s['shot_ids']} source {ss:.2f}-{ss + dur:.2f} s ({dur:.2f} s) product shots {s.get('product_shots')}")
        print('  sheet:', sheet(state['source'], out/f"{s['id']}.jpg", ss, dur))
        print('  shot windows:', [(x, round(b, 2), round(c, 2)) for x, b, c in s.get('shot_times', [])])
        for x in s.get('speech', []): print(f"  {x['start']:.2f}-{x['end']:.2f} [{x.get('kind', '')}] {x.get('speaker')}: {x['text']}")
        print('  rewrites:', [(r['original'], r['rewrite']) for r in s['rewrites']['dialogue'] + s['rewrites']['vo']])


if __name__ == '__main__':
    main()
