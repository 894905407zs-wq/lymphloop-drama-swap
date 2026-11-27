"""Stored-template watermark scan (user 2026-11-21: "固定位置+有水印才盖")."""
import subprocess, sys, tempfile, unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'scripts'))
import watermark_template as wt  # noqa: E402
import media_pipeline as media  # noqa: E402


class Runs(unittest.TestCase):
    def test_hi_seeds_grow_over_lo_and_pad(self):
        col = np.zeros(100, np.float32)
        col[40:50] = 0.9; col[37:40] = 0.22; col[50:52] = 0.21; col[60] = 0.25    # a lone LO frame is not a seed
        on = wt.runs(col)
        idx = np.flatnonzero(on)
        self.assertEqual((idx[0], idx[-1]), (37 - wt.PAD, 51 + wt.PAD))
        self.assertFalse(on[60])

    def test_short_gap_is_bridged(self):
        col = np.zeros(100, np.float32); col[10:20] = 0.8; col[25:35] = 0.8
        on = wt.runs(col, pad=0)
        self.assertTrue(on[10:35].all())
        col2 = np.zeros(100, np.float32); col2[10:20] = 0.8; col2[40:50] = 0.8
        self.assertFalse(wt.runs(col2, pad=0)[30])


class StoredTemplate(unittest.TestCase):
    def test_lymphoria_template_is_shipped(self):
        meta = wt.available('LYMPHORIA')
        self.assertIsNotNone(meta)
        m = wt.load(meta)
        self.assertEqual(len(m['places']), 6)
        self.assertEqual(sorted(p['orient'] for p in m['places']), ['h', 'h', 'v_down', 'v_down', 'v_down', 'v_down'])
        for p in m['places']:
            self.assertEqual(p['template'].shape, (p['box'][3] + 2, p['box'][2] + 2))
        self.assertIsNone(wt.available('NOSUCHBRAND'))

    def test_marked_frames_found_only_where_painted(self):
        """A synthetic film: textured background, the mark painted at one place on frames 20-39 (2 px off the stored
        box). The scan finds those frames (plus PAD) at that place only, at the shifted box; left() finds them unless stamped."""
        m = wt.load(wt.available('LYMPHORIA'))
        p = m['places'][0]; x, y, w, h = p['box']; dx, dy = 2, -1
        rng = np.random.default_rng(1)
        import cv2
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp)/'v.mp4'
            enc = subprocess.Popen([media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', '720x1280', '-r', '30',
                                    '-i', '-', '-c:v', 'libx264', '-crf', '12', '-pix_fmt', 'yuv420p', str(video)], stdin=subprocess.PIPE)
            for f in range(60):
                bg = cv2.GaussianBlur(rng.normal(120, 40, (1280, 720)).astype(np.float32), (0, 0), 6)
                if 20 <= f < 40:
                    t = p['template']
                    bg[y - 1 + dy:y - 1 + dy + t.shape[0], x - 1 + dx:x - 1 + dx + t.shape[1]] += 2.0 * t
                enc.stdin.write(np.clip(bg, 0, 255).astype(np.uint8).tobytes())
            enc.stdin.close(); enc.wait()
            r = wt.scan(video, 'LYMPHORIA', require=False)
            self.assertIsNotNone(r)
            self.assertEqual(r['scanned_frames'], 60)
            frames = sorted({d['frame'] for d in r['detections'] if d['bbox'][:2] == [x + dx, y + dy]})
            self.assertEqual((frames[0], frames[-1]), (20 - wt.PAD, 39 + wt.PAD))
            self.assertEqual({d['bbox'][0] for d in r['detections']}, {x + dx})     # no other place fired
            self.assertTrue(all(d['orient'] == p['orient'] and d['classification'] == 'floating_overlay' for d in r['detections']))
            self.assertEqual(r['positions'][0]['offset_px'], [dx, dy])
            self.assertIsNotNone(wt.scan(video, 'LYMPHORIA'))       # 20 marked frames >= MIN_FRAMES: a whole film applies too
            stamped = {0: set(range(20 - wt.PAD, 40 + wt.PAD))}
            self.assertEqual(wt.left(video, 'LYMPHORIA', stamped), [])
            self.assertEqual(sorted({d['frame'] for d in wt.left(video, 'LYMPHORIA', {})}), list(range(20, 40)))

    def test_film_without_the_mark_falls_back(self):
        rng = np.random.default_rng(2)
        import cv2
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp)/'v.mp4'
            enc = subprocess.Popen([media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray', '-s', '720x1280', '-r', '30',
                                    '-i', '-', '-c:v', 'libx264', '-crf', '12', '-pix_fmt', 'yuv420p', str(video)], stdin=subprocess.PIPE)
            for f in range(40):
                enc.stdin.write(np.clip(cv2.GaussianBlur(rng.normal(120, 40, (1280, 720)).astype(np.float32), (0, 0), 6), 0, 255).astype(np.uint8).tobytes())
            enc.stdin.close(); enc.wait()
            self.assertIsNone(wt.scan(video, 'LYMPHORIA'))            # no place reaches HI: OCR scan instead
            r = wt.scan(video, 'LYMPHORIA', require=False)            # a reference clip: applies, finds nothing
            self.assertEqual(r['detections'], [])


if __name__ == '__main__':
    unittest.main()
