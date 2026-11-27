"""Machine-wide fetch gate (hostd.reserve/release) and the carton geometry check (flash_screen.qa + swap.py select).

2026-10-25: five sessions submitting at once put 20+ reference downloads on an uplink that tops out around 1.1 MB/s,
pushing single downloads past APIMART's 60 s limit; and Flash, given the product only in words, passed stretched,
fingertip-sized and multiplied cartons. Offline: no tunnel, no network, Flash and APIMART mocked."""
import argparse, json, os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import hostd  # noqa: E402

URL = 'https://abc-def.trycloudflare.com/{d}/{n}'


class Gate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); root = Path(self.td.name)
        self.patches = [mock.patch.object(hostd, 'GATE_DIR', root/'gate'), mock.patch.object(hostd, 'ROOT', root/'host'),
                        mock.patch.object(hostd, 'FETCH_SLOTS', 2)]
        for p in self.patches: p.start()
        (root/'host').mkdir(); self.root = root
        self.files = []
        for i in range(4):
            f = root/f'G0{i}.mp4'; f.write_bytes(b'x' * (1000 + i)); self.files.append(f)

    def tearDown(self):
        for p in self.patches: p.stop()
        self.td.cleanup()

    def ref(self, i): return (URL.format(d=f'd{i}', n=self.files[i].name), self.files[i])

    def log(self, i, sent, start=0, when=None):
        stamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when or time.time() + 1))
        with open(self.root/'host'/'access.log', 'a') as f:
            f.write(f'{stamp} GET /d{i}/{self.files[i].name} range=- from={start} sent={sent} secs=3.0 ua=Python/3.12 aiohttp/3.13.5 \n')

    def test_full_gate_blocks_until_the_reference_is_fully_sent(self):
        hostd.reserve([self.ref(0), self.ref(1)])
        with self.assertRaisesRegex(hostd.Error, 'stayed full'):
            hostd.reserve([self.ref(2)], wait=0.3)
        self.log(0, 600)                                     # APIMART's probe read: partial, slot stays taken
        with self.assertRaisesRegex(hostd.Error, 'stayed full'):
            hostd.reserve([self.ref(2)], wait=0.3)
        self.log(0, self.files[0].stat().st_size)            # the full download frees it
        self.assertEqual(len(hostd.reserve([self.ref(2)], wait=1)), 1)
        self.assertEqual(hostd.gate_status()['in_use'], 2)

    def test_a_download_logged_before_the_slot_does_not_count(self):
        self.log(0, self.files[0].stat().st_size, when=time.time() - 30)
        hostd.reserve([self.ref(0), self.ref(1)])
        with self.assertRaisesRegex(hostd.Error, 'stayed full'):
            hostd.reserve([self.ref(2)], wait=0.3)

    def test_unfetched_slots_expire_and_release_frees_at_once(self):
        ids = hostd.reserve([self.ref(0), self.ref(1)])
        hostd.release(ids[:1])
        self.assertEqual(len(hostd.reserve([self.ref(2)], wait=1)), 1)
        with mock.patch.object(hostd, 'FETCH_TIMEOUT', 0):
            self.assertEqual(hostd.gate_status()['in_use'], 0)

    def test_batch_larger_than_the_gate_goes_alone_and_foreign_urls_are_not_gated(self):
        self.assertEqual(len(hostd.reserve([self.ref(i) for i in range(3)], wait=1)), 3)   # empty gate: let it through
        self.assertEqual(hostd.reserve([('https://litter.catbox.moe/x.mp4', self.files[3])], wait=0.3), [])

    def test_two_skill_copies_share_one_gate(self):
        hostd.reserve([self.ref(0)])
        with mock.patch.object(hostd, 'ROOT', self.root/'other-copy'):
            hostd.reserve([self.ref(1)])
            with self.assertRaisesRegex(hostd.Error, 'stayed full'):
                hostd.reserve([self.ref(2)], wait=0.3)


class SubmitUsesGate(unittest.TestCase):
    def test_each_submission_reserves_its_reference_and_a_refused_one_releases_it(self):
        import apimart_wan as sd
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)/'RUN'; (run/'clips').mkdir(parents=True); (run/'prompts').mkdir()
            clip = run/'clips/G01.hq.mp4'; clip.write_bytes(b'video')
            (run/'prompts'/'G01.txt').write_text('prompt')
            job = {'job_id': 'G01-a1', 'run_dir': str(run), 'model': sd.MODEL, 'resolution': '480P', 'aspect_ratio': '9:16',
                   'duration_seconds': 6, 'prompt_file': str(run/'prompts'/'G01.txt'), 'candidates': 1, 'output_seconds': 6,
                   'references': [{'path': str(clip), 'kind': 'video', 'duration_seconds': 5.5}]}
            jp = Path(td)/'G01.json'; jp.write_text(json.dumps(job))
            reserved, released = [], []
            def call(key, path, payload=None, **kw): raise sd.HTTPFailure(422, 'bad prompt')
            with mock.patch.object(sd, 'VIDEO_HOST', 'tunnel'), mock.patch.object(sd, 'credential', return_value='k'), mock.patch.object(sd, 'validate', side_effect=lambda j: j), \
                    mock.patch.object(sd, 'call', side_effect=call), \
                    mock.patch.object(sd.hostd, 'publish', return_value=URL.format(d='d0', n='G01.hq.mp4')), \
                    mock.patch.object(sd.hostd, 'reserve', side_effect=lambda refs, **k: reserved.append(refs) or ['slot1']), \
                    mock.patch.object(sd.hostd, 'release', side_effect=lambda ids: released.append(ids)), \
                    mock.patch.object(sd.time, 'sleep'):
                ledgers = sd.run_batch([str(jp)])
            # a 422 is refused before any charge: re-queued MAX_FREE_RESUBMITS times, each submission reserving and releasing
            n = 1 + sd.MAX_FREE_RESUBMITS
            self.assertEqual(reserved, [[(URL.format(d='d0', n='G01.hq.mp4'), str(clip))]] * n)
            self.assertEqual(released, [['slot1']] * n)
            c1 = ledgers[0]['candidates']['c1']
            self.assertEqual((c1['status'], c1['generation_submit_count'], len(c1['unpaid_attempts'])), ('known_failed', n, sd.MAX_FREE_RESUBMITS))

    def test_an_unknown_outcome_is_never_resubmitted(self):
        import apimart_wan as sd
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)/'RUN'; (run/'clips').mkdir(parents=True); (run/'prompts').mkdir()
            clip = run/'clips/G01.hq.mp4'; clip.write_bytes(b'video'); (run/'prompts'/'G01.txt').write_text('prompt')
            job = {'job_id': 'G01-a1', 'run_dir': str(run), 'model': sd.MODEL, 'prompt_file': str(run/'prompts'/'G01.txt'), 'candidates': 1, 'output_seconds': 6,
                   'references': [{'path': str(clip), 'kind': 'video', 'duration_seconds': 5.5}]}
            jp = Path(td)/'G01.json'; jp.write_text(json.dumps(job))
            calls = []
            def call(key, path, payload=None, **kw): calls.append(path); raise TimeoutError()     # may have been accepted and charged
            with mock.patch.object(sd, 'VIDEO_HOST', 'tunnel'), mock.patch.object(sd, 'credential', return_value='k'), mock.patch.object(sd, 'validate', side_effect=lambda j: j), \
                    mock.patch.object(sd, 'call', side_effect=call), mock.patch.object(sd.hostd, 'publish', return_value='https://h/x.mp4'), \
                    mock.patch.object(sd.hostd, 'reserve', return_value=[]), mock.patch.object(sd.time, 'sleep'):
                ledgers = sd.run_batch([str(jp)])
            self.assertEqual((len(calls), ledgers[0]['candidates']['c1']['status']), (1, 'outcome_unknown'))


class CartonQA(unittest.TestCase):
    def setUp(self):
        import flash_screen
        self.fs = flash_screen
        self.td = tempfile.TemporaryDirectory(); d = Path(self.td.name)
        self.src = d/'src.mp4'; self.cand = d/'cand.mp4'; self.prompt = d/'p.txt'; self.sheet = d/'carton-sheet.jpg'
        for f in (self.src, self.cand, self.sheet): f.write_bytes(b'x')
        self.prompt.write_text('prompt'); self.d = d

    def tearDown(self): self.td.cleanup()

    def run_qa(self, result, carton):
        seen = {}
        def fake(parts, schema, run_dir, transport=None, fps=3):
            seen.update(parts=parts, schema=schema); return {'request_id': 'r', 'result': result}
        with mock.patch.object(self.fs, 'call_flash', side_effect=fake), mock.patch.object(self.fs.api, 'file_description', return_value={}):
            rec = self.fs.qa(self.src, self.cand, self.prompt, self.d/'qa.json', self.d, carton_ref=self.sheet if carton else None)
        return rec, seen

    def test_bad_carton_is_recorded_and_the_sheet_is_attached(self):
        # one generation per segment, always used (user 2026-11-11): the carton check records, it does not reject
        rec, seen = self.run_qa({'verdict': 'accept', 'carton_geometry': 'too_slim', 'carton_findings': ['3 slim boxes 0-4 s']}, True)
        self.assertIs(seen['schema'], self.fs.CARTON_SCHEMA)
        self.assertIn(('image', str(self.sheet)), seen['parts'])
        self.assertEqual((rec['result']['verdict'], rec['result']['carton_gate']), ('accept', 'fail'))

    def test_good_carton_keeps_the_verdict_and_non_carton_jobs_are_unchanged(self):
        rec, _ = self.run_qa({'verdict': 'accept', 'carton_geometry': 'ok', 'carton_findings': []}, True)
        self.assertEqual((rec['result']['verdict'], rec['result']['carton_gate']), ('accept', 'pass'))
        rec, seen = self.run_qa({'verdict': 'revise'}, False)
        self.assertIs(seen['schema'], self.fs.QA_SCHEMA)
        self.assertNotIn('carton_gate', rec['result'])


class SelectAlwaysTakesTheOneGeneration(unittest.TestCase):
    def setUp(self):
        import swap
        self.swap = swap
        self.td = tempfile.TemporaryDirectory(); run = Path(self.td.name)/'RUN'; self.run = run
        for d in ('jobs', 'qa', 'plan', 'generation/jobs', 'generation/results'): (run/d).mkdir(parents=True)
        (run/'state.json').write_text('{}')
        (run/'plan/segments.json').write_text(json.dumps({'fps': 30, 'segments': [{'id': 'G01', 'in_frame': 0, 'out_frame': 60, 'duration_s': 2.0}]}))
        out = run/'generation/results/G01-a1-c1.mp4'; out.write_bytes(b'x')
        (run/'generation/jobs/G01-a1.json').write_text(json.dumps({'candidates': {'c1': {'output_path': str(out)}}}))

    def tearDown(self): self.td.cleanup()

    def job(self, carton):
        refs = [{'path': '/x/clip.mp4', 'kind': 'video'}] + ([{'path': '/skill/assets/product/carton-sheet.jpg', 'kind': 'image'}] if carton else [])
        (self.run/'jobs/G01-a1.json').write_text(json.dumps({'job_id': 'G01-a1', 'references': refs}))

    def qa(self, gate):
        (self.run/'qa/G01-a1-c1.json').write_text(json.dumps({'result': {'carton_gate': gate, 'carton_geometry': 'too_slim', 'carton_findings': ['slim']}}))

    def select(self, **kw):
        a = argparse.Namespace(**{**dict(run=str(self.run), job='G01-a1', candidate='c1', clip_in=None, clip_out=None, note=None,
                                         keep_source_picture=False, audio_only=False, no_caption_clean=True), **kw})
        with mock.patch.object(self.swap.media, 'probe', return_value={'video_duration': 2.0}):
            self.swap.cmd_select(a)
        return json.loads((self.run/'plan/selections.json').read_text())['G01']

    def test_a_failed_carton_check_is_selected_and_recorded(self):
        self.job(True)
        self.assertIn('missing', self.select()['defects']['review'])
        self.qa('fail')
        entry = self.select()
        self.assertEqual((entry['job'], entry['defects']['carton_gate'], entry['defects']['carton_findings']), ('G01-a1', 'fail', ['slim']))

    def test_a_short_segment_is_cut_at_its_own_length_by_default(self):
        self.job(False)
        with mock.patch.object(self.swap.media, 'probe', return_value={'video_duration': 4.1}):
            a = argparse.Namespace(run=str(self.run), job='G01-a1', candidate='c1', clip_in=None, clip_out=None, note=None,
                                   keep_source_picture=False, audio_only=False, no_caption_clean=True)
            self.swap.cmd_select(a)                                   # the 2 s segment was padded to 4.1 s for the API
            self.assertEqual(json.loads((self.run/'plan/selections.json').read_text())['G01']['clip_out'], 2.0)
            a.clip_out = 3.0; self.swap.cmd_select(a)
            self.assertEqual(json.loads((self.run/'plan/selections.json').read_text())['G01']['clip_out'], 3.0)

    def test_source_picture_selections_still_work(self):
        self.job(True); self.assertEqual(self.select(keep_source_picture=True)['picture'], 'source')

    def test_a_segment_that_was_never_generated_can_keep_the_source(self):
        # Nuflos runs, 2026-11-12: two planned segments were not submitted (the product was a few pixels in an untouched
        # shot); `select --keep-source-picture` asked for a generation ledger and failed, and recut refuses a segment
        # without a selection.
        (self.run/'generation/jobs/G01-a1.json').unlink(); (self.run/'generation/results/G01-a1-c1.mp4').unlink()
        entry = self.select(keep_source_picture=True, candidate=None, note='left as the source; residue fixed on the master')
        self.assertEqual((entry['picture'], entry['generated'], entry['candidate']), ('source', False, None))
        self.assertNotIn('media', entry)                      # recut skips picture == 'source' entries without reading media
        with self.assertRaises(FileNotFoundError): self.select()                      # a generated selection still needs its ledger

    def test_a_generated_selection_needs_a_candidate(self):
        self.job(False)
        with self.assertRaises(SystemExit): self.select(candidate=None)

    def test_burned_in_captions_are_cleaned_off_the_generated_picture(self):
        self.job(False)
        import caption_clean
        made = {}
        def fake_clean(clip, out, work=None, short_edge=480, keep_audio=False, pixels=None, spoken=None):
            Path(out).write_bytes(b'y'); made.update(short_edge=short_edge, keep_audio=keep_audio, spoken=spoken)
            return {'frames_cleaned': 40, 'caption_texts': ['TASTES GREAT']}
        with mock.patch.object(caption_clean, 'caption_frames', return_value=(40, 60, ['TASTES GREAT'])), \
                mock.patch.object(caption_clean, 'clean', side_effect=fake_clean), \
                mock.patch.object(self.swap.media, 'probe', return_value={'video_duration': 2.0, 'video': {'width': 480, 'height': 854}}):
            a = argparse.Namespace(run=str(self.run), job='G01-a1', candidate='c1', clip_in=None, clip_out=None, note=None,
                                   keep_source_picture=False, audio_only=False, no_caption_clean=False)
            # a segment without speech has no spoken-word captions: the packet print is never inpainted
            self.swap.cmd_select(a)
            self.assertFalse(made); self.assertNotIn('caption_clean', json.loads((self.run/'plan/selections.json').read_text())['G01'])
            seg = {'id': 'G01', 'in_frame': 0, 'out_frame': 60, 'duration_s': 2.0, 'speech': [{'start': 0.2, 'end': 1.5, 'text': 'Tastes great.'}]}
            (self.run/'plan/segments.json').write_text(json.dumps({'fps': 30, 'segments': [seg]}))
            # a generated segment carries no captions, rewritten line or not (SKILL.md rule 6)
            self.swap.cmd_select(a)
        entry = json.loads((self.run/'plan/selections.json').read_text())['G01']
        self.assertTrue(entry['media']['path'].endswith('G01-a1-c1.decaptioned.mp4'))
        self.assertEqual((entry['caption_clean']['frames_cleaned'], made['short_edge'], made['keep_audio']), (40, 480, True))


if __name__ == '__main__':
    unittest.main()


class ApimartVideoUpload(unittest.TestCase):
    """User 2026-11-20 "隧道问题怎么解决": references are uploaded to APIMART (POST /v1/uploads/videos), not fetched
    through the tunnel; film 2210 had every tunnel submission refused."""
    def test_default_route_uploads_once_and_sends_the_cdn_url(self):
        import apimart_wan as sd
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)/'RUN'; (run/'clips').mkdir(parents=True); (run/'prompts').mkdir()
            clip = run/'clips/G01.hq.mp4'; clip.write_bytes(b'video'); (run/'prompts'/'G01.txt').write_text('prompt')
            job = {'job_id': 'G01-a1', 'run_dir': str(run), 'model': sd.MODEL, 'prompt_file': str(run/'prompts'/'G01.txt'), 'candidates': 1,
                   'output_seconds': 6, 'references': [{'path': str(clip), 'kind': 'video', 'duration_seconds': 6}]}
            jp = Path(td)/'G01.json'; jp.write_text(json.dumps(job))
            calls = []
            def call(key, path, payload=None, **kw):
                calls.append((path, payload))
                if path.startswith('/uploads/videos'): return {'url': 'https://cdn/G01.mp4', 'expires_at': 4102444800, 'bytes': 5}
                raise sd.HTTPFailure(422, 'stop here')
            self.assertEqual(sd.VIDEO_HOST, 'apimart')
            with mock.patch.object(sd, 'credential', return_value='k'), mock.patch.object(sd, 'validate', side_effect=lambda j: j), \
                    mock.patch.object(sd, 'call', side_effect=call), mock.patch.object(sd.hostd, 'publish', side_effect=AssertionError('no tunnel')), \
                    mock.patch.object(sd.hostd, 'reserve', side_effect=AssertionError('no gate')), mock.patch.object(sd.time, 'sleep'):
                sd.run_batch([str(jp)])
            uploads = [p for p, _ in calls if p.startswith('/uploads/videos')]
            gens = [b for p, b in calls if p == '/videos/generations']
            self.assertEqual(uploads, ['/uploads/videos?retention_days=3'])                # once, cached for the retries
            self.assertTrue(gens and all(b['video_urls'] == ['https://cdn/G01.mp4'] for b in gens))
            self.assertTrue(all(b['size'] == '9:16' and b['resolution'] == '720p' and b['duration'] == 6 for b in gens))
