import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import caption_clean as cc  # noqa: E402
import swap  # noqa: E402


class CaptionFilter(unittest.TestCase):
    W, H = 360, 640

    def test_centred_caption_lines_are_kept(self):
        self.assertTrue(cc.is_caption([120, 400, 120, 22], self.W, self.H))   # "Drops?" pill in the lower middle
        self.assertTrue(cc.is_caption([60, 260, 240, 30], self.W, self.H))    # "3 MONTHS EARLIER" title card

    def test_watermark_labels_and_top_text_are_left_alone(self):
        self.assertFalse(cc.is_caption([335, 11, 14, 67], self.W, self.H))    # rotated watermark at the edge
        self.assertFalse(cc.is_caption([120, 40, 120, 22], self.W, self.H))   # top band (signs, shop names)
        self.assertFalse(cc.is_caption([5, 420, 60, 20], self.W, self.H))     # off-centre label
        self.assertFalse(cc.is_caption([100, 400, 160, 120], self.W, self.H)) # too tall to be a caption line

    def test_boxes_are_held_around_detections(self):
        scan = {'width': self.W, 'height': self.H, 'frame_count': 20,
                'detections': [{'frame': 10, 'bbox': [120, 400, 120, 22], 'text': 'Drops?'}]}
        held, raw = cc.caption_boxes(scan)
        self.assertEqual(sorted(raw), [10])
        self.assertEqual(sorted(held), list(range(10 - cc.HOLD, 10 + cc.HOLD + 1)))


class OverlayGate(unittest.TestCase):
    def test_source_watermark_and_titles_are_caught_but_packet_print_is_not(self):
        from unittest import mock
        src = {'frame_count': 3, 'detections': [{'frame': 0, 'text': '@LYMPHORIA'}, {'frame': 1, 'text': 'WEEK 1'}, {'frame': 2, 'text': 'MONDAY'}]}
        cand = {'frame_count': 4, 'detections': [{'frame': 0, 'text': '© LYHPHORIA'}, {'frame': 1, 'text': 'MONDAY'},
                                                  {'frame': 2, 'text': 'yesnap LymphLoop'}, {'frame': 3, 'text': '12-IN-1 HERBAL BLEND'}]}
        with mock.patch.object(cc, '_scan', side_effect=[src, cand]):
            n, total, texts = cc.leftover_overlay_frames('cand.mp4', 'src.mp4', '/tmp')
        self.assertEqual((n, total), (2, 4)); self.assertEqual(texts, ['MONDAY', '© LYHPHORIA'])


class ApimartValidation(unittest.TestCase):
    def job(self, **kw):
        import apimart_wan as sd
        j = {'job_id': 'G01-a1', 'run_dir': '/tmp', 'model': sd.MODEL, 'duration_seconds': 10, 'prompt_file': '/tmp/p.txt', 'resolution': '720P',
             'aspect_ratio': '9:16', 'candidates': 1, 'generation_mode': 'reference', 'output_seconds': 10,
             'references': [{'path': '/tmp/v.mp4', 'kind': 'video', 'duration_seconds': 10}]}
        j.update(kw); return j

    def test_validates_reference_generation_inputs(self):
        import apimart_wan as sd
        from unittest import mock
        good = {'video_duration': 10, 'video': {'width': 720, 'height': 1280}}
        with mock.patch.object(sd.media, 'probe', return_value=good) as probe:
            self.assertEqual(sd.validate(self.job())['model'], sd.MODEL)
            for change, message in [
                ({'model': 'wan3.0-video'}, 'only generates with'),
                ({'generation_mode': 'edit'}, 'must be reference'),          # user 2026-11-11: video reference generation only
                ({'output_seconds': 10.5}, 'whole number of seconds'), ({'output_seconds': 3}, 'whole number of seconds'),
                ({'resolution': '480p'}, 'fixed to 720p'),
                ({'job_id': 'G01-a2'}, 'Only the a1'),
                ({'candidates': 2}, 'one candidate'),
                ({'references': []}, 'one reference video'),
                ({'references': [{'path': '/tmp/a.wav', 'kind': 'audio'}]}, 'video and image only'),
            ]:
                with self.subTest(change=change), self.assertRaisesRegex(sd.AdapterError, message):
                    sd.validate(self.job(**change))
            probe.return_value = {**good, 'video_duration': 15.0}; sd.validate(self.job())     # 14.9 s rounds up to a 15 s reference
            for secs in (3.99, 15.3):
                probe.return_value = {**good, 'video_duration': secs}
                with self.assertRaisesRegex(sd.AdapterError, 'at least 4 seconds and at most 15'):
                    sd.validate(self.job())
            probe.return_value = {**good, 'video': {'width': 1280, 'height': 720}}
            with self.assertRaisesRegex(sd.AdapterError, 'must be 9:16'):
                sd.validate(self.job())

    def test_a_refunded_failure_is_resubmitted_once_and_resume_submits_nothing_more(self):
        import apimart_wan as sd, tempfile, json
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)/'RUN'; (run/'clips').mkdir(parents=True); (run/'prompts').mkdir()
            clip = run/'clips/G01.hq.mp4'; clip.write_bytes(b'video'); img = Path(td)/'p.jpg'; img.write_bytes(b'img')
            jobs = []
            for g in ('G01', 'G02'):
                (run/'prompts'/f'{g}.txt').write_text('prompt ' + g)
                job = {'job_id': f'{g}-a1', 'run_dir': str(run), 'model': sd.MODEL, 'resolution': '480P', 'aspect_ratio': '9:16',
                       'duration_seconds': 6, 'prompt_file': str(run/'prompts'/f'{g}.txt'), 'candidates': 1, 'generation_mode': 'reference', 'output_seconds': 6,
                       'references': [{'path': str(clip), 'kind': 'video', 'duration_seconds': 5.5}, {'path': str(img), 'kind': 'image'}]}
                jp = Path(td)/f'{g}.json'; jp.write_text(json.dumps(job)); jobs.append(jp)
            submitted, published, polls = [], [], {}
            import threading
            lock = threading.Lock(); per_job = {}
            def call(key, path, payload=None, **kw):
                # the batch submits on threads: name the task after its job, so "G01's first task fails" does not
                # depend on which job reaches the API first (this test failed two runs in three before 2026-11-11)
                with lock:
                    submitted.append(payload); g = 'G01' if payload['prompt'].endswith('G01') else 'G02'
                    per_job[g] = per_job.get(g, 0) + 1
                    return {'data': {'task_id': 'task1' if (g, per_job[g]) == ('G01', 1) else f'{g}-task{per_job[g]}'}}
            def task_data(key, tid):
                with lock: polls[tid] = polls.get(tid, 0) + 1
                if tid == 'task1' and polls[tid] == 1: return {'status': 'failed', 'error': 'cannot fetch video_urls'}
                return {'status': 'completed', 'result': {'videos': [{'url': ['https://x/' + tid + '.mp4']}]}, 'cost': 0.1}
            class Resp:
                def __init__(self, *a, **k): pass
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def read(self): return b'mp4'
            with mock.patch.object(sd, 'VIDEO_HOST', 'tunnel'), mock.patch.object(sd, 'credential', return_value='k'), mock.patch.object(sd, 'validate', side_effect=lambda j: j), \
                    mock.patch.object(sd, 'call', side_effect=call), mock.patch.object(sd, 'task_data', side_effect=task_data), \
                    mock.patch.object(sd, 'upload_image', return_value='https://img'), \
                    mock.patch.object(sd.hostd, 'publish', side_effect=lambda p: published.append(p) or 'https://host/' + Path(p).name), \
                    mock.patch.object(sd.hostd, 'reserve', return_value=[]), \
                    mock.patch.object(sd.urllib.request, 'urlopen', side_effect=Resp), \
                    mock.patch.object(sd.media, 'probe', return_value={'video_duration': 6.0, 'frame_count': 144, 'fps': '24', 'audio': {}}), \
                    mock.patch.object(sd.time, 'sleep'):
                ledgers = sd.run_batch([str(p) for p in jobs])
                resumed = sd.run_batch([str(p) for p in jobs])
            self.assertEqual(len(submitted), 3)                          # G01 failed (refunded) -> same inputs again
            self.assertEqual(published.count(str(clip.resolve())), 2)   # republished for the second round
            for body in submitted:
                self.assertEqual({k: body[k] for k in (*sd.SETTINGS, 'duration')}, {
                    'model': 'seedance-2.0', 'resolution': '720p', 'size': '9:16', 'duration': 6,
                })
                self.assertNotIn('omni_reference_task_type', body)               # seedance-2.0: video_urls make it reference generation
                self.assertEqual(body['video_urls'], ['https://host/G01.hq.mp4'])
                self.assertEqual(body['image_urls'], ['https://img'])
                self.assertNotIn('generation_type', body)
            for result in (ledgers, resumed):
                states = [c['status'] for l in result for c in l['candidates'].values()]
                self.assertCountEqual(states, ['downloaded', 'downloaded'])
                self.assertCountEqual([l['candidates']['c1']['generation_submit_count'] for l in result], [2, 1])
                g01 = next(l for l in result if l['job_id'] == 'G01-a1')['candidates']['c1']
                self.assertEqual([(a['task_id'], a['reason']) for a in g01['unpaid_attempts']], [('task1', 'task failed (refunded)')])

    def test_only_unpaid_attempts_are_requeued(self):
        import apimart_wan as sd
        self.assertTrue(sd.reopen_unpaid({'status': 'failed', 'task_id': 't1', 'cost_usd': 0}, 'refunded'))
        self.assertFalse(sd.reopen_unpaid({'status': 'failed', 'task_id': 't1', 'cost_usd': 0.4}, 'charged'))      # paid: never twice
        self.assertFalse(sd.reopen_unpaid({'status': 'downloaded', 'task_id': 't1'}, 'done'))
        self.assertFalse(sd.reopen_unpaid({'status': 'known_failed', 'http_status': 402}, 'balance'))           # needs the user
        c = {'status': 'failed'}
        for _ in range(sd.MAX_FREE_RESUBMITS): self.assertTrue(sd.reopen_unpaid(c, 'x')); c['status'] = 'failed'
        self.assertFalse(sd.reopen_unpaid(c, 'x')); self.assertEqual(len(c['unpaid_attempts']), sd.MAX_FREE_RESUBMITS)

    def test_result_url_reads_list_or_string(self):
        import apimart_wan as sd
        self.assertEqual(sd.result_url({'result': {'videos': [{'url': ['https://x/y.mp4']}]}}), 'https://x/y.mp4')
        self.assertEqual(sd.result_url({'result': {'videos': [{'url': 'https://x/z.mp4'}]}}), 'https://x/z.mp4')
        self.assertIsNone(sd.result_url({'result': {}}))


class SourceReference(unittest.TestCase):
    """Source reference rule: cut by time from the ORIGINAL source, encode once, 720 short edge in, audio copied in sync."""
    def setUp(self):
        import tempfile, media_pipeline as media
        self.media = media
        self.temp = tempfile.TemporaryDirectory(); root = Path(self.temp.name)
        self.src = root/'source.mp4'; self.run = root/'RUN'; self.run.mkdir()
        # 23.976 fps 360x640 source, one keyframe only (the case where an output-side -t shifted copied audio),
        # silent until 2.5 s, then a tone.
        media.ff(['-y', '-f', 'lavfi', '-i', 'testsrc2=s=360x640:r=24000/1001:d=4', '-f', 'lavfi', '-i',
                  "aevalsrc='if(gte(t,2.5),0.5*sin(2*PI*440*t),0)':s=48000:d=4", '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '999',
                  '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '64k', '-shortest', str(self.src)])
        self.state = {'source': str(self.src), 'stages': {'prepare': {'cfr_master': {'path': '/nonexistent/master-cfr.mp4'}}}}
        self.seg = {'id': 'G01', 'in_frame': 48, 'out_frame': 72}   # 2.0–3.0 s on the 24 fps grid

    def tearDown(self): self.temp.cleanup()

    def test_reviewed_reference_edge_is_source_bound_and_cache_tracks_removal(self):
        state = {**self.state, 'source_sha256': swap.sha(self.src)}
        policy = self.run/'plan/reference_edge_overrides.json'
        item = {'source_frames': [48, 72], 'first': 0, 'end': 23,
                'reviewed': True, 'basis': 'Native final transition frame reviewed.'}
        swap.save(policy, {'source_sha256': state['source_sha256'], 'segments': {'G01': item}})
        out = swap.reference_clip_hq(self.run, state, self.seg, 24, upload=True)
        meta = swap.read(out.with_suffix('.json'))
        self.assertEqual(meta['edge_frames_kept'], [0, 23])
        self.assertEqual(meta['reviewed_reference_edge'], item)
        self.assertAlmostEqual(float(self.media.probe(str(out))['video_duration']), 4.0, delta=.01)
        bad = {**state, 'source_sha256': 'different-source'}
        with self.assertRaises(SystemExit):
            swap.reference_edge_policy(self.run, bad, self.seg)
        policy.unlink()
        swap.reference_clip_hq(self.run, state, self.seg, 24, upload=True)
        self.assertNotIn('reviewed_reference_edge', swap.read(out.with_suffix('.json')))

    def test_reviewed_caption_policy_and_source_words_invalidate_only_clean_cache(self):
        import caption_lama, shutil
        from unittest import mock
        policy = self.run/'plan/reference_caption_policy.json'
        policy.parent.mkdir(parents=True)

        def fake_clean(plain, out, *args, **kwargs):
            shutil.copyfile(plain, out)
            return {'frames': 24, 'frames_left': 0}

        with mock.patch.object(swap, 'watermark_boxes', return_value={}), \
             mock.patch.object(caption_lama, 'clean', side_effect=fake_clean) as cleaner:
            swap.reference_clip_hq(self.run, self.state, self.seg, 24, clean=True)
            swap.reference_clip_hq(self.run, self.state, self.seg, 24, clean=True)
            self.assertEqual(cleaner.call_count, 1)
            plain = self.run/'clips/G01.hq.mp4'
            plain_sha = swap.sha(plain)
            swap.save(policy, {'G01': {'spoken_filter': True, 'min_text_height_px': 36}})
            swap.reference_clip_hq(self.run, self.state, self.seg, 24, clean=True)
            self.assertEqual(cleaner.call_count, 2)
            self.seg['speech'] = [{'text': 'Hello.'}]
            swap.reference_clip_hq(self.run, self.state, self.seg, 24, clean=True)
            self.assertEqual(cleaner.call_count, 3)
            policy.unlink()
            swap.reference_clip_hq(self.run, self.state, self.seg, 24, clean=True)
            self.assertEqual(cleaner.call_count, 4)
            self.assertEqual(swap.sha(plain), plain_sha)

    def test_cut_from_source_once_at_720_with_audio_in_sync(self):
        import numpy as np, subprocess
        out = swap.reference_clip_hq(self.run, self.state, self.seg, 24)
        meta = swap.read(out.with_suffix('.json'))
        self.assertEqual(meta['source'], str(self.src)); self.assertEqual(meta['encodes'], 1); self.assertEqual(meta['crf'], swap.REF_CRF)
        self.assertEqual(meta['maxrate'], swap.ref_maxrate(1.0))
        self.assertEqual(meta['size'], [720, 1280]); self.assertEqual(meta['audio'], 'copied')
        info = self.media.probe(str(out))
        self.assertAlmostEqual(float(info['video_duration']), 1.0, delta=0.08)
        pcm = subprocess.run([self.media.binary('ffmpeg'), '-v', 'error', '-i', str(out), '-vn', '-ac', '1', '-ar', '8000', '-f', 's16le', '-'],
                             capture_output=True, check=True).stdout
        x = np.abs(np.frombuffer(pcm, np.int16).astype(float))
        self.assertLess(len(x) / 8000, 1.1)                      # audio is not longer than the window
        onset = int(np.argmax(x > 3000)) / 8000
        self.assertAlmostEqual(onset, 0.5, delta=0.05)          # tone at 2.5 s in the source lands at 0.5 s

    def test_reference_bitrate_is_capped_so_apimart_can_fetch_it_within_60s(self):
        # heavy-noise source: unconstrained CRF 12 at 720P would be far above the cap
        noisy = Path(self.temp.name)/'noisy.mp4'
        self.media.ff(['-y', '-f', 'lavfi', '-i', 'testsrc2=s=360x640:r=30:d=4,noise=alls=60:allf=t', '-f', 'lavfi', '-i', 'sine=d=4',
                       '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '10', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(noisy)])
        out = swap.reference_clip_hq(self.run, {**self.state, 'source': str(noisy)}, {'id': 'G02', 'in_frame': 0, 'out_frame': 90}, 30)
        info = self.media.probe(str(out)); secs = float(info['video_duration'])
        video_kbit = float(info['video'].get('bit_rate') or 0) / 1000 * secs
        # VBV bound: at most maxrate for the duration plus one buffer (uncapped this clip is ~115 Mb/s)
        self.assertLess(video_kbit, 1500 * secs + 3000, f'reference video {video_kbit:.0f} kbit over {secs:.1f} s')

    def test_muted_reference_is_rebuilt_when_its_clip_is_newer(self):
        import os, time
        clip = swap.reference_clip_hq(self.run, self.state, self.seg, 24)
        first = swap.muted_reference(clip, [(0.1, 0.4)]); old = first.read_bytes()
        stale = time.time() - 3600; os.utime(first, (stale, stale))      # muted file older than a re-encoded clip
        again = swap.muted_reference(clip, [(0.1, 0.4)])
        self.assertEqual(again, first); self.assertGreaterEqual(again.stat().st_mtime, clip.stat().st_mtime)
        mtime = again.stat().st_mtime
        self.assertEqual(swap.muted_reference(clip, [(0.1, 0.4)]).stat().st_mtime, mtime)   # fresh one is reused
        self.assertTrue(old)

    def test_long_references_get_a_lower_cap_so_the_file_stays_small(self):
        self.assertEqual(swap.ref_maxrate(8), '1500k')
        self.assertEqual(swap.ref_maxrate(14.9), '925k')
        self.assertLessEqual((int(swap.ref_maxrate(14.9)[:-1]) + swap.REF_AUDIO_KBPS) * 1000 * 14.9 / 8, swap.REF_MAX_BYTES)
        self.assertEqual(swap.ref_maxrate(60), f'{swap.REF_MIN_KBPS}k')

    def test_scale_rule_upscales_to_720_and_never_downscales(self):
        self.assertEqual(swap.reference_scale(360, 640), ('scale=720:1280:flags=lanczos', (720, 1280)))
        self.assertEqual(swap.reference_scale(640, 360), ('scale=1280:720:flags=lanczos', (1280, 720)))
        self.assertEqual(swap.reference_scale(1080, 1920), (None, (1080, 1920)))

    def test_output_is_fixed_to_720p(self):
        self.assertEqual(swap.seedance.RESOLUTION, '720p'); self.assertEqual(swap.REF_SHORT_EDGE, 720)


if __name__ == '__main__':
    unittest.main()


class Seedance20Model(unittest.TestCase):
    """User 2026-11-20: "我们先采用APIMART中的seedance-2.0 … 画幅固定9:16/根据目标段时长匹配/分辨率固定720P"; all three are
    request parameters, never prompt text."""
    def test_seedance_20_parameters(self):
        import apimart_wan as sd
        self.assertEqual(sd.SETTINGS, {'model': 'seedance-2.0', 'resolution': '720p', 'size': '9:16'})
        self.assertEqual((sd.OUTPUT_MIN, sd.OUTPUT_MAX), (4, 15))
        body = sd.payload({'job_id': 'G01-a1', 'model': sd.MODEL, 'output_seconds': 7}, 'p', ['https://i'], ['https://v'])
        self.assertEqual((body['duration'], body['size'], body['resolution']), (7, '9:16', '720p'))
        self.assertTrue({'seedance-2.5', 'seedance-2-5-flash'} <= sd.LEGACY_MODELS)   # older ledgers are still read
        with self.assertRaisesRegex(sd.AdapterError, 'legacy'):
            sd.payload({'job_id': 'G01-a1', 'model': 'seedance-2-5-flash', 'output_seconds': 7}, 'p', [], ['https://v'])
        self.assertEqual(sd.settings_for({'model': 'seedance-2-5-flash'})['resolution'], '480p')


class UserApprovedRegen(unittest.TestCase):
    """User 2026-11-19: "把失败的镜头重新生成一遍" -- a second paid generation only on the user's request."""
    def test_numbering_skips_splices(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            run = Path(d); (run/'generation/jobs').mkdir(parents=True); (run/'jobs').mkdir()
            swap.save(run/'generation/jobs/G07-a1.json', {'provider': 'apimart'})
            swap.save(run/'generation/jobs/G07-a2.json', {'provider': 'local_splice'})
            self.assertEqual(swap.regen_attempt(run, 'G07'), 3)
            self.assertEqual(swap.regen_previous(run, 'G07', 'G07-a3'), 'G07-a1')
            swap.save(run/'generation/jobs/G06-a1.json', {'provider': 'apimart'})
            self.assertEqual(swap.regen_attempt(run, 'G06'), 2)
            with self.assertRaises(SystemExit):
                swap.regen_attempt(run, 'G09')                      # never generated: plain a1
            swap.save(run/'generation/jobs/G06-a2.json', {'provider': 'apimart', 'job': {'user_approved_regen': {'quote': 'x'}},
                                                          'candidates': {'c1': {'status': 'known_failed'}}})
            self.assertEqual(swap.regen_attempt(run, 'G06'), 2)         # resumed, not a new paid job
            swap.save(run/'generation/jobs/G06-a2.json', {'provider': 'apimart', 'job': {'user_approved_regen': {'quote': 'x'}},
                                                          'candidates': {'c1': {'status': 'downloaded'}}})
            self.assertEqual(swap.regen_attempt(run, 'G06'), 3)         # a further request after a result

    def test_later_job_numbers_need_the_users_words(self):
        import apimart_wan as sd
        from unittest import mock
        job = {'job_id': 'G06-a2', 'run_dir': '/tmp', 'model': sd.MODEL, 'prompt_file': '/tmp/p.txt', 'generation_mode': sd.MODE,
               'output_seconds': 6, 'references': [{'kind': 'video', 'path': '/tmp/v.mp4'}]}
        with mock.patch.object(sd.media, 'probe', return_value={'video': {'width': 720, 'height': 1280}, 'video_duration': 6.0}):
            with self.assertRaisesRegex(sd.AdapterError, 'user'):
                sd.validate(job)
            try:
                sd.validate({**job, 'user_approved_regen': {'quote': '把失败的镜头重新生成一遍'}})
            except sd.AdapterError as e:
                self.assertNotIn('a1 job ID', str(e))


class ReplacementSentences(unittest.TestCase):
    """User 2026-11-19: no erasing; every competitor left in the reference is replaced in the prompt, on its shot line."""
    BODY = ('镜头1（0–3 秒）：玻璃水杯特写。将@视频1中水杯后面的琥珀色滴管瓶更换为@图片1的青绿色纸盒。\n'
            '镜头2（3–6 秒）：男人坐在桌边说话。\n'
            '镜头3（6–8 秒）：将@视频1中他手里的盒子更换为@图片1的青绿色纸盒。')

    def test_missing_shot_is_reported(self):
        items = [{'t': 1.0, 'kind': 'container'}, {'t': 4.5, 'kind': 'container'}, {'t': 7.0, 'kind': 'container'}]
        self.assertEqual([x['t'] for x in swap.unreplaced(self.BODY, items)], [4.5])

    def test_erase_is_refused(self):
        import argparse
        a = argparse.Namespace(erase=['G01:0,0,1,1@0-1'], mute=None, attempt=1)
        seg = {'id': 'G01', 'rewrites': {'dialogue': [], 'vo': []}, 'duration_s': 4}
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            run = Path(d); (run/'prompts').mkdir()
            (run/'prompts'/'G01-a1.txt').write_text('镜头1（0–4 秒）：男人说话。')
            with self.assertRaisesRegex(SystemExit, '已停用'):
                swap.prepare_jobs(a, run, {}, {'fps': 30}, [seg], [run/'jobs'/'G01-a1.json'])


class ReferenceCacheKey(unittest.TestCase):
    def test_plain_request_does_not_take_an_erased_cache(self):
        import inspect
        src = inspect.getsource(swap.reference_clip_hq)
        self.assertIn("m.get('erase') == key.get('erase')", src)
        self.assertIn("output_sha256", src)


class SpliceRetime(unittest.TestCase):
    """User 2026-11-19: shots under 4 s are generated from a reference slowed to 4 s, then fitted back to the shot's frames."""
    def test_retimed_piece_fills_its_frames(self):
        import tempfile, subprocess, json as _j
        ff = swap.media.binary('ffmpeg')
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)/'run'; (run/'plan').mkdir(parents=True); (run/'generation/jobs').mkdir(parents=True); (run/'generation/results').mkdir(parents=True)
            swap.save(run/'state.json', {'stages': {}}); swap.save(run/'plan/segments.json', {'segments': [{'id': 'G01', 'in_frame': 0, 'out_frame': 48}]})
            for name, sec, col in (('G01-a1-c1', 2, 'red'), ('G01-s1-c1', 4, 'blue')):
                out = run/'generation/results'/f'{name}.mp4'
                subprocess.run([ff, '-v', 'error', '-y', '-f', 'lavfi', '-i', f'color=c={col}:s=64x64:r=24:d={sec}', '-f', 'lavfi', '-i', f'sine=d={sec}',
                                '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(out)], check=True)
                swap.save(run/'generation/jobs'/f'{name[:-3]}.json', {'provider': 'apimart', 'candidates': {'c1': {'status': 'downloaded', 'output_path': str(out), 'output_sha256': swap.sha(out)}}})
            import argparse
            a = argparse.Namespace(run=str(run), segment='G01', out_attempt='a2', picture=['G01-a1-c1:0-11', 'G01-s1-c1:0-95@12', 'G01-a1-c1:24-47'],
                                   audio=None, audio_window=None, mute_window=None, hold_last=None, note='t', force=False)
            with self.assertRaises(SystemExit): swap.cmd_splice(a)          # a retimed piece needs --audio
            a.audio = 'G01-a1-c1'
            with self.assertRaises(SystemExit): swap.cmd_splice(a)          # a1's sound under the retimed shot: say no mouth speaks there
            a.no_mouth = ['0.5-1.0']
            swap.cmd_splice(a)
            info = swap.media.probe(str(run/'generation/results/G01-a2-c1.mp4'), include_frames=True)
            self.assertEqual(info['frame_count'], 48)


class SpliceTimingForCaptions(unittest.TestCase):
    def test_in_place_shot_replacements_keep_timing(self):
        base = lambda a, b: {'source': 'G05-a1-c1', 'frames_inclusive': [a, b]}
        shot = lambda: {'source': 'G05-S167-c1', 'frames_inclusive': [0, 95], 'fit': 30}      # slowed shot fitted to its 30 frames
        self.assertTrue(swap.splice_keeps_timing([base(0, 185), shot(), base(216, 336)], 'G05'))
        ripple = {'source': 'G05-S167-c1', 'frames_inclusive': [0, 59]}                       # real-speed redo at its own 60 frames
        self.assertFalse(swap.splice_keeps_timing([base(0, 185), ripple, base(216, 336)], 'G05'))  # what follows moved (2026-11-22)
        self.assertTrue(swap.splice_keeps_timing([{'source': 'G14-p2-c1', 'frames_inclusive': [0, 120]}], 'G14'))
        self.assertFalse(swap.splice_keeps_timing([{'source': 'G19-a1-c1', 'frames_inclusive': [65, 96]}], 'G19'))   # moved
        self.assertFalse(swap.splice_keeps_timing([base(0, 100), base(150, 200)], 'G05'))                              # frames dropped
        self.assertFalse(swap.splice_keeps_timing([{'source': '/x/master-clean.mp4', 'frames_inclusive': [9582, 9906]}, shot()], 'G05'))


class MultiformReferenceImages(unittest.TestCase):
    def test_carton_and_both_packet_states_fit_official_image_limit(self):
        import apimart_wan as sd
        from unittest import mock
        probe = {'video': {'width': 720, 'height': 1280}, 'video_duration': 10.0}
        job = {'job_id': 'G06-a1', 'run_dir': '/tmp', 'model': sd.MODEL, 'prompt_file': '/tmp/p.txt',
               'generation_mode': 'reference', 'output_seconds': 10, 'resolution': '720p', 'aspect_ratio': '9:16',
               'references': [{'path': '/tmp/ref.mp4', 'kind': 'video'}]}
        with mock.patch.object(sd.media, 'probe', return_value=probe):
            for count in (4, 9):
                refs = job['references'] + [{'path': f'/tmp/{i}.jpg', 'kind': 'image'} for i in range(count)]
                sd.validate({**job, 'references': refs})
            with self.assertRaisesRegex(sd.AdapterError, 'at most nine'):
                sd.validate({**job, 'references': job['references'] + [{'path': '/tmp/i.jpg', 'kind': 'image'}] * 10})
