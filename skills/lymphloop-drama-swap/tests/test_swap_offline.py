"""Offline walk of swap.py: init → prepare → watermark none → shots → merge(overrides) → plan → reference → fake candidate
→ select → recut.

No Gemini or APIMART calls (lock levels come from lock_overrides.json). The generated candidate is cut from the source,
which is enough to prove lock blocks, prompt stubs, reference preparation and ripple rendering.
"""
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL/'scripts'))
import media_pipeline as media  # noqa: E402
import swap  # noqa: E402

PRO_SAMPLE = """---
### ⏱️ Timecode: [00:00:03:00] - [00:00:05:24]
*   **场景简述**: 男子举起棕色滴管瓶
*   ** VO冲突 (旁白)**
    - **原词**: None
    - **改写为**: None
*   ** Dialogue冲突 (对白/需重绘口型)**
    - **原词**: Just a few drops of this tincture every morning.
    - **改写为**: Just one packet of this blend every morning.
*   ** Visual冲突 (画面竞品露出)**
    - **冲突元素**: 棕色滴管瓶及捏滴管动作
---
### ⏱️ Timecode: [00:00:06:00] - [00:00:07:10]
*   **场景简述**: 桌面静物
*   ** VO冲突 (旁白)**
    - **原词**: The dropper bottle sits by the window.
    - **改写为**: The teal carton sits by the window.
*   ** Dialogue冲突 (对白/需重绘口型)**
    - **原词**: None
    - **改写为**: None
*   ** Visual冲突 (画面竞品露出)**
    - **冲突元素**: None
---
"""


def run_swap(*args):
    result = subprocess.run([sys.executable, str(SKILL/'scripts/swap.py'), *map(str, args)], capture_output=True, text=True,
                            env={**os.environ, 'LYMPHLOOP_NO_FLASH': '1'})
    if result.returncode: raise AssertionError(result.stderr or result.stdout)
    text = result.stdout.strip()
    return json.loads(text[text.rfind('\n{') + 1:] if '\n{' in text else text)


class ParseTests(unittest.TestCase):
    def test_pro_markdown_nodes(self):
        nodes = swap.parse_pro_markdown(PRO_SAMPLE, 25)
        self.assertEqual(len(nodes), 2)
        self.assertEqual((nodes[0]['in_frame'], nodes[0]['out_frame']), (75, 150))
        self.assertEqual(nodes[0]['dialogue']['rewrite'], 'Just one packet of this blend every morning.')
        self.assertEqual(nodes[0]['visual'], '棕色滴管瓶及捏滴管动作');self.assertIsNone(nodes[0]['vo'])
        self.assertIsNone(nodes[1]['dialogue']);self.assertIsNone(nodes[1]['visual'])
        self.assertEqual(nodes[1]['vo']['original'], 'The dropper bottle sits by the window.')

    def test_only_the_three_registered_references_go_to_a_model(self):
        profile = swap.read(swap.PRODUCT)
        photos = {'handheld_photo': 'assets/product/stick-handheld-scale.jpg', 'opened_packet_hand': 'assets/product/opened-packet-handheld.jpg',
                  'carton_views': 'assets/product/carton-views.jpg', 'carton_hand': 'assets/product/carton-hand-scale.jpg',   # user 2026-11-20: two carton images only
                  'phone_page': 'assets/product/landing-page-phone.jpg', 'desktop_page': 'assets/product/landing-page-desktop.jpg'}   # user 2026-11-22: our product page for screens
        self.assertEqual(profile['reference_views'], photos)
        self.assertEqual(sorted(profile['active_model_reference_files']), sorted(photos.values()))
        for view, rel in photos.items():
            self.assertTrue((SKILL/swap.resolve_view(profile, view)).is_file())
        for gone in ['stick_photo', 'carton_sheet', 'carton_front', 'carton_top', 'carton_back', 'carton_front_hand', 'carton_angle_hand', 'state:tear_seal', 'state:hand_scale', 'lymphoria_anchor', 'competitor']:
            with self.assertRaises(SystemExit): swap.resolve_view(profile, gone)
        # Every file in the manifest exists with the recorded bytes, and only the three references may go to a model.
        for entry in profile['asset_manifest']:
            self.assertEqual(swap.sha(SKILL/entry['file']), entry['sha256'], entry['file'])
        allowed = sorted(e['file'] for e in profile['asset_manifest'] if e.get('model_input_allowed'))
        self.assertEqual(allowed, sorted(photos.values()))
        images = {p.relative_to(SKILL).as_posix() for p in (SKILL/'assets').rglob('*') if p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}}
        sources = {e['file'] for e in profile['asset_manifest'] if e.get('role') == 'agent_only_source'}
        self.assertTrue(all(f.startswith('assets/product/carton-sources/') for f in sources))   # 11-20: carton sources moved to the backup
        self.assertEqual({i for i in images if not i.startswith('assets/competitors/')}, set(photos.values()) | sources)
        # the registered opaque photos pass the reference gate
        import google_api as api
        api.validate_references({'kind': 'analyze', 'images': [{**api.file_description(SKILL/p), 'role': 'product_reference'} for p in photos.values()]})

    def test_retired_routes_are_gone(self):
        for name in ('x100_client', 'compose_prompt', 'apimart_seedance', 'generation_attempts', 'wm_track'):
            self.assertFalse((SKILL/'scripts'/(name + '.py')).exists(), name)
        for attr in ('requested_seconds', 'final_prompt', 'reference_clip', 'retime', 'H3_MIN'):
            self.assertFalse(hasattr(swap, attr), attr)


class OfflineWalk(unittest.TestCase):
    def test_init_to_recut(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); src = root/'source.mp4'; run = root/'run'; out = root/'out'
            # Two visibly different shots (red 3 s, blue 3 s) at 25 fps with a tone track.
            media.ff(['-y', '-f', 'lavfi', '-i', 'color=c=red:s=192x320:r=25:d=3', '-f', 'lavfi', '-i', 'color=c=blue:s=192x320:r=25:d=3',
                      '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=6',
                      '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]', '-map', '[v]', '-map', '2:a',
                      '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(src)])
            state = run_swap('init', '--video', src, '--run', run, '--out', out)
            self.assertEqual(state['probe']['frame_count'], 150)
            run_swap('prepare', '--run', run)
            run_swap('watermark', 'none', '--run', run, '--note', 'synthetic clip has no overlay')
            self.assertTrue((run/'master-clean.mp4').is_file())
            shots = run_swap('shots', '--run', run)
            self.assertEqual(shots['shots'], 2)
            data = json.loads((run/'shots/shots.json').read_text())
            self.assertEqual([s['in_frame'] for s in data['shots']], [0, 75])
            # Replace the first shot so the position of the untouched second shot can be checked.
            swap.save(run/'detect/overrides.json', {'S001': {'decision': 'replace', 'kinds': ['visual'], 'note': 'fixture: red shot carries the bottle'}})
            self.assertEqual(run_swap('merge', '--run', run)['replace'], ['S001'])
            swap.save(run/'plan/lock_overrides.json', {'S002': 'paragraph'})
            planned = run_swap('plan', '--run', run, '--no-speech')
            seg = planned['segments'][0]
            self.assertEqual((seg['id'], seg['shot_ids'], seg['output_seconds']), ('G01', ['S001'], 4))
            stub = (run/'prompts/G01-a1.txt').read_text()
            self.assertIn('【参考素材职责】', stub); self.assertNotIn('请注意这不是视频编辑', stub.split('# design:')[1])   # five-section (11-20)
            self.assertIn('阶段一：', stub); self.assertIn('画面不显示字幕', stub)
            with self.assertRaises(AssertionError):
                run_swap('generate', '--run', run, '--segment', 'G01', '--prepare-only')
            st = swap.read(run/'state.json')
            full = swap.read(run/'plan/segments.json')
            hq = swap.reference_clip_hq(run, st, full['segments'][0], full['fps'])
            info = media.probe(str(hq))
            self.assertEqual(min(info['video']['width'], info['video']['height']), 720)
            self.assertTrue(info['audio'])
            self.assertAlmostEqual(float(info['video_duration']), 3.0, delta=0.1)

            scan_path = run/'watermark/scan.json'
            scan = swap.read(scan_path)
            scan['detections'] = [{'frame': 85, 'classification': 'floating_overlay',
                                   'bbox': [20, 250, 150, 20], 'text': 'LYMPHORIA'}]
            swap.save(scan_path, scan)
            for seconds in (4, 2):
                with self.subTest(seconds=seconds):
                    fake = run/f'generation/results/candidate-{seconds}.mp4'
                    fake.parent.mkdir(parents=True, exist_ok=True)
                    media.ff(['-y', '-f', 'lavfi', '-i', f'color=c=green:s=192x320:r=24:d={seconds}',
                              '-f', 'lavfi', '-i', f'sine=frequency=880:sample_rate=48000:duration={seconds}',
                              '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(fake)])
                    swap.save(run/'generation/jobs/G01-a1.json', {'job': {'job_id': 'G01-a1', 'model': 'seedance-2.5'},
                              'candidates': {'c1': {'status': 'downloaded', 'output_path': str(fake), 'output_sha256': swap.sha(fake)}}})
                    full['segments'][0]['speech'] = [{'kind': 'on_screen_dialogue'}] if seconds == 4 else []
                    # a rewritten line: the selected clip keeps its own generated sound (a segment without one takes the
                    # source sound, see test_any_competitor.SoundUnderTheGeneratedPicture)
                    full['segments'][0]['rewrites'] = {'dialogue': [{'original': 'drops', 'rewrite': 'packets'}], 'vo': []}
                    swap.save(run/'plan/segments.json', full)
                    if seconds == 4:
                        with self.assertRaisesRegex(AssertionError, 'on-screen dialogue'):
                            run_swap('select', '--run', run, '--job', 'G01-a1', '--candidate', 'c1', '--audio-only')
                    # the window is given: left out, a segment shorter than the API minimum is cut at its own length (the rest is reference padding)
                    sel = run_swap('select', '--run', run, '--job', 'G01-a1', '--candidate', 'c1', '--clip-out', str(seconds), '--note', 'fixture',
                                   '--keep-frozen-tail')          # the fixture is one still colour: nothing to trim here
                    self.assertEqual((sel['clip_in'], sel['clip_out']), (0.0, seconds))
                    if seconds < 3:
                        # 2026-11-14: a selection shorter than its source segment drops original content; recut refuses
                        # it unless the user approved that cut
                        with self.assertRaisesRegex(AssertionError, '不许删掉原片内容'):
                            run_swap('recut', '--run', run)
                        recut = run_swap('recut', '--run', run, '--user-approved-cut', 'G01')
                    else:
                        recut = run_swap('recut', '--run', run)
                    master = Path(recut['master']['path'])
                    final = media.probe(str(master), include_frames=True)
                    self.assertEqual(final['frame_count'], 25 * (seconds + 3))
                    timeline = swap.read(Path(recut['timeline']))
                    replacement, following = timeline['segments']
                    self.assertEqual((replacement['picture_id'], replacement['frames']), ('G01', seconds * 25))
                    self.assertEqual((following['source_in'], following['out_in']), (75, seconds * 25))
                    self.assertTrue(all(s['picture_speed'] == s['source_audio_speed'] == 1 for s in timeline['segments']))
                    audio, = timeline['audio_replacements']
                    self.assertEqual((audio['path'], audio['clip_in'], audio['clip_out']), (str(fake.resolve()), 0, seconds))
                    self.assertEqual((audio['out_in'], audio['out_out']), (0, seconds * 25))

                    import numpy as np
                    for start, hz in ((0.25, 880), (seconds + 0.25, 440)):
                        samples = subprocess.check_output([media.binary('ffmpeg'), '-v', 'error', '-ss', str(start),
                            '-i', str(master), '-t', '0.4', '-vn', '-ar', '8000', '-ac', '1', '-f', 'f32le', '-'])
                        x = np.frombuffer(samples, dtype='float32')
                        dominant = np.fft.rfftfreq(len(x), 1/8000)[abs(np.fft.rfft(x)).argmax()]
                        self.assertAlmostEqual(dominant, hz, delta=10)
                    branded = run_swap('brand', '--run', run)
                    self.assertTrue(branded['verification']['ok'])
                    stamps = swap.read(run/'watermark/brand_plan.json')['stamps']
                    self.assertEqual(list(stamps), [str(seconds * 25 + 10)])
            status = run_swap('status', '--run', run)
            self.assertIn('recut', status['stages'])


class InShotCutWalk(unittest.TestCase):
    """Keep original pause hints separate from the reviewed candidate cuts."""
    def test_long_shot_uses_a_reviewed_breath_trim(self):
        from unittest import mock
        import speech_map
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); src = root/'source.mp4'; run = root/'run'; out = root/'out'
            media.ff(['-y', '-f', 'lavfi', '-i', 'testsrc2=s=192x320:r=25:d=20', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=20',
                      '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(src)])
            run_swap('init', '--video', src, '--run', run, '--out', out); run_swap('prepare', '--run', run)
            run_swap('watermark', 'none', '--run', run, '--note', 'synthetic')
            run_swap('shots', '--run', run)
            self.assertEqual(len(json.loads((run/'shots/shots.json').read_text())['shots']), 1)
            swap.save(run/'detect/overrides.json', {'S001': {'decision': 'replace', 'kinds': ['dialogue'], 'note': 'fixture: one long talking shot with the bottle'}})
            run_swap('merge', '--run', run)
            sentences = [{'start': 0.5, 'end': 8.0, 'speaker': 'woman', 'kind': 'on_screen_dialogue', 'text': 'First sentence.'},
                         {'start': 9.0, 'end': 19.5, 'speaker': 'woman', 'kind': 'on_screen_dialogue', 'text': 'Second sentence.'}]
            args = type('A', (), {'run': str(run), 'no_speech': False, 'dry_run': False, 'runs_only': False, 'no_scenes': False})()
            with mock.patch.object(speech_map, 'transcribe', return_value=sentences), mock.patch.object(speech_map, 'semantic_breaks', return_value={}):
                swap.cmd_plan(args)
            segs = swap.read(run/'plan/segments.json')['segments']
            self.assertEqual([s['shot_ids'] for s in segs], [['S001.1'], ['S001.2']])
            self.assertEqual(segs[1]['in_frame'], 212)                                   # 8.5 s pause midpoint on the 25 fps grid
            self.assertTrue(all(s['duration_s'] < 15 for s in segs))
            self.assertAlmostEqual(segs[0]['trim']['tail_s'], 8.48 - 8.15, places=3)      # keep 0.15 s after the sentence
            self.assertAlmostEqual(segs[1]['trim']['head_s'], 8.85 - 8.48, places=3)      # keep 0.15 s before the next one
            self.assertIn('长镜头中间', (run/'prompts/G02-a1.txt').read_text())
            master = swap.working_master(swap.read(run/'state.json'))
            for seg in segs:
                gid = seg['id']; secs = seg['duration_s']; made = swap.output_seconds(secs)     # generated at whole seconds, used at its own length
                fake = run/f'generation/results/{gid}-a1-c1.mp4'; fake.parent.mkdir(parents=True, exist_ok=True)
                media.ff(['-y', '-ss', str(seg['in_frame']/25), '-t', str(made), '-i', str(master), '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(fake)])
                swap.save(run/f'generation/jobs/{gid}-a1.json', {'job': {'job_id': f'{gid}-a1', 'model': 'apimart-wan3.0'},
                                                         'candidates': {'c1': {'status': 'downloaded', 'output_path': str(fake), 'output_sha256': swap.sha(fake)}}})
                sel = run_swap('select', '--run', run, '--job', f'{gid}-a1', '--candidate', 'c1', '--note', 'fixture')
                self.assertEqual(sel['clip_in'], 0)
                self.assertAlmostEqual(sel['clip_out'], secs, places=3)
                run_swap('select', '--run', run, '--job', f'{gid}-a1', '--candidate', 'c1',
                         '--clip-in', seg.get('trim', {}).get('head_s', 0.0),
                         '--clip-out', secs - seg.get('trim', {}).get('tail_s', 0.0),
                         '--note', 'reviewed fixture pause')
            recut = run_swap('recut', '--run', run)
            final = media.probe(recut['master']['path'], include_frames=True)
            expect = 20 - segs[0]['trim']['tail_s'] - segs[1]['trim']['head_s']
            self.assertAlmostEqual(final['frame_count'] / 25, expect, delta=0.12)


if __name__ == '__main__': unittest.main()
