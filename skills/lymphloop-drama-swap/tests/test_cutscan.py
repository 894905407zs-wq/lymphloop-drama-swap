"""Frame-exact cuts, edge snapping and the flashback frame list (user 2026-11-21: "杜绝滴剂瓶闪回")."""
import json, subprocess, sys, tempfile, unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'scripts'))
import cutscan  # noqa: E402
import media_pipeline as media  # noqa: E402


def seg(i, a, b, **kw):
    return {'id': f'G{i:02d}', 'in_frame': a, 'out_frame': b, 'duration_s': (b - a) / 30, 'speech': [{'start': 1.0, 'end': 2.0}],
            'shot_times': [['S1', 0.0, (b - a) / 30]], **kw}


class Cuts(unittest.TestCase):
    def test_cuts_from_diffs(self):
        d = np.full(100, 2.0); d[[9, 40, 47]] = 60.0; d[70] = 15.0          # 15 is under ABS
        self.assertEqual(cutscan.cuts_from_diffs(d), [10, 41, 48])

    def test_cuts_on_a_real_encode(self):
        rng = np.random.default_rng(3)
        with tempfile.TemporaryDirectory() as tmp:
            v = Path(tmp)/'v.mp4'
            enc = subprocess.Popen([media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', '72x128', '-r', '30',
                                    '-i', '-', '-c:v', 'libx264', '-crf', '10', '-pix_fmt', 'yuv420p', str(v)], stdin=subprocess.PIPE)
            levels = [40] * 30 + [200] * 7 + [90] * 40                         # a 7-frame flash between two shots
            for lv in levels:
                enc.stdin.write(np.clip(lv + rng.normal(0, 2, (128, 72)), 0, 255).astype(np.uint8).tobytes())
            enc.stdin.close(); enc.wait()
            r = cutscan.cuts(v, cache=Path(tmp)/'c.json')
            self.assertEqual(r['cuts'], [30, 37])
            self.assertEqual(cutscan.cuts(v, cache=Path(tmp)/'c.json')['cuts'], [30, 37])     # cached


class Snap(unittest.TestCase):
    def test_short_piece_before_and_after_is_pulled_in(self):
        s = [seg(1, 100, 300)]
        notes = cutscan.snap_edges(s, [92, 100, 300, 309, 500], 30, 1000)
        self.assertEqual((s[0]['in_frame'], s[0]['out_frame']), (92, 309))
        self.assertEqual(len(notes[0]['notes']), 2)
        self.assertAlmostEqual(s[0]['speech'][0]['start'], 1.0 + 8 / 30, places=3)   # speech keeps its place in the film
        self.assertEqual(s[0]['shot_times'][0][1], 0.0)

    def test_edge_off_a_cut_moves_outward_never_in(self):
        s = [seg(1, 105, 300)]
        cutscan.snap_edges(s, [100, 300, 600], 30, 1000)
        self.assertEqual(s[0]['in_frame'], 100)
        s = [seg(1, 105, 300)]
        notes = cutscan.snap_edges(s, [111, 300, 600], 30, 1000)          # only a cut inside: stays, warns
        self.assertEqual(s[0]['in_frame'], 105); self.assertIn('check the seam', notes[0]['notes'][0])

    def test_long_piece_stays_and_neighbours_are_respected(self):
        s = [seg(1, 100, 300)]
        self.assertEqual(cutscan.snap_edges(s, [50, 100, 300, 360], 30, 1000), [])   # pieces of 50 and 60 frames are real shots
        s = [seg(1, 100, 300), seg(2, 300, 500)]
        cutscan.snap_edges(s, [100, 296, 300, 500], 30, 1000)
        self.assertEqual(s[0]['out_frame'], 300); self.assertEqual(s[1]['in_frame'], 300)

    def test_ceiling_and_in_shot_cuts(self):
        s = [seg(1, 10, 450)]                                               # 14.67 s
        notes = cutscan.snap_edges(s, [2, 10, 450], 30, 1000, max_s=14.9)
        self.assertEqual(s[0]['in_frame'], 10)
        self.assertIn('check', notes[0]['notes'][0])
        s = [seg(1, 105, 300, in_shot_cut_in={'kind': 'pause'})]
        cutscan.snap_edges(s, [100, 300], 30, 1000)
        self.assertEqual(s[0]['in_frame'], 105)


class Spots(unittest.TestCase):
    def test_seams_joins_and_short_shots(self):
        tl = [{'out_in': 0, 'out_out': 300, 'picture_id': None}, {'out_in': 300, 'out_out': 600, 'picture_id': 'G01'},
              {'out_in': 600, 'out_out': 1000, 'picture_id': None}]
        spots = cutscan.flashback_spots(tl, [300, 450, 600, 800, 806], 1000, 30, joins=[350])
        self.assertEqual([s['kinds'] for s in spots], [['seam original|G01'], ['splice join'], ['seam G01|original'], ['short shot 6f']])
        self.assertTrue({285, 288, 299, 300, 312} <= set(spots[0]['frames']) and 286 not in spots[0]['frames'])   # every 3rd + the join frames
        self.assertTrue({335, 349, 350, 362} <= set(spots[1]['frames']))
        self.assertEqual(spots[3]['frames'], list(range(798, 808)))                               # short shot: every frame +-2
        merged = cutscan.flashback_spots(tl, [300, 600], 1000, 30, joins=[320])
        self.assertEqual(merged[0]['kinds'], ['seam original|G01', 'splice join'])                # overlapping windows merge
        self.assertFalse(any(449 <= f <= 451 for s in spots for f in s['frames']))              # a cut inside G01 is review's job
        spots = cutscan.flashback_spots(tl, [300, 450, 600, 800, 806], 1000, 30, ranges=[(280, 320)])
        self.assertTrue(all(280 <= f < 320 for s in spots for f in s['frames']))

    def test_sheets_are_labelled_and_sized_for_the_agent(self):
        rng = np.random.default_rng(4)
        with tempfile.TemporaryDirectory() as tmp:
            v = Path(tmp)/'v.mp4'
            enc = subprocess.Popen([media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', '720x1280', '-r', '30',
                                    '-i', '-', '-c:v', 'libx264', '-crf', '20', '-pix_fmt', 'yuv420p', str(v)], stdin=subprocess.PIPE)
            for _ in range(60): enc.stdin.write(rng.integers(0, 255, (1280, 720), dtype=np.uint8).tobytes())
            enc.stdin.close(); enc.wait()
            out = cutscan.spot_sheets(v, [{'kinds': ['seam a|b'], 'at': [30], 'frames': list(range(10, 50, 2))}], Path(tmp)/'s', 30.0)
            self.assertEqual([len(x['frames']) for x in out], [15, 5])
            from PIL import Image
            self.assertEqual(Image.open(out[0]['path']).size, (1500, 3 * 534 + 36))


class SpliceJoins(unittest.TestCase):
    def test_recipe_joins_are_found_recursively(self):
        import swap
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp); jobs = run/'generation/jobs'; jobs.mkdir(parents=True)
            res = run/'generation/results'
            def ledger(job, pieces):
                (jobs/f'{job}.json').write_text(json.dumps({'provider': 'local_splice', 'recipe': {'pictures': [
                    {'path': str(res/f'{src}-c1.mp4'), 'frames_inclusive': fr} for src, fr in pieces]}}))
            ledger('G01-a2', [('G01-a1', [0, 26]), ('G01-retrieve', [0, 41]), ('G01-a1', [60, 99])])     # joins at 27, 69
            ledger('G01-a3', [('G01-a2', [0, 40]), ('G01-a2', [50, 109])])                            # 27 inside, join at 41, 69 -> 60
            (jobs/'G01-a1.json').write_text(json.dumps({'provider': 'apimart'}))
            self.assertEqual(swap.splice_joins(run, res/'G01-a2-c1.mp4'), [27, 69])
            self.assertEqual(swap.splice_joins(run, res/'G01-a3-c1.captioned.mp4'), [27, 41, 60])
            self.assertEqual(swap.splice_joins(run, res/'G01-a1-c1.mp4'), [])


if __name__ == '__main__':
    unittest.main()
