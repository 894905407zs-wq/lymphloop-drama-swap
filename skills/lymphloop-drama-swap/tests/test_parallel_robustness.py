"""Robustness for several agents / processes running videos in parallel (2026-10-24 bug-fix round).

Offline only: fake transports, tiny lavfi clips, no Gemini / APIMART calls.
  1  RUN lock               2  upload-cache write race        3  region_inpaint per-box gate
  4  Flash no-JSON retry     5  Gemini upload stale state      6  screen clips keyed on source bytes
  7  watermark classify/pad  8  '# views: none'                9  swap.py splice
  10 location-agnostic tool lookup
"""
import json, os, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
SCRIPTS = SKILL/'scripts'
sys.path.insert(0, str(SCRIPTS)); sys.path.insert(0, str(SKILL/'tests'))
import google_api as api  # noqa: E402
import media_pipeline as media  # noqa: E402
import swap  # noqa: E402


def run_swap(*args, ok=True):
    r = subprocess.run([sys.executable, str(SCRIPTS/'swap.py'), *map(str, args)], capture_output=True, text=True)
    if ok and r.returncode: raise AssertionError(r.stderr or r.stdout)
    return r


def last_json(text):
    text = text.strip()
    return json.loads(text[text.rfind('\n{') + 1:] if '\n{' in text else text)


def clip(path, color, freq, frames=48, fps=24, size='64x96'):
    media.ff(['-y', '-f', 'lavfi', '-i', f'color=c={color}:s={size}:r={fps}:d={frames / fps}', '-f', 'lavfi', '-i',
              f'sine=frequency={freq}:sample_rate=48000:duration={frames / fps}', '-c:v', 'libx264', '-preset', 'ultrafast',
              '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)])
    return path


def pcm(path, sr=8000):
    import numpy as np
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-i', str(path), '-vn', '-ac', '1', '-ar', str(sr), '-f', 's16le', '-'],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(float)


def frame_rgb(path, index, fps=24):
    import numpy as np
    info = media.probe(str(path)); w, h = info['video']['width'], info['video']['height']
    raw = subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-i', str(path), '-vf', f'select=eq(n\\,{index})', '-frames:v', '1',
                          '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).reshape(-1, 3).mean(axis=0)


# ---------------------------------------------------------------- 1 RUN lock
class RunLock(unittest.TestCase):
    def test_second_writer_is_refused_with_the_holder_named_and_status_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)/'run'; run.mkdir(); swap.save(run/'state.json', {'source': 'x', 'stages': {}})
            holder = subprocess.Popen([sys.executable, '-c', (
                'import sys,time; sys.path.insert(0, sys.argv[1]); sys.argv=["swap.py","generate"]; import google_api as api\n'
                'with api.lock(%r):\n print("held", flush=True); time.sleep(30)') % str(run/'.run.lock'),
                str(SCRIPTS)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), 'held')
                r = run_swap('merge', '--run', run, ok=False)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn('RUN is busy', r.stderr); self.assertIn(f'pid {holder.pid}', r.stderr); self.assertIn('swap.py generate', r.stderr)
                self.assertEqual(run_swap('status', '--run', run).returncode, 0)          # read-only: no lock
                t0 = time.monotonic(); r = run_swap('merge', '--run', run, '--lock-wait', '1', ok=False)
                self.assertIn('RUN is busy', r.stderr); self.assertGreaterEqual(time.monotonic() - t0, 0.9)
            finally:
                holder.kill(); holder.wait(); holder.stdout.close()
            # The kernel released the dead holder's lock: the next writer gets past the lock (and fails on its own
            # missing inputs, not on the lock).
            r = run_swap('merge', '--run', run, ok=False)
            self.assertNotIn('RUN is busy', r.stderr)

    def test_lock_policy(self):
        A = lambda cmd, **kw: type('A', (), {'cmd': cmd, 'no_flash': False, **kw})()
        self.assertFalse(swap.lock_needed(A('status')))
        self.assertFalse(swap.lock_needed(A('review', no_flash=True)))
        for cmd in ('init', 'prepare', 'watermark', 'plan', 'generate', 'review', 'select', 'splice', 'recut', 'final-screen'):
            self.assertTrue(swap.lock_needed(A(cmd)), cmd)


# ---------------------------------------------------------------- 2 upload cache race / atomic save
class UploadCache(unittest.TestCase):
    def test_parallel_processes_keep_every_cache_entry(self):
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)/'apimart/image_uploads.json'
            code = ('import sys; sys.path.insert(0, sys.argv[1]); import apimart_wan as sd\n'
                    'sd.call = lambda key, path, body=None, headers=None, **kw: {"url": "https://img/" + sys.argv[3]}\n'
                    'for i in range(5):\n'
                    '    p = sys.argv[2] + "/img-" + sys.argv[3] + "-" + str(i) + ".png"; open(p, "wb").write((sys.argv[3] + str(i)).encode())\n'
                    '    sd.upload_image("k", p, sys.argv[4])\n')
            procs = [subprocess.Popen([sys.executable, '-c', code, str(SCRIPTS), td, str(n), str(cache)], stderr=subprocess.PIPE, text=True) for n in range(6)]
            errs = [p.communicate()[1] for p in procs]
            self.assertEqual([p.returncode for p in procs], [0] * 6, errs)
            self.assertEqual(len(api.read(cache)), 30)                    # nothing lost, nothing crashed on a temp rename
            self.assertEqual([x for x in cache.parent.iterdir() if x.name.endswith('.tmp')], [])

    def test_threads_saving_one_file_never_collide(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)/'x.json'; errors = []
            def work(n):
                try:
                    for i in range(30): api.save(target, {'n': n, 'i': i})
                except Exception as e: errors.append(e)
            ts = [threading.Thread(target=work, args=(n,)) for n in range(8)]; [t.start() for t in ts]; [t.join() for t in ts]
            self.assertEqual(errors, []); self.assertIn('n', api.read(target))


# ---------------------------------------------------------------- 3 region_inpaint per-box gate
class RegionInpaintGate(unittest.TestCase):
    def setUp(self):
        import region_inpaint as ri
        self.ri = ri

    def test_parse_gates(self):
        jobs = self.ri.parse_jobs(['0,0,4,4:purple', '4,4,4,4', '8,8,4,4'], ['0,1', '0,1', '0,1'], ['none'])
        self.assertEqual([j[-1] for j in jobs], ['purple', 'none', 'none'])          # one --gate = all boxes without their own
        jobs = self.ri.parse_jobs(['0,0,4,4', '4,4,4,4'], ['0,1', '2,3'], ['purple', 'none'])
        self.assertEqual([j[-1] for j in jobs], ['purple', 'none'])                  # once per box, in order
        self.assertEqual(self.ri.parse_jobs(['0,0,4,4'], ['0,1'], ['purple'])[0][-1], 'purple')
        self.assertEqual(self.ri.parse_jobs(['0,0,4,4'], ['0,1'])[0], (0, 0, 4, 4, 0, 1, 'none'))
        with self.assertRaises(ValueError): self.ri.parse_jobs(['0,0,4,4'] * 3, ['0,1'] * 3, ['purple', 'none'])
        with self.assertRaises(ValueError): self.ri.parse_jobs(['0,0,4,4:blue'], ['0,1'])
        with self.assertRaises(ValueError): self.ri.parse_jobs(['0,0,4,4'], [])

    def test_gate_applies_only_to_its_own_box(self):
        with tempfile.TemporaryDirectory() as td:
            src = clip(Path(td)/'src.mp4', 'gray', 440, frames=12)
            def run(*args):
                rep = Path(td)/'rep.json'
                subprocess.run([sys.executable, str(SCRIPTS/'region_inpaint.py'), str(src), str(Path(td)/'out.mp4'), *args, '--report', str(rep)],
                               check=True, capture_output=True)
                return json.loads(rep.read_text())
            # gray picture has no purple: the gated box is skipped, the ungated one is inpainted on every frame of its range
            rep = run('--box', '2,2,10,10:purple', '--frames', '0,11', '--box', '20,20,10,10', '--frames', '0,5')
            self.assertEqual([j['frames_touched'] for j in rep['jobs']], [0, 6])
            rep = run('--box', '2,2,10,10', '--frames', '0,11', '--box', '20,20,10,10', '--frames', '0,5', '--gate', 'none', '--gate', 'purple')
            self.assertEqual([j['frames_touched'] for j in rep['jobs']], [12, 0])
            rep = run('--box', '2,2,10,10', '--frames', '0,11', '--box', '20,20,10,10', '--frames', '0,5', '--gate', 'purple')   # old form
            self.assertEqual([j['frames_touched'] for j in rep['jobs']], [0, 0])
            self.assertEqual(media.probe(str(Path(td)/'out.mp4'), include_frames=True)['frame_count'], 12)


# ---------------------------------------------------------------- 4 Flash no JSON
class FlashText:
    key = 'k'
    def __init__(self, texts): self.texts = list(texts); self.calls = []
    def json(self, url, **kw):
        self.calls.append(kw); t = self.texts.pop(0)
        return {'responseId': 'r', 'candidates': [{'content': {'parts': [{'text': t}]}, 'finishReason': 'STOP'}]}


class FlashNoJson(unittest.TestCase):
    def setUp(self):
        import flash_screen as fs
        self.fs = fs; self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)

    def tearDown(self): self.tmp.cleanup()

    def test_retries_with_a_stricter_instruction_then_succeeds(self):
        t = FlashText(['Sure! Here is my analysis of the clip.', '{"verdict": "accept"}'])
        state = self.fs.call_flash([('text', 'q')], {'type': 'OBJECT'}, self.root, t)
        self.assertEqual(state['result'], {'verdict': 'accept'}); self.assertEqual(len(t.calls), 2)
        parts = t.calls[1]['payload']['contents'][0]['parts']
        self.assertEqual(parts[-1]['text'], self.fs.STRICT_JSON); self.assertEqual(len(state['no_json_attempts']), 1)
        again = self.fs.call_flash([('text', 'q')], {'type': 'OBJECT'}, self.root, FlashText([]))   # cached
        self.assertTrue(again['cache_hit'])

    def test_fenced_json_is_accepted_and_three_misses_raise(self):
        self.assertEqual(self.fs.parse_json_object('```json\n{"a": 1}\n```'), {'a': 1})
        self.assertEqual(self.fs.parse_json_object('Answer: {"a": 2} done'), {'a': 2})
        self.assertIsNone(self.fs.parse_json_object('[1, 2]'))
        t = FlashText(['no', 'still no', 'nope'])
        with self.assertRaisesRegex(api.AdapterError, 'no JSON object'):
            self.fs.call_flash([('text', 'q2')], {'type': 'OBJECT'}, self.root, t)
        self.assertEqual(len(t.calls), 3)
        # not cached as final: a later run asks again
        state = self.fs.call_flash([('text', 'q2')], {'type': 'OBJECT'}, self.root, FlashText(['{"ok": true}']))
        self.assertEqual(state['result'], {'ok': True})

    def test_screen_records_a_failed_shot_and_continues(self):
        import shots
        src = clip(self.root/'s.mp4', 'red', 440, frames=50, fps=25)
        sj = self.root/'shots.json'
        media.save(sj, {'source': str(src), 'source_sha256': shots.sha(src), 'fps': '25', 'frame_count': 50,
                        'shots': [{'id': 'S001', 'in_frame': 0, 'out_frame': 25}, {'id': 'S002', 'in_frame': 25, 'out_frame': 50}]})
        good = json.dumps({'competitor_visible': 'no', 'visible_evidence': '', 'brand_text': [], 'ingestion_action': 'none', 'speech_present': False,
                           'speech_mentions_product': 'no', 'speech_transcript': '', 'captions_present': False, 'floating_watermark': False, 'confidence': 1})
        up = lambda *a, **k: {'uri': 'https://generativelanguage.googleapis.com/v1beta/files/f', 'mimeType': 'video/mp4'}
        with mock.patch.object(api, 'upload_file', up):
            summary = self.fs.screen(sj, self.root/'run', transport=FlashText(['x', 'y', 'z', good]))   # S001 fails 3x, S002 answers
        self.assertEqual(summary['error_ids'], ['S001']); self.assertIn('S001', summary['unsure_ids']); self.assertEqual(summary['keep_ids'], ['S002'])


# ---------------------------------------------------------------- 5 Gemini upload state / lock
class UploadRecovery(unittest.TestCase):
    def test_stale_starting_state_restarts_the_free_session(self):
        from test_google_api import FakeTransport, Response
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); src = root/'v.mp4'; src.write_bytes(b'bytes')
            d = root/'state/uploads'/api.file_sha(src); d.mkdir(parents=True)
            api.save(d/'state.json', {'status': 'starting', 'source': api.file_description(src)})     # worker died mid-start
            obj = {'name': 'files/f1', 'state': 'ACTIVE', 'uri': api.API + '/v1beta/files/f1'}
            t = FakeTransport([{'file': obj}, obj], [Response(headers={'X-Goog-Upload-URL': api.API + '/upload/s1'})])
            self.assertEqual(api.upload_file(src, root/'state', t), obj)
            self.assertEqual(api.read(d/'state.json')['restarted_starts'], 1)

    def test_live_holder_is_waited_for_dead_holder_cannot_exist(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td)/'.lock'; entered = threading.Event(); release = threading.Event()
            def hold():
                with api.lock(path):
                    entered.set(); release.wait(5)
            t = threading.Thread(target=hold); t.start(); entered.wait(5)
            with self.assertRaisesRegex(api.AdapterError, f'held by pid {os.getpid()}'):
                with api.lock(path): pass
            threading.Timer(0.5, release.set).start()
            with api.lock(path, wait=5): got = True                                          # waited, then took it
            t.join(); self.assertTrue(got)
            # a killed holder process: the kernel drops its flock, the next worker takes it immediately
            p = subprocess.Popen([sys.executable, '-c', 'import sys,time; sys.path.insert(0, sys.argv[1]); import google_api as api\n'
                                  'with api.lock(sys.argv[2]):\n print(1, flush=True); time.sleep(30)', str(SCRIPTS), str(path)], stdout=subprocess.PIPE)
            p.stdout.readline(); p.kill(); p.wait(); p.stdout.close()
            with api.lock(path): pass


# ---------------------------------------------------------------- 6 clips keyed on source bytes
class ScreenClipCache(unittest.TestCase):
    def test_a_rerendered_source_gets_new_clips(self):
        import flash_screen as fs, shots
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); clips = root/'clips'
            answer = json.dumps({'competitor_visible': 'no', 'visible_evidence': '', 'brand_text': [], 'ingestion_action': 'none', 'speech_present': False,
                                 'speech_mentions_product': 'no', 'speech_transcript': '', 'captions_present': False, 'floating_watermark': False, 'confidence': 1})
            up = lambda *a, **k: {'uri': 'https://generativelanguage.googleapis.com/v1beta/files/f', 'mimeType': 'video/mp4'}
            for color in ('red', 'blue'):
                src = clip(root/'master.mp4', color, 440, frames=25, fps=25)
                media.save(root/'shots.json', {'source': str(src), 'source_sha256': shots.sha(src), 'fps': '25', 'frame_count': 25,
                                               'shots': [{'id': 'S001', 'in_frame': 0, 'out_frame': 25}]})
                with mock.patch.object(api, 'upload_file', up):
                    fs.screen(root/'shots.json', root/'run', clip_dir=clips, transport=FlashText([answer]))
                rgb = frame_rgb(clips/'S001.mp4', 3)
                self.assertGreater(rgb[0] if color == 'red' else rgb[2], 150, color)

    def test_final_screen_folder_is_keyed_on_the_master(self):
        src = (SCRIPTS/'swap.py').read_text()
        self.assertIn("fdir = run/'final'/master_sha[:16]", src)


# ---------------------------------------------------------------- 7 watermark classification + fade pad
class WatermarkScan(unittest.TestCase):
    def test_pad_and_bridge(self):
        dets = [{'frame': f, 'bbox': [10, 10, 20, 8], 'classification': 'floating_overlay', 'text': 'LYMPHORIA'} for f in (20, 21, 25)]
        n = swap.pad_overlay_fades(dets, 100, 64, 96, pad=5)
        frames = sorted({d['frame'] for d in dets})
        self.assertEqual(frames, list(range(15, 31)))                      # 15-19 fade-in, 22-24 bridged, 26-30 fade-out
        self.assertEqual(n, 13)
        pad = next(d for d in dets if d['frame'] == 15)
        self.assertEqual((pad['bbox'], pad['synthetic']), ([6, 6, 28, 16], 'fade_pad'))
        self.assertEqual(next(d for d in dets if d['frame'] == 23)['synthetic'], 'gap_fill')
        self.assertEqual(swap.pad_overlay_fades(dets, 100, 64, 96, pad=0), 0)

    def test_fade_pad_runs_back_to_the_cut_the_mark_came_in_with(self):
        import swap
        mk = lambda f: {'frame': f, 'text': 'LYMPHORIA', 'bbox': [10, 10, 12, 60], 'classification': 'floating_overlay'}
        dets = [mk(f) for f in range(40, 50)]
        swap.pad_overlay_fades(dets, 100, 64, 96, pad=5, cuts=[31, 58, 90])      # shot starts at 31, next cut at 58
        frames = sorted({d['frame'] for d in dets})
        self.assertEqual((frames[0], frames[-1]), (31, 57))                       # 293 at 52 s: the mark is there from the cut
        dets = [mk(f) for f in range(40, 50)]
        swap.pad_overlay_fades(dets, 100, 64, 96, pad=5, cuts=[10, 80])           # cuts too far away: the plain pad
        frames = sorted({d['frame'] for d in dets}); self.assertEqual((frames[0], frames[-1]), (35, 54))

    def test_each_mark_gets_its_own_fade_and_brand_captions_get_none(self):
        # 305 at 127 s: a "LYMPHORIA 4 HERBS" caption came up first, then the corner watermark faded in
        cap = [{'frame': f, 'bbox': [10, 80, 40, 8], 'classification': 'floating_overlay', 'text': 'LYMPHORIA 4 HERBS'} for f in range(10, 40)]
        mark = [{'frame': f, 'bbox': [2, 2, 6, 30], 'classification': 'floating_overlay', 'text': 'LYMPHORIA'} for f in range(20, 30)]
        dets = cap + mark
        swap.pad_overlay_fades(dets, 100, 64, 96, pad=5)
        added = [d for d in dets if d.get('synthetic')]
        self.assertEqual(sorted(d['frame'] for d in added), list(range(15, 20)) + list(range(30, 35)))
        self.assertTrue(all(d['bbox'][1] < 10 for d in added))            # all on the corner mark, none on the caption

    def test_unreviewed_ocr_boxes_are_classified_and_apply_runs_without_editing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); src = clip(root/'src.mp4', 'gray', 440, frames=50, fps=25, size='96x128'); run = root/'run'
            run_swap('init', '--video', src, '--run', run, '--out', root/'out'); run_swap('prepare', '--run', run)
            ocr = {'detections': [{'frame': f, 'text': '@lymphoria', 'bbox': [10, 10, 30, 10], 'classification': 'unreviewed'} for f in (20, 21, 22)]
                                 + [{'frame': 30, 'text': 'LYMPHORIA tincture', 'bbox': [40, 60, 30, 10], 'classification': 'printed_label_keep'}]}
            (root/'ocr.json').write_text(json.dumps(ocr))
            refused = run_swap('watermark', 'scan', '--run', run, '--scan-json', root/'ocr.json', ok=False)
            self.assertIn('accept the competitor profile first', refused.stderr)    # the watermark word comes from the profile
            out = last_json(run_swap('watermark', 'scan', '--run', run, '--scan-json', root/'ocr.json', '--fade-pad', '4', '--target', 'LYMPHORIA').stdout)
            self.assertEqual((out['ocr_overlay_frames'], out['overlay_frames']), (3, 11))
            scan = swap.read(run/'watermark/scan.json')
            self.assertNotIn('unreviewed', {d['classification'] for d in scan['detections']})
            self.assertEqual(next(d for d in scan['detections'] if d['frame'] == 30)['classification'], 'printed_label_keep')   # an agent's choice stays
            done = last_json(run_swap('watermark', 'apply', '--run', run, '--note', 'test').stdout)
            self.assertEqual(done['modified_frames'], 11)


# ---------------------------------------------------------------- 8 views: none
class ViewsNone(unittest.TestCase):
    def test_parse_views(self):
        self.assertEqual(swap.parse_views('none'), []); self.assertEqual(swap.parse_views(' None '), [])
        self.assertEqual(swap.parse_views('carton_sheet, handheld_photo'), ['carton_sheet', 'handheld_photo'])
        self.assertEqual(swap.parse_views(''), [swap.DEFAULT_VIEWS]); self.assertEqual(swap.parse_views(None), [swap.DEFAULT_VIEWS])

    def test_generate_sends_only_the_video_reference(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)/'run'; (run/'prompts').mkdir(parents=True); (run/'plan').mkdir()
            ref = clip(Path(td)/'G01.hq.mp4', 'gray', 440, frames=48, size='720x1280')
            swap.save(run/'state.json', {'source': str(ref), 'probe': {'video': {'width': 720, 'height': 1280}}, 'stages': {}})
            swap.save(run/'plan/segments.json', {'fps': 24, 'segments': [{'id': 'G01', 'in_frame': 0, 'out_frame': 48, 'duration_s': 2.0, 'kinds': ['vo'],
                                                                           'rewrites': {'dialogue': [], 'vo': []}}]})
            (run/'prompts/G01-a1.txt').write_text('# views: none\n【生成目标】\n复刻@视频1的灰色画面与英语旁白。\n\n【参考素材职责】\n@视频1用于复刻画面和旁白节奏，完全移除原片字幕。\n\n【主体与关系】\n灰色背景。\n\n【事件脚本】\n阶段一：固定机位灰色画面，画外旁白（平静，用英语）：{It tastes great.}。结束时画面仍是灰色。\n\n【保持一致】\n听者自然闭口；无bgm；画面不显示字幕。\n')
            a = type('A', (), {'run': str(run), 'segment': 'G01', 'attempt': 1, 'views': None, 'mute': None, 'clean_captions': False,
                               'aspect_ratio': None, 'candidates': 1, 'prepare_only': False})()
            import contextlib, io
            import apimart_wan as sd
            with mock.patch.object(sd, 'credential', side_effect=AssertionError('prepare is offline')), \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(SystemExit, 'inspect the upload video'):
                    swap.cmd_generate(a)
                a.prepare_only = True
                swap.cmd_generate(a)
            job_path = run/'jobs/G01-a1.json'
            job = swap.read(job_path)
            self.assertEqual([r['kind'] for r in job['references']], ['video'])
            self.assertGreaterEqual(job['references'][0]['duration_seconds'], 4)
            self.assertEqual(job['source_segment_duration_seconds'], 2)
            self.assertEqual((job['generation_mode'], job['output_seconds']), ('reference', 4))
            self.assertEqual(job['references'][0]['duration_seconds'], 4)                 # held on the last frame up to the output length
            self.assertIn('阶段一：固定机位灰色画面', Path(job['prompt_file']).read_text())   # five-section body, '#' lines dropped
            self.assertTrue(list((run/'clips').glob('G01.upload-*.jpg')))
            sd.validate(job)
            self.assertNotIn('image_urls', sd.payload(job, 'p', [], ['https://v']))
            source = Path(job['references'][0]['path'])
            self.assertEqual(swap.sha(source), job['references'][0]['sha256'])
            with source.open('ab') as f:
                f.write(b'changed after review')
            with self.assertRaisesRegex(sd.AdapterError, 'Prepared reference changed'):
                sd.signature(job)


# ---------------------------------------------------------------- 9 splice
class Splice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name); self.run = run = root/'run'
        res = run/'generation/results'; res.mkdir(parents=True); (run/'generation/jobs').mkdir(); (run/'plan').mkdir()
        swap.save(run/'state.json', {'source': 'x', 'stages': {}})
        swap.save(run/'plan/segments.json', {'fps': 24, 'segments': [{'id': 'G01', 'in_frame': 0, 'out_frame': 54, 'duration_s': 2.25, 'kinds': ['dialogue'],
                                                                       'rewrites': {'dialogue': [], 'vo': []}}]})
        for att, color, freq in (('a1', 'red', 440), ('a2', 'blue', 1000)):
            out = clip(res/f'G01-{att}-c1.mp4', color, freq)
            swap.save(run/f'generation/jobs/G01-{att}.json', {'type': 'apimart_job_ledger', 'provider': 'apimart', 'job_id': f'G01-{att}',
                                                              'candidates': {'c1': {'status': 'downloaded', 'output_path': str(out)}}})

    def tearDown(self): self.tmp.cleanup()

    def dominant(self, x, a, b, sr=8000):
        import numpy as np
        seg = x[int(a * sr):int(b * sr)]; spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        return np.fft.rfftfreq(len(seg), 1 / sr)[int(np.argmax(spec))], float(np.sqrt(np.mean(seg ** 2)))

    def test_pictures_audio_window_mute_hold_and_select(self):
        args = ['splice', '--run', self.run, '--segment', 'G01', '--out-attempt', 'a3', '--picture', 'G01-a1-c1:0-23',
                '--picture', 'G01-a2-c1:24-47', '--audio-window', 'G01-a2-c1:0.5-1.0@0.2', '--mute-window', '1.5-1.8',
                '--hold-last', '6', '--note', 'a1 picture first second, a2 after']
        r = run_swap(*args, ok=False)                     # a2's sound over a1's picture: refused unless no mouth speaks there (11-21)
        self.assertIn('0.20-0.70s', r.stderr); self.assertIn('--no-mouth', r.stderr)
        r = run_swap(*args, '--no-mouth', '0.2-0.45', '--no-mouth', '0.45-0.7', '--out-attempt', '9')     # touching windows cover it together (2026-11-22)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_swap(*args, '--no-mouth', '0.2-0.7')
        out = last_json(r.stdout); self.assertEqual((out['job'], out['frames']), ('G01-a3', 54))
        mp4 = self.run/'generation/results/G01-a3-c1.mp4'
        info = media.probe(str(mp4), include_frames=True)
        self.assertEqual(info['frame_count'], 54); self.assertEqual(media.ratio(info['fps']), 24)
        self.assertGreater(frame_rgb(mp4, 10)[0], 150); self.assertGreater(frame_rgb(mp4, 30)[2], 150); self.assertGreater(frame_rgb(mp4, 53)[2], 150)
        x = pcm(mp4)
        self.assertAlmostEqual(len(x) / 8000, 2.25, delta=0.05)
        f, _ = self.dominant(x, 0.05, 0.18); self.assertAlmostEqual(f, 440, delta=30)          # first picture and its own sound
        f, _ = self.dominant(x, 0.27, 0.65); self.assertAlmostEqual(f, 1000, delta=30)         # pasted window 0.2-0.7
        f, _ = self.dominant(x, 0.8, 0.95); self.assertAlmostEqual(f, 440, delta=30)
        f, _ = self.dominant(x, 1.1, 1.4); self.assertAlmostEqual(f, 1000, delta=30)  # second picture's own sound
        _, rms = self.dominant(x, 1.52, 1.78); self.assertLess(rms, 30)                         # muted
        _, rms = self.dominant(x, 1.9, 1.98); self.assertGreater(rms, 1000)                     # unmuted part of the second picture
        ledger = swap.read(self.run/'generation/jobs/G01-a3.json')
        self.assertEqual((ledger['type'], ledger['provider'], ledger['note']), ('apimart_job_ledger', 'local_splice', 'a1 picture first second, a2 after'))
        self.assertEqual((ledger['recipe']['foreign_sound'], ledger['recipe']['no_mouth']), ([[0.2, 0.7]], [[0.2, 0.7]]))
        c1 = ledger['candidates']['c1']
        self.assertEqual((c1['status'], c1['cost_usd'], c1['output_sha256']), ('downloaded', 0, swap.sha(mp4)))
        self.assertAlmostEqual(c1['duration_seconds'], 2.25, delta=0.05)
        sel = last_json(run_swap('select', '--run', self.run, '--job', 'G01-a3', '--candidate', 'c1', '--note', 'splice').stdout)
        self.assertEqual(sel['media']['path'], str(mp4.resolve())); self.assertAlmostEqual(sel['clip_out'], 2.25, delta=0.05)
        # the id is taken now; a real generation with it is refused too
        r = run_swap('splice', '--run', self.run, '--segment', 'G01', '--out-attempt', 'a3', '--picture', 'G01-a1-c1:0-10', ok=False)
        self.assertIn('already exists', r.stderr)

    def test_audio_source_and_bad_ranges(self):
        r = run_swap('splice', '--run', self.run, '--segment', 'G01', '--out-attempt', '4', '--picture', 'G01-a1-c1:0-47', '--audio', 'G01-a2-c1', ok=False)
        self.assertIn('0.00-2.00s', r.stderr)
        r = run_swap('splice', '--run', self.run, '--segment', 'G01', '--out-attempt', '4', '--picture', 'G01-a1-c1:0-47', '--audio', 'G01-a2-c1', '--no-mouth', '0-2')
        run_swap('splice', '--run', self.run, '--segment', 'G01', '--out-attempt', '6', '--picture', 'G01-a1-c1:0-23',
                 '--picture', 'G01-a2-c1:24-47')                  # each picture with its own sound: no declaration needed
        x = pcm(self.run/'generation/results/G01-a4-c1.mp4')
        f, _ = self.dominant(x, 0.2, 1.8); self.assertAlmostEqual(f, 1000, delta=30)
        r = run_swap('splice', '--run', self.run, '--segment', 'G01', '--out-attempt', 'a5', '--picture', 'G01-a1-c1:0-99', ok=False)
        self.assertIn('outside', r.stderr)
        r = run_swap('splice', '--run', self.run, '--segment', 'G01', '--out-attempt', 'a5', '--picture', 'G01-a9-c1:0-9', ok=False)
        self.assertIn('not found', r.stderr)
        r = run_swap('splice', '--run', self.run, '--segment', 'G09', '--out-attempt', 'a5', '--picture', 'G01-a1-c1:0-9', ok=False)
        self.assertIn('unknown segment', r.stderr)


# ---------------------------------------------------------------- 10 tool lookup
class ToolLookup(unittest.TestCase):
    def fake(self, d, name):
        p = Path(d)/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_text('#!/bin/sh\n'); p.chmod(0o755); return str(p.resolve())

    def test_order_env_skill_bin_path_codex(self):
        with tempfile.TemporaryDirectory() as td:
            env_bin, skill_bin, path_bin, codex = (self.fake(Path(td)/x, 'ffmpeg') for x in ('env', 'skill', 'path', 'codex'))
            clean = {k: v for k, v in os.environ.items() if k not in ('FFMPEG_BIN', 'FFPROBE_BIN')}
            with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(media, 'SKILL_BIN', Path(td)/'skill'), \
                    mock.patch.object(media, 'CODEX_TOOLS', Path(td)/'codex'), \
                    mock.patch.object(media.shutil, 'which', return_value=path_bin), mock.patch.object(media.Path, 'home', return_value=Path(td)/'h'):
                with mock.patch.dict(os.environ, {'FFMPEG_BIN': env_bin}): self.assertEqual(media.binary('ffmpeg'), env_bin)
                self.assertEqual(media.binary('ffmpeg'), skill_bin)
                os.remove(skill_bin); self.assertEqual(media.binary('ffmpeg'), path_bin)
                with mock.patch.object(media.shutil, 'which', return_value=None), mock.patch.object(media.Path, 'is_file', lambda p: str(p).startswith(td) and os.path.isfile(p)):
                    self.assertEqual(media.binary('ffmpeg'), codex)

    def test_host_root_and_cloudflared_follow_the_copy(self):
        import hostd
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)/'home'
            env = {k: v for k, v in os.environ.items() if k not in ('LYMPHLOOP_HOST_ROOT', 'CLOUDFLARED_BIN')}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(hostd.Path, 'home', return_value=home):
                self.assertEqual(hostd.default_root(home/'.claude/skills/lymphloop-swap-claude'), home/'.cache/lymphloop-host-claude')
                self.assertEqual(hostd.default_root(home/'.codex/skills/lymphloop-swap-claude'), home/'.cache/lymphloop-host')
                with mock.patch.dict(os.environ, {'LYMPHLOOP_HOST_ROOT': str(Path(td)/'hr')}):
                    self.assertEqual(hostd.default_root(home/'.claude/skills/x'), Path(td)/'hr')
                # cloudflared: env -> skill bin -> ~/.codex/tools/wan-media/2026.10.0
                codex_cf = self.fake(home/'.codex/tools/wan-media/2026.10.0', 'cloudflared')
                with mock.patch.object(hostd, 'SKILL', Path(td)/'skill'), mock.patch.object(hostd.shutil, 'which', return_value=None):
                    self.assertEqual(str(hostd.cloudflared_path().resolve()), codex_cf)
                    skill_cf = self.fake(Path(td)/'skill/bin', 'cloudflared')
                    self.assertEqual(str(hostd.cloudflared_path().resolve()), skill_cf)
                    env_cf = self.fake(Path(td)/'env', 'cloudflared')
                    with mock.patch.dict(os.environ, {'CLOUDFLARED_BIN': env_cf}): self.assertEqual(str(hostd.cloudflared_path().resolve()), env_cf)

    def test_scripts_and_tests_use_the_one_lookup(self):
        """No script or test builds a tool path of its own (FFMPEG_BIN / bin/ffmpeg / ~/.codex/tools): everything goes
        through media_pipeline.binary (ffmpeg/ffprobe) or hostd.cloudflared_path, so the files run from either copy."""
        import re
        allowed = {'media_pipeline.py', 'hostd.py', 'test_parallel_robustness.py'}
        for f in [*SCRIPTS.glob('*.py'), *SCRIPTS.glob('*.swift'), *(SKILL/'tests').glob('*.py')]:
            if f.name in allowed: continue
            text = f.read_text()
            for pat in (r"FFMPEG_BIN|FFPROBE_BIN", r"'bin'\s*/\s*'ff", r"/bin/ff(mpeg|probe)", r"\.codex/tools", r"/Users/", r"\.claude/skills", r"lymphloop-host"):
                self.assertIsNone(re.search(pat, text), f'{f.name}: {pat}')
            for line in text.splitlines():
                if re.search(r"""\[\s*['"]ff(mpeg|probe)['"]""", line): self.fail(f'{f.name} calls a bare ffmpeg: {line.strip()}')


if __name__ == '__main__':
    unittest.main()
