"""Content-keyed recut / brand / final-screen (user 2026-11-22: "顺剪有个问题，只要中间有一个镜头穿帮，回剪之后所有的镜头流程
都要从头来过" -> "改，按内容缓存"). A goof redo that changes one segment's length must not re-encode, re-stamp or
re-screen the rest of the film, which is the same picture and sound at a new place."""
import json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'scripts'))
import media_pipeline as m  # noqa: E402
import watermark_brand as wb  # noqa: E402
import swap  # noqa: E402


def movie(path, duration, color, tone=220, fps=25):
    m.ff(['-y', '-f', 'lavfi', '-i', f'color=c={color}:s=128x192:r={fps}:d={duration}',
          '-f', 'lavfi', '-i', f'sine=frequency={tone}:sample_rate=48000:duration={duration}',
          '-map', '0:v', '-map', '1:a', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', path])


def frame(path, n):
    import subprocess
    raw = subprocess.run([m.binary('ffmpeg'), '-v', 'error', '-i', str(path), '-vf', f"select='eq(n\\,{n})'", '-fps_mode', 'passthrough',
                          '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(192, 128, 3)


class ContentCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='lymphloop-content-cache-'); cls.root = Path(cls.tmp.name)
        movie(cls.root/'source.mp4', 8, 'gray')
        movie(cls.root/'g1.mp4', 2.0, 'blue', 660)
        movie(cls.root/'g1_long.mp4', 3.0, 'purple', 660)
        movie(cls.root/'g2.mp4', 1.0, 'yellow', 440)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def render(self, g1, clip_out, name):
        data = {'schema_version': 1, 'source': 'source.mp4', 'fps': '25', 'timing': 'ripple', 'max_speed': 1.0,
                'picture_replacements': [
                    {'id': 'G01', 'kind': 'Dialogue', 'source_in': 25, 'source_out': 75, 'path': g1, 'clip_in': 0, 'clip_out': clip_out, 'evidence': 't'},
                    {'id': 'G02', 'kind': 'Dialogue', 'source_in': 125, 'source_out': 150, 'path': 'g2.mp4', 'clip_in': 0, 'clip_out': 1.0, 'evidence': 't'}],
                'audio_replacements': []}
        mf = self.root/(name + '.json'); m.save(mf, data)
        out = self.root/(name + '.mp4')
        m.render(mf, out, self.root/'render')
        return out, json.loads(Path(str(out) + '.timeline.json').read_text())

    def stamps_on_last_second(self, tl):
        """A watermark on the last 10 frames of the film (inside the final original atom)."""
        n = tl['output_frames']
        return {'stamps': {str(f): [{'box': [10, 10, 100, 24], 'orient': 'h'}] for f in range(n - 10, n)}}

    def test_brand_restamps_only_changed_atoms_and_reuses_the_rest(self):
        out_a, tl_a = self.render('g1.mp4', 2.0, 'a')
        self.assertTrue(wb.atoms_usable(tl_a))
        cache = self.root/'brand-atoms'
        rec_a = wb.stamp_atoms(out_a, tl_a, self.stamps_on_last_second(tl_a), self.root/'a.branded.mp4', self.root/'a.brand.json', cache)
        self.assertEqual(rec_a['by_atom']['stamped_now'], 1)
        self.assertTrue(rec_a['verification']['ok'])
        n = tl_a['output_frames']
        self.assertGreater(frame(self.root/'a.branded.mp4', n - 5)[10:34, 10:110].max(), 240)        # white letters
        self.assertTrue(np.allclose(frame(self.root/'a.branded.mp4', 5).astype(int), frame(out_a, 5).astype(int), atol=0))  # untouched atom: same bytes

        out_b, tl_b = self.render('g1_long.mp4', 3.0, 'b')          # goof redo: G01 one second longer, the rest moves
        self.assertEqual(tl_b['output_frames'], n + 25)
        rec_b = wb.stamp_atoms(out_b, tl_b, self.stamps_on_last_second(tl_b), self.root/'b.branded.mp4', self.root/'b.brand.json', cache)
        self.assertEqual((rec_b['by_atom']['stamped_now'], rec_b['by_atom']['stamped_reused']), (0, 1))
        self.assertTrue(rec_b['verification']['ok'])
        self.assertEqual(m.probe(str(self.root/'b.branded.mp4'), include_frames=True)['frame_count'], n + 25)

    def test_final_screen_baseline_only_new_content(self):
        out_a, tl_a = self.render('g1.mp4', 2.0, 'c')
        out_b, tl_b = self.render('g1_long.mp4', 3.0, 'd')
        prev_dir = self.root/'final_prev'; (prev_dir/'shots').mkdir(parents=True, exist_ok=True)
        ca = swap.master_content({'stages': {}}, out_a, 'shaA')
        m.save(prev_dir/'content.json', ca)
        m.save(prev_dir/'shots/shots.json', {'shots': []})
        state = {'stages': {'final_screen': {'dir': str(prev_dir), 'master_sha256': 'shaA', 'replace_ids': [], 'unsure_ids': [], 'error_ids': []},
                            'flashback_review': {'master_sha256': 'shaA', 'verdict': 'clean', 'hits': []}}}
        cb = swap.master_content(state, out_b, 'shaB')
        base = swap.screened_baseline(None, state, cb, 'shaB')
        self.assertEqual(base['new_pictures'], ['G01'])
        self.assertTrue(base['flashback_seen'])
        g1 = next(x for x in tl_b['segments'] if x.get('picture_id') == 'G01')
        g2 = next(x for x in tl_b['segments'] if x.get('picture_id') == 'G02')
        self.assertEqual(base['changed'], [(max(0, g1['out_in'] - 30), g1['out_out'] + 30)])
        narrowed = swap.intersect_ranges([(g1['out_in'] - 30, g1['out_out'] + 30), (g2['out_in'] - 30, g2['out_out'] + 30)], base['changed'])
        self.assertTrue(all(b <= g2['out_in'] or a >= g2['out_out'] for a, b in narrowed))   # G02 moved, not screened again

    def test_open_finding_on_unchanged_content_is_screened_again(self):
        out_a, tl_a = self.render('g1.mp4', 2.0, 'e')
        out_b, _ = self.render('g1_long.mp4', 3.0, 'f')
        g2 = next(x for x in tl_a['segments'] if x.get('picture_id') == 'G02')
        prev_dir = self.root/'final_prev2'; (prev_dir/'shots').mkdir(parents=True, exist_ok=True)
        m.save(prev_dir/'content.json', swap.master_content({'stages': {}}, out_a, 'shaA'))
        m.save(prev_dir/'shots/shots.json', {'shots': [{'id': 'S009', 'in_frame': g2['out_in'], 'out_frame': g2['out_out']}]})
        state = {'stages': {'final_screen': {'dir': str(prev_dir), 'master_sha256': 'shaA', 'replace_ids': ['S009'], 'unsure_ids': [], 'error_ids': []}}}
        base = swap.screened_baseline(None, state, swap.master_content(state, out_b, 'shaB'), 'shaB')
        self.assertEqual(sorted(base['new_pictures']), ['G01', 'G02'])     # G02 is unchanged but was flagged last time
        self.assertFalse(base['flashback_seen'])                              # no flashback review of the old master: all seams again

    def test_no_baseline_without_atom_keys(self):
        out_a, tl_a = self.render('g1.mp4', 2.0, 'g')
        for x in tl_a['segments']: x.pop('atom_key')
        Path(str(out_a) + '.timeline.json').write_text(json.dumps(tl_a))
        self.assertIsNone(swap.master_content({'stages': {}}, out_a, 'sha'))
        self.assertFalse(wb.atoms_usable(tl_a))


if __name__ == '__main__':
    unittest.main()
