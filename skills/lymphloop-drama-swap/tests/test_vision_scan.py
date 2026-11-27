"""scripts/vision_scan.py: one text / face scanner interface over macOS Vision and RapidOCR + YuNet.

The clip used here prints its own frame number on every frame, so a scan proves both that the text is read and
that every detection carries the right frame (including after a seek to the middle of the clip)."""
import json, os, re, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL/'scripts'))
import media_pipeline as media, oscompat, vision_scan as vs  # noqa: E402

try:
    import rapidocr  # noqa: F401
    HAVE_RAPIDOCR = True
except ImportError:
    HAVE_RAPIDOCR = False
FONT = oscompat.find_font(*oscompat.SANS_BOLD) or oscompat.find_font(*oscompat.SANS_REGULAR)
ENGINES = (['vision'] if vs.engine() == 'vision' else []) + (['rapidocr'] if HAVE_RAPIDOCR else [])


def numbered_clip(path, frames=60, mark=(12, 40)):
    """720x1280, 30 fps: 'frame N' on every frame and LYMPHORIA.CO on frames mark[0]..mark[1]."""
    font = media.filter_quote(FONT)
    vf = (f"drawtext=fontfile='{font}':text='LYMPHORIA.CO':fontsize=44:fontcolor=white@0.9:x=60:y=200:enable='between(n,{mark[0]},{mark[1]})',"
          f"drawtext=fontfile='{font}':text='frame %{{frame_num}}':fontsize=60:fontcolor=white:x=(w-tw)/2:y=900")
    subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c=0x334455:s=720x1280:r=30:d={frames / 30}',
                    '-vf', vf, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-g', '30', str(path)], check=True)


class Commands(unittest.TestCase):
    def test_engine_choice(self):
        with mock.patch.dict(os.environ, {'LYMPHLOOP_OCR_ENGINE': 'rapidocr'}): self.assertEqual(vs.engine(), 'rapidocr')
        with mock.patch.dict(os.environ, {'LYMPHLOOP_OCR_ENGINE': 'vision'}): self.assertEqual(vs.engine(), 'vision')
        with mock.patch.dict(os.environ, {'LYMPHLOOP_OCR_ENGINE': ''}):
            if not oscompat.MACOS: self.assertEqual(vs.engine(), 'rapidocr')      # Vision exists on macOS only

    def test_rapidocr_commands_run_this_interpreter(self):
        with mock.patch.dict(os.environ, {'LYMPHLOOP_OCR_ENGINE': 'rapidocr'}), tempfile.TemporaryDirectory() as d:
            for cmd, mode in ((vs.text_cmd(d), 'text'), (vs.watermark_cmd(d), 'watermark'), (vs.people_cmd(d), 'people')):
                self.assertEqual(cmd, [sys.executable, str(SKILL/'scripts'/'vision_scan.py'), mode])
            self.assertEqual(len(vs.fingerprint(vs.text_cmd(d))), 64)

    def test_a_single_program_path_is_still_a_command(self):
        # callers written for the compiled Swift binary (and their test doubles) hand over one path
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'scanner'; p.write_bytes(b'x')
            self.assertEqual(vs.argv(p), [str(p)]); self.assertEqual(vs.argv([p, 'text']), [str(p), 'text'])
            self.assertEqual(vs.fingerprint(p), vs.fingerprint([str(p)]))

    def test_ranges(self):
        self.assertEqual(vs.parse_ranges('8-64,100-120'), [(8, 64), (100, 120)])
        self.assertEqual(vs.parse_ranges(''), []); self.assertEqual(vs.parse_ranges('x-3,5'), [])


@unittest.skipUnless(FONT and ENGINES, 'needs a sans font and an OCR engine (Vision, or pip install rapidocr)')
class Scans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory(); cls.root = Path(cls.dir.name)
        cls.clip = cls.root/'numbered.mp4'; numbered_clip(cls.clip)

    @classmethod
    def tearDownClass(cls): cls.dir.cleanup()

    def run_scan(self, engine, kind, *args):
        out = self.root/f'{kind}-{engine}-{len(args)}-{abs(hash(args)) % 10**6}.json'
        with mock.patch.dict(os.environ, {'LYMPHLOOP_OCR_ENGINE': engine}):
            cmd = vs.text_cmd(self.root) if kind == 'text' else vs.watermark_cmd(self.root)
        env = {**os.environ, 'LYMPHLOOP_OCR_ENGINE': engine, 'PYTHONUTF8': '1'}
        r = subprocess.run([*cmd, str(self.clip), str(out), *args], capture_output=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode('utf-8', 'replace'))
        return json.loads(out.read_text(encoding='utf-8'))

    def test_text_scan_reads_every_frame_and_numbers_it_right(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                scan = self.run_scan(engine, 'text')
                self.assertEqual((scan['type'], scan['frame_count'], scan['width'], scan['height']), ('lymphloop_text_scan', 60, 720, 1280))
                read = {}
                for d in scan['detections']:
                    m = re.fullmatch(r'frame\s*(\d+)', d['text'].strip(), re.I)
                    if m: read[d['frame']] = int(m.group(1))
                    x, y, w, h = d['bbox']
                    self.assertTrue(0 <= x < x + w <= 720 and 0 <= y < y + h <= 1280, d)
                self.assertGreaterEqual(len(read), 54, 'the frame number was not read on most frames')
                self.assertEqual({f for f, n in read.items() if f != n}, set(), 'a detection carries the wrong frame')
                marked = sorted({d['frame'] for d in scan['detections'] if 'LYMPHORIA' in d['text'].upper()})
                self.assertTrue(marked and 12 <= marked[0] <= 14 and 38 <= marked[-1] <= 40, marked)

    def test_watermark_scan_keeps_only_the_mark_on_the_asked_frames(self):
        for engine in ENGINES:
            with self.subTest(engine=engine):
                sparse = self.run_scan(engine, 'watermark', 'sha', vs.DEFAULT_PATTERN, 'LYMPHORIA', '4', '4-48')
                self.assertEqual(sparse['scanned_frames'], 11); self.assertEqual(sparse['coverage'], 'partial')
                self.assertEqual(sorted({d['frame'] for d in sparse['detections']}), [12, 16, 20, 24, 28, 32, 36, 40])
                dense = self.run_scan(engine, 'watermark', 'sha', vs.DEFAULT_PATTERN, 'LYMPHORIA', '1', '35-45')   # starts mid-clip: a seek
                self.assertEqual(sorted({d['frame'] for d in dense['detections']}), [35, 36, 37, 38, 39, 40])
                for d in sparse['detections'] + dense['detections']:
                    self.assertIn('LYMPHORIA', d['text'].upper()); self.assertEqual(d['classification'], 'unreviewed')
                    x, y, w, h = d['bbox']                    # drawn at x=60, y=200, 44 px: the box sits on the word
                    self.assertTrue(30 <= x <= 70 and 170 <= y <= 215 and 300 <= w <= 400 and 35 <= h <= 70, d['bbox'])

    def test_both_engines_agree(self):
        if len(ENGINES) < 2: self.skipTest('one engine on this machine')
        a, b = (self.run_scan(e, 'watermark', 'sha', vs.DEFAULT_PATTERN, 'LYMPHORIA', '1', '0-60') for e in ENGINES)
        self.assertEqual(sorted({d['frame'] for d in a['detections']}), sorted({d['frame'] for d in b['detections']}))

    def test_the_caption_check_runs_on_this_engine(self):
        # caption_clean is the caller every review goes through: a centred caption in the lower half is found
        import caption_clean
        for engine in ENGINES:
            with self.subTest(engine=engine), mock.patch.dict(os.environ, {'LYMPHLOOP_OCR_ENGINE': engine, 'PYTHONUTF8': '1'}):
                n_cap, n_all, texts = caption_clean.caption_frames(self.clip, self.root/f'cap-{engine}')
                self.assertEqual(n_all, 60); self.assertGreaterEqual(n_cap, 54)
                self.assertTrue(any('frame' in t.lower() for t in texts), texts)


class Faces(unittest.TestCase):
    def detect(self, engine, paths, work):
        with mock.patch.dict(os.environ, {'LYMPHLOOP_OCR_ENGINE': engine}): cmd = vs.people_cmd(work)
        r = subprocess.run([*cmd, *map(str, paths)], capture_output=True, env={**os.environ, 'LYMPHLOOP_OCR_ENGINE': engine, 'PYTHONUTF8': '1'})
        if r.returncode and b'urlopen' in r.stderr: self.skipTest('face model could not be downloaded (offline)')
        self.assertEqual(r.returncode, 0, r.stderr.decode('utf-8', 'replace'))
        return [json.loads(line) for line in r.stdout.decode('utf-8').splitlines() if line.strip()]

    def test_a_face_is_found_and_an_empty_wall_has_none(self):
        import cv2, numpy as np
        engines = (['vision'] if vs.engine() == 'vision' else []) + ['rapidocr']
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            cap = cv2.VideoCapture(str(SKILL/'assets'/'physical'/'sip-opened-sachet.mp4')); cap.set(cv2.CAP_PROP_POS_MSEC, 3000)
            ok, frame = cap.read(); cap.release()
            self.assertTrue(ok)
            face, wall = d/'face.jpg', d/'wall.jpg'
            cv2.imwrite(str(face), frame); cv2.imwrite(str(wall), np.full((640, 360, 3), 120, np.uint8))
            for engine in engines:
                with self.subTest(engine=engine):
                    rows = {Path(r['path']).name: r for r in self.detect(engine, [face, wall, d/'missing.jpg'], d/engine)}
                    self.assertEqual(len(rows['face.jpg']['faces']), 1)
                    x, y, w, h = rows['face.jpg']['faces'][0]       # normalised, origin top-left: the head is in the upper middle
                    self.assertTrue(0.2 < x < 0.6 and 0.1 < y < 0.45 and 0.25 < w < 0.65 and 0.2 < h < 0.55, (x, y, w, h))
                    self.assertEqual(rows['wall.jpg']['faces'], [])
                    self.assertEqual(rows['missing.jpg'].get('error'), 'unreadable')


if __name__ == '__main__':
    unittest.main()
