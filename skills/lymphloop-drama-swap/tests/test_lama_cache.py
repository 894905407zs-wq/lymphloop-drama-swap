"""LaMa result cache (user 2026-11-21: "先做缓存"): a window inpainted before comes back exactly, without the model."""
import sys, tempfile, unittest
from pathlib import Path
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'scripts'))
import caption_lama as cl  # noqa: E402


class FakeSession:
    def __init__(self): self.calls = 0
    def get_inputs(self):
        return [mock.Mock(name='image'), mock.Mock(name='mask')]
    def run(self, _, feeds):
        self.calls += 1
        img = list(feeds.values())[0]
        return [img[:, ::-1] * 255.0 * 0.5 + 40.0]          # deterministic stand-in for LaMa (0-255 output)


class LamaCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dirs = mock.patch.object(cl, 'CACHE_DIR', Path(self.tmp.name)/'lama-cache')
        self.dirs.start(); cl._pruned = True
        self.fake = FakeSession()
        self.sess = mock.patch.object(cl, 'session', return_value=self.fake); self.sess.start()
        rng = np.random.default_rng(5)
        self.frame = rng.integers(0, 255, (1280, 720, 3), dtype=np.uint8)
        self.mask = cl.line_mask(self.frame.shape, [[60, 1040, 600, 60]])

    def tearDown(self):
        self.sess.stop(); self.dirs.stop(); self.tmp.cleanup()

    def test_second_call_is_a_hit_and_identical(self):
        a = cl.inpaint(self.frame.copy(), self.mask)
        b = cl.inpaint(self.frame.copy(), self.mask)
        self.assertEqual(self.fake.calls, 1)
        self.assertTrue(np.array_equal(a, b))
        self.assertFalse(np.array_equal(a, self.frame))
        self.assertEqual(len(list(cl.CACHE_DIR.glob('*/*.png'))), 1)

    def test_other_pixels_or_mask_miss(self):
        cl.inpaint(self.frame.copy(), self.mask)
        f2 = self.frame.copy(); f2[1050, 100] ^= 1                        # one pixel inside the window
        cl.inpaint(f2, self.mask)
        cl.inpaint(self.frame.copy(), cl.line_mask(self.frame.shape, [[60, 1041, 600, 60]]))
        self.assertEqual(self.fake.calls, 3)

    def test_cache_off(self):
        with mock.patch.object(cl, '_cache_on', False):
            cl.inpaint(self.frame.copy(), self.mask); cl.inpaint(self.frame.copy(), self.mask)
        self.assertEqual(self.fake.calls, 2)
        self.assertFalse(cl.CACHE_DIR.exists())

    def test_prune_keeps_the_newest(self):
        import os, time
        for i in range(5):
            p = cl.CACHE_DIR/'aa'/f'{i:064d}.png'; p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'x' * 1000); os.utime(p, (time.time() + i, time.time() + i))
        cl.prune_cache(limit_gb=3000 / 1024 ** 3)                     # limit 3000 bytes: keep at most 2400
        left = sorted(p.name for p in cl.CACHE_DIR.glob('*/*.png'))
        self.assertEqual(left, [f'{3:064d}.png', f'{4:064d}.png'])


if __name__ == '__main__':
    unittest.main()
