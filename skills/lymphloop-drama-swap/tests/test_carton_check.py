"""Measured carton check (user 2026-11-24: "盒装产品比例变形的问题还是被放过了"): numbers, not a feeling, decide whether a
carton's proportions are a goof, and select refuses a flagged clip until the agent has looked."""
import argparse, json, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'scripts'))
import carton_check as cc  # noqa: E402
import media_pipeline as media  # noqa: E402

FONT = '/System/Library/Fonts/Supplemental/Arial Bold.ttf'


def carton_movie(path, box_w, box_h, word=True, seconds=2):
    """A still teal box (BGR teal) on grey, LYMPHATIC printed across 0.81 of its width when `word`."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.new('RGB', (720, 1280), (200, 200, 200)); d = ImageDraw.Draw(im)
    x0, y0 = (720 - box_w) // 2, (1280 - box_h) // 2
    d.rectangle((x0, y0, x0 + box_w, y0 + box_h), fill=(30, 160, 170))
    if word:
        size = 10
        while True:
            f = ImageFont.truetype(FONT, size + 1)
            if d.textlength('LYMPHATIC', font=f) > 0.81 * box_w: break
            size += 1
        f = ImageFont.truetype(FONT, size)
        d.text((x0 + 0.095 * box_w, y0 + 0.3 * box_h), 'LYMPHATIC', font=f, fill=(255, 255, 255))
    png = Path(path).with_suffix('.png'); im.save(png)
    media.ff(['-y', '-loop', '1', '-i', str(png), '-t', str(seconds), '-r', '24', '-pix_fmt', 'yuv420p', '-c:v', 'libx264', str(path)])


class Judge(unittest.TestCase):
    def blob(self, w, h, area=0.08): return {'box': [100, 100, w, h], 'outline': round(max(w, h) / min(w, h), 2), 'area': area}

    def test_front_ratio_comes_from_the_word(self):
        # word 81 px -> front 100 px wide: 130 tall is the real 1.3, 190 tall is stretched
        self.assertEqual(cc.judge(self.blob(100, 130), [[110, 140, 81, 20]], False), (None, 1.3))
        self.assertEqual(cc.judge(self.blob(100, 190), [[110, 140, 81, 20]], False)[0], 'front_stretched')

    def test_without_the_front(self):
        self.assertEqual(cc.judge(self.blob(60, 150), [], False)[0], 'too_slim')            # 2.5: beyond any real face
        self.assertEqual(cc.judge(self.blob(80, 160), [], False)[0], 'side_as_front')       # 2.0 upright, no front
        self.assertEqual(cc.judge(self.blob(50, 175), [], True)[0], None)                   # 3.5 in a packet segment
        self.assertEqual(cc.judge(self.blob(100, 125), [], False)[0], None)                 # a real carton seen at an angle

    def test_segment_kind_from_the_images_sent(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td); (r/'jobs').mkdir()
            (r/'jobs/G01-a1.json').write_text(json.dumps({'references': [{'kind': 'image', 'path': '/x/carton-views.jpg'}, {'kind': 'image', 'path': '/x/carton-hand-scale.jpg'}]}))
            (r/'jobs/G02-a1.json').write_text(json.dumps({'references': [{'kind': 'image', 'path': '/x/carton-views.jpg'}, {'kind': 'image', 'path': '/x/opened-packet-handheld.jpg'}]}))
            (r/'jobs/G03-a1.json').write_text(json.dumps({'references': [{'kind': 'image', 'path': '/x/stick-handheld-scale.jpg'}]}))
            self.assertEqual([cc.segment_kind(r, g) for g in ('G01', 'G02', 'G03')], ['carton', 'mixed', None])


class Measure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='carton-check-'); cls.d = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def test_a_real_carton_passes_and_a_stretched_one_is_flagged(self):
        carton_movie(self.d/'ok.mp4', 240, 312)
        carton_movie(self.d/'tall.mp4', 240, 480)
        carton_movie(self.d/'slim.mp4', 140, 400, word=False)
        ok = cc.check(self.d/'ok.mp4', work=self.d/'w_ok')
        tall = cc.check(self.d/'tall.mp4', out=self.d/'tall.json', work=self.d/'w_tall')
        slim = cc.check(self.d/'slim.mp4', work=self.d/'w_slim')
        self.assertEqual(ok['flags'], [])
        self.assertEqual([f['kind'] for f in tall['flags']], ['front_stretched'])
        self.assertAlmostEqual(tall['flags'][0]['worst'], 2.0, delta=0.15)
        self.assertEqual(tall['flags'][0]['goof'], 'shape'); self.assertTrue(Path(tall['sheet']).is_file())
        self.assertEqual([f['kind'] for f in slim['flags']], ['too_slim'])

    def test_select_gate_needs_a_look(self):
        import swap
        r = self.d/'run'; (r/'jobs').mkdir(parents=True, exist_ok=True)
        (r/'jobs/G01-a1.json').write_text(json.dumps({'references': [{'kind': 'image', 'path': '/x/carton-views.jpg'}]}))
        carton_movie(self.d/'tall2.mp4', 240, 480)
        a = argparse.Namespace(job='G01-a1', candidate='c1', carton_ok=None, note=None)
        with self.assertRaises(SystemExit) as e: swap.carton_gate(r, 'G01', a, self.d/'tall2.mp4', 0.0, 2.0, {})
        self.assertIn('front_stretched', str(e.exception)); self.assertIn('--carton-ok', str(e.exception))
        a.carton_ok = '看过：穿帮，已列入补做'
        rec = swap.carton_gate(r, 'G01', a, self.d/'tall2.mp4', 0.0, 2.0, {})
        self.assertEqual(rec['ok'], a.carton_ok); self.assertEqual(rec['flags'][0]['kind'], 'front_stretched')
        # a retrim reselect of the same picture keeps the earlier look
        a2 = argparse.Namespace(job='G01-a1', candidate='c1', carton_ok=None, note='x | retrim')
        self.assertEqual(swap.carton_gate(r, 'G01', a2, self.d/'tall2.mp4', 0.0, 2.0, {'job': 'G01-a1', 'carton_check': rec})['ok'], a.carton_ok)
        # a segment that sent no carton image is not checked
        self.assertIsNone(swap.carton_gate(r, 'G02', a2, self.d/'tall2.mp4', 0.0, 2.0, {}))


class ProductReview(unittest.TestCase):
    def test_crops_cut_every_appearance_out(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td); carton_movie(d/'c.mp4', 120, 156, seconds=2)
            sheets = cc.product_crops(d/'c.mp4', 0.0, 2.0, d/'out')
            self.assertEqual(len(sheets), 1); self.assertTrue(Path(sheets[0]).is_file())

    def test_finish_waits_for_the_agents_look(self):
        import swap
        with tempfile.TemporaryDirectory() as td:
            fd = Path(td)/'final'; (fd/'product').mkdir(parents=True); (fd/'flashback').mkdir()
            (fd/'product/sheets.json').write_text(json.dumps({'master_sha256': 'm1', 'sheets': [{'sheet': 'x.jpg'}]}))
            (fd/'flashback/sheets.json').write_text(json.dumps({'master_sha256': 'm1', 'sheets': ['s.jpg']}))
            state = {'stages': {'final_screen': {'dir': str(fd)}}}
            self.assertEqual({x['review'] for x in swap.reviews_pending(Path(td), state)}, {'product-review', 'flashback-review'})
            state['stages'].update(flashback_review={'master_sha256': 'm1', 'verdict': 'hit'}, product_review={'master_sha256': 'm1', 'verdict': 'hit', 'hits': [1]})
            self.assertEqual([x['review'] for x in swap.reviews_pending(Path(td), state)], ['product-review'])      # a product goof still open
            state['stages']['product_review'].update(verdict='hit_accepted')
            self.assertEqual(swap.reviews_pending(Path(td), state), [])
            state['stages']['product_review']['master_sha256'] = 'm0'                                              # reviewed an older master
            self.assertEqual([x['review'] for x in swap.reviews_pending(Path(td), state)], ['product-review'])


if __name__ == '__main__':
    unittest.main()
