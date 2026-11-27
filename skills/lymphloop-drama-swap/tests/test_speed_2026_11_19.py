"""User 2026-11-19: a 10-minute film took 4 h from video to delivery. Independent stages run at once, scans are sparse
and parallel, and final-screen only looks at what was changed."""
import json, subprocess, sys, tempfile, threading, unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import swap  # noqa: E402
import watermark_brand as wb  # noqa: E402
import scenes  # noqa: E402
import caption_burn as cb  # noqa: E402


class StateMerge(unittest.TestCase):
    def test_parallel_commits_keep_every_stage(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d)
            swap.save(run / 'state.json', {'source': 'x', 'stages': {'prepare': {'done_at': 't'}}})
            states = [swap.load(run)[1] for _ in range(8)]          # every worker read the state before anyone wrote
            threads = [threading.Thread(target=swap.commit, args=(run, st, f'stage{i}'), kwargs={'n': i}) for i, st in enumerate(states)]
            for t in threads: t.start()
            for t in threads: t.join()
            stages = swap.read(run / 'state.json')['stages']
            self.assertEqual(sorted(stages), sorted(['prepare'] + [f'stage{i}' for i in range(8)]))
            self.assertEqual(stages['stage3']['n'], 3)

    def test_caller_sees_merged_state(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d); swap.save(run / 'state.json', {'stages': {}})
            a, b = swap.load(run)[1], swap.load(run)[1]
            swap.commit(run, a, 'screen'); swap.commit(run, b, 'detect_pro')
            self.assertIn('screen', b['stages'])


class SharedStageLocks(unittest.TestCase):
    def ns(self, **kw):
        import argparse
        return argparse.Namespace(**{'run': '', 'lock_wait': 0, 'action': None, 'refs_only': False, **kw})

    def test_which_commands_share(self):
        self.assertEqual(swap.shared_stage(self.ns(cmd='screen')), 'screen')
        self.assertEqual(swap.shared_stage(self.ns(cmd='detect-pro')), 'detect-pro')
        self.assertEqual(swap.shared_stage(self.ns(cmd='watermark', action='scan')), 'watermark-scan')
        self.assertIsNone(swap.shared_stage(self.ns(cmd='watermark', action='apply')))
        self.assertEqual(swap.shared_stage(self.ns(cmd='generate', refs_only=True)), 'refs')
        self.assertIsNone(swap.shared_stage(self.ns(cmd='generate')))
        self.assertIsNone(swap.shared_stage(self.ns(cmd='merge')))

    def test_shared_holders_coexist_and_block_a_writer(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / '.run.lock'
            with swap.flock(lock, True, 0, 'run'), swap.flock(lock, True, 0, 'run'):
                with self.assertRaises(swap.api.AdapterError):
                    with swap.api.lock(lock, wait=0, what='run'): pass
            with swap.api.lock(lock, wait=0, what='run'): pass          # free again


class SparseScanRanges(unittest.TestCase):
    def test_split_and_merge(self):
        self.assertEqual(wb.merge_ranges([(5, 20), (15, 30), (50, 60), (-3, 2)], 55), [(0, 2), (5, 30), (50, 55)])
        parts = wb.split_ranges([(0, 100)], 4)
        self.assertEqual(parts[0][0], 0); self.assertEqual(parts[-1][1], 100); self.assertEqual(len(parts), 4)
        self.assertEqual(sum(b - a for a, b in wb.split_ranges([(0, 100), (200, 250)], 4)), 150)

    def test_dense_pass_only_around_sparse_hits(self):
        calls = []

        def fake_run(cmd, check, capture_output):
            out, step, rng = Path(cmd[2]), int(cmd[6]), cmd[7]
            a, b = map(int, rng.split('-')); calls.append((step, a, b))
            hits = [f for f in range(a, b) if (f - a) % step == 0 and 400 <= f < 430]
            out.write_text(json.dumps({'detections': [{'frame': f, 'bbox': [1, 1, 5, 5], 'text': 'LYMPHORIA'} for f in hits],
                                       'scanned_frames': len(range(a, b, step))}))
        real = wb.subprocess.run; wb.subprocess.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as d:
                r = wb.fast_scan('bin', 'v.mp4', d, 'sha', 'PHOR', 'LYMPHORIA', 1000)
        finally:
            wb.subprocess.run = real
        frames = sorted({x['frame'] for x in r['detections']})
        self.assertEqual(frames, list(range(400, 430)))                 # every frame of the mark, not every 4th
        self.assertEqual({step for step, a, b in calls}, {1, 4})
        lo, hi = r['dense_ranges'][0]
        self.assertTrue(all(lo <= a and b <= hi for step, a, b in calls if step == 1))   # frame by frame only around the hits
        self.assertEqual(len(r['dense_ranges']), 1)
        lo, hi = r['dense_ranges'][0]; self.assertTrue(lo <= 400 and hi >= 430)   # the dense pass covers the whole mark
        self.assertLess(r['scanned_frames'], 400)


class CutsNearProductOnly(unittest.TestCase):
    def shots(self):
        out = []
        for i in range(60):                                           # 60 shots of 2 s
            out.append({'id': f'S{i + 1:03}', 'in_frame': i * 60, 'out_frame': (i + 1) * 60, 'decision': 'replace' if i == 40 else 'keep'})
        return out

    def test_wanted_cuts(self):
        w = scenes.wanted_cuts(self.shots(), 30)
        self.assertTrue(w); self.assertTrue(all(abs(k - 40) <= 11 for k in w))
        self.assertIsNone(scenes.wanted_cuts([{'id': 'S1', 'in_frame': 0, 'out_frame': 9}], 30))

    def test_only_wanted_cuts_are_asked(self):
        shots = self.shots(); wanted = scenes.wanted_cuts(shots, 30); asked = []
        scenes.shot_strip = lambda master, s, fps, path: path     # no ffmpeg in this test

        def call(parts):
            names = [p[1].split()[1] for p in parts if p[0] == 'text' and p[1].startswith('Shot ')]
            asked.extend(names[1:])
            return {'cuts': [{'next_shot': n, 'same_location': True, 'continuous': True} for n in names[1:]]}
        cuts = scenes.classify_cuts(Path(tempfile.mkdtemp()), 'm.mp4', shots, 30, {}, call, wanted=wanted)
        self.assertEqual(sorted(asked), sorted(shots[k]['id'] for k in wanted))
        self.assertEqual(cuts[5]['level'], 'paragraph'); self.assertTrue(cuts[5]['far'])
        self.assertEqual(cuts[min(wanted)]['level'], 'none')


class CaptionColours(unittest.TestCase):
    def test_white_fill_and_yellow_highlight_are_told_apart(self):
        import numpy as np
        crop = np.zeros((40, 200, 3), np.uint8)
        crop[10:30, 10:120] = (250, 250, 250)                         # white words (BGR)
        crop[10:30, 130:190] = (40, 230, 240)                         # the highlighted word, yellow
        fill, hi = cb._fill_colours(crop)
        self.assertTrue(min(fill) > 230)
        self.assertIsNotNone(hi); self.assertLess(hi[0], 100)


class FinalScreenScope(unittest.TestCase):
    def test_changed_ranges(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / 'run'; (run / 'plan').mkdir(parents=True); (run / 'watermark').mkdir()
            master = Path(d) / 'master.mp4'
            subprocess.run([swap.media.binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i', 'color=c=black:s=64x64:r=30:d=40',
                            '-pix_fmt', 'yuv420p', str(master)], check=True)
            tl = [{'source_in': 0, 'source_out': 300, 'picture_id': None, 'out_in': 0, 'out_out': 300},
                  {'source_in': 300, 'source_out': 450, 'picture_id': 'G01', 'out_in': 300, 'out_out': 480},
                  {'source_in': 450, 'source_out': 1200, 'picture_id': None, 'out_in': 480, 'out_out': 1200}]
            Path(str(master) + '.timeline.json').write_text(json.dumps({'segments': tl}))
            swap.save(run / 'plan/segments.json', {'segments': [{'id': 'G02', 'in_frame': 900, 'out_frame': 960}]})
            swap.save(run / 'plan/selections.json', {'G02': {'picture': 'source', 'generated': False}})
            swap.save(run / 'watermark/scan.json', {'detections': [{'frame': 100, 'classification': 'floating_overlay'},
                                                                   {'frame': 700, 'classification': 'printed_label_keep'}]})
            r = swap.changed_ranges(run, master, pad=10)
            self.assertIn((290, 490), r)                               # the generated segment
            self.assertIn((920, 1000), r)                              # the kept-source segment, shifted by +30
            self.assertIn((90, 111), r)                                # the original watermark frame
            self.assertFalse(any(a <= 700 + 30 < b for a, b in r))     # a printed label is not a watermark


if __name__ == '__main__':
    unittest.main()


class CaptionCopy(unittest.TestCase):
    """User 2026-11-19: "字幕是不固定的" -- the film's own captions are copied, not imitated."""
    def setUp(self):
        import caption_matte
        self.cm = caption_matte

    def test_ocr_variants_merge_into_one_caption(self):
        scan = {'detections': [{'frame': f, 'bbox': [10, 900, 300, 40], 'text': t} for f, t in
                               [(0, 'cara por mí'), (1, 'cara por mi'), (2, 'cara por mí'), (3, 'cara por míl'), (20, 'otra cosa')]]}
        ev = self.cm.text_events(scan, lambda d: True)
        self.assertEqual(len(ev), 2); self.assertEqual((ev[0]['k0'], ev[0]['k1']), (0, 3))

    def test_product_print_and_gibberish_are_not_titles(self):
        banned = {'lymphatic', 'drainage'}
        self.assertFalse(self.cm.readable('lymphatic drainage', banned))
        self.assertFalse(self.cm.readable('ЗАСТОNАL УЫРНАТІ', banned))
        self.assertTrue(self.cm.readable('THAT BELLY', banned))
        self.assertTrue(self.cm.readable('SEMANA 5', banned))

    def test_changed_lines_are_drawn_new(self):
        e = {'key': ('medio', 'gotero', 'con')}
        self.assertTrue(self.cm.changed_event(e, {'medio', 'gotero'}))
        self.assertFalse(self.cm.changed_event({'key': ('el', 'desayuno')}, {'medio', 'gotero'}))

    def test_shift_follows_the_generated_words(self):
        e = {'key': ('mi', 'hija', 'tuvo'), 'k0': 48, 'k1': 60}               # 1.6 s at 30 fps
        heard = [{'w': 'Hace', 'a': 0.5}, {'w': 'doce', 'a': 0.8}, {'w': 'mi', 'a': 1.9}, {'w': 'hija', 'a': 2.0}, {'w': 'tuvo', 'a': 2.2}]
        self.assertAlmostEqual(self.cm.event_shift(e, 30, heard, {'mi', 'hija', 'tuvo'}), 0.3, places=2)
        self.assertIsNone(self.cm.event_shift({'key': ('semana', '5'), 'k0': 0, 'k1': 30}, 30, heard, {'mi'}))


class CaptionCopyShortLinesAndTurns(unittest.TestCase):
    """11-19 G16 board: "y si a" / "mi no me" were never copied (no 3-letter word), and "dinero" moved earlier was laid
    over "te devuelven el"."""
    def setUp(self):
        import caption_matte
        self.cm = caption_matte

    def test_short_word_lines_match_the_speech(self):
        said_all = {self.cm.fold(t) for t in self.cm.norm_tokens('Y si a mí no me hace un carajo')}
        said = {t for t in said_all if len(t) >= 3}
        self.assertTrue(self.cm.says('y si a', said, said_all))
        self.assertTrue(self.cm.says('mi no me', said, said_all))
        self.assertFalse(self.cm.says('a', said, said_all))              # one stray letter is not a caption
        self.assertFalse(self.cm.says('tu no', said, said_all))
        self.assertTrue(self.cm.says('hace un carajo', said, said_all))

    def test_accents_do_not_block_the_shift(self):
        e = {'key': ('mi', 'no', 'me'), 'k0': 30, 'k1': 40}
        heard = [{'w': 'mí', 'a': 1.1}, {'w': 'no', 'a': 1.2}, {'w': 'me', 'a': 1.3}]
        self.assertAlmostEqual(self.cm.event_shift(e, 30, heard, {'mí', 'no', 'me'}), 0.1, places=2)

    def test_a_line_moved_earlier_cuts_the_previous_one_short(self):
        ev = [{'k0': 0, 'k1': 29}, {'k0': 30, 'k1': 59}, {'k0': 0, 'k1': 89}]    # two lines + a title card
        win = self.cm.show_windows(ev, [0.09, -0.20, 0.0], 30, 2)
        self.assertAlmostEqual(win[0][1], 0.80, places=2)                 # ends where "dinero" starts
        self.assertAlmostEqual(win[1][0], 0.80, places=2)
        self.assertEqual(win[2], [0.0, 3.0])                              # title card untouched
        self.assertIsNone(self.cm.show_windows(ev, [None, 0, 0], 30, 2)[0])
