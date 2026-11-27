"""Reuse across RUNs (user 2026-11-22; Codex: "尚未找到跨 RUN 导入共用结果的命令"): a film that shares footage with a
finished film takes that film's reviewed segments, frame-matched, with no API call."""
import argparse, contextlib, io, json, sys, tempfile, unittest
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'scripts'))
import media_pipeline as m  # noqa: E402
import reuse as rz  # noqa: E402
import swap  # noqa: E402

FPS = 25


def encode(path, graph, n_inputs, crf=23):
    args = ['-y']
    for src in n_inputs: args += ['-f', 'lavfi', '-i', src]
    m.ff([*args, '-filter_complex', graph, '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', str(crf),
          '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)])


class Films(unittest.TestCase):
    """Donor: 8 s of moving test picture. Target: a 2 s hook of other footage, then the donor's film from its 1 s on,
    in another encode: target frame = donor frame + 25."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='lymphloop-reuse-'); r = cls.root = Path(cls.tmp.name)
        body = f'testsrc2=s=360x640:r={FPS}:d=8'; tone = 'sine=frequency=330:sample_rate=48000:duration=8'
        encode(r/'donor.mp4', '[0:v]null[v];[1:a]anull[a]', [body, tone])
        encode(r/'target.mp4', '[0:v]trim=start=1,setpts=PTS-STARTPTS[b];[1:v]trim=end=2,setpts=PTS-STARTPTS[h];[h][b]concat=n=2:v=1:a=0[v];[2:a]anull[a]',
               [body, f'mandelbrot=s=360x640:r={FPS}', 'sine=frequency=550:sample_rate=48000:duration=9'], crf=30)

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def run_dir(self, name, master, segments, extra_stages=()):
        run = self.root/name
        for d in ('plan', 'prompts', 'generation/results', 'generation/jobs', 'out'): (run/d).mkdir(parents=True, exist_ok=True)
        ref = {'path': str(master), 'sha256': swap.sha(master)}
        state = {'source': str(master), 'output_dir': str(run/'out'), 'stages': {'prepare': {'cfr_master': ref, 'working_master': ref}, **{k: {} for k in extra_stages}}}
        (run/'state.json').write_text(json.dumps(state))
        (run/'plan/segments.json').write_text(json.dumps({'fps': FPS, 'segments': segments}))
        return run

    def seg(self, gid, a, b):
        return {'id': gid, 'in_frame': a, 'out_frame': b, 'duration_s': (b - a) / FPS, 'shot_ids': ['S001'], 'shot_times': [['S001', 0.0, (b - a) / FPS]],
                'product_shots': ['S001'], 'speech': [], 'rewrites': {'dialogue': [], 'vo': []}, 'kinds': ['Visual']}

    def donor(self):
        run = self.run_dir('donor', self.root/'donor.mp4', [self.seg('G01', 50, 100)], extra_stages=('recut', 'final_screen'))
        base = run/'generation/results/G01-a1-c1.mp4'; cap = run/'generation/results/G01-a1-c1.captioned.mp4'
        if not base.is_file():
            encode(base, '[0:v]null[v];[1:a]anull[a]', [f'color=c=blue:s=360x640:r={FPS}:d=2', 'sine=frequency=880:sample_rate=48000:duration=2'])
            encode(cap, '[0:v]null[v];[1:a]anull[a]', [f'color=c=navy:s=360x640:r={FPS}:d=2', 'sine=frequency=880:sample_rate=48000:duration=2'])
        (run/'generation/jobs/G01-a1.json').write_text(json.dumps({'provider': 'apimart', 'job_id': 'G01-a1', 'candidates': {
            'c1': {'status': 'downloaded', 'output_path': str(base), 'output_sha256': swap.sha(base), 'cost_usd': 1.5}}}))
        (run/'plan/selections.json').write_text(json.dumps({'G01': {
            'job': 'G01-a1', 'candidate': 'c1', 'media': swap.ref(cap), 'media_uncaptioned': swap.ref(base), 'clip_in': 0.0, 'clip_out': 2.0,
            'note': '纸盒正面', 'captions': {'method': 'copied from the film', 'events': []}, 'defects': ['品牌名轻微近音']}}))
        (run/'prompts/G01-a1.final.txt').write_text('【生成目标】\n…')
        (run/'plan/board_notes.json').write_text(json.dumps({'G01': {'issue': '品牌名轻微近音'}}))
        return run

    def reuse(self, target, donor, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            swap.cmd_reuse(argparse.Namespace(run=str(target), donor=[str(donor)], dry_run=False, unfinished_donor=False, **kw))
        return json.loads(out.getvalue())

    def test_prints_find_the_same_footage_at_its_offset(self):
        dp, tp = rz.prints(self.root/'donor.mp4'), rz.prints(self.root/'target.mp4')
        hit = rz.locate(dp, tp, 50, 100)
        self.assertEqual((hit['offset'], hit['ok']), (25, True)); self.assertLess(hit['worst'], rz.SAME)
        miss = rz.locate(dp, tp, 0, 20)                      # the donor's first second is not in the target
        self.assertFalse(miss and miss['ok'])
        self.assertTrue(rz.same_at(dp, tp, 60, 75, 25)); self.assertFalse(rz.same_at(dp, tp, 40, 75, 25))   # 40-50 is the hook

    def test_shared_segment_is_imported_and_the_film_recuts_with_it(self):
        donor = self.donor()
        target = self.run_dir('target', self.root/'target.mp4', [self.seg('G01', 10, 40), self.seg('G02', 80, 120)])
        rep = self.reuse(target, donor)
        self.assertEqual([(x['segment'], x['source_s'], x['covers']) for x in rep['adopted']], [('G901', [3.0, 5.0], ['G02'])])
        self.assertEqual(rep['left_to_generate'], ['G01'])      # the hook is this film's own and is generated as usual
        plan = json.loads((target/'plan/segments.json').read_text())
        self.assertEqual([x['id'] for x in plan['segments']], ['G01']); self.assertEqual([x['id'] for x in plan['dropped_for_reuse']], ['G02'])
        g = plan['reused_segments'][0]
        self.assertEqual((g['id'], g['in_frame'], g['out_frame'], g['reused_from']['segment'], g['reused_from']['offset']), ('G901', 75, 125, 'G01', 25))
        sel = json.loads((target/'plan/selections.json').read_text())['G901']
        self.assertTrue(sel['media']['path'].endswith('target/generation/results/G901-a1-c1.captioned.mp4'))
        self.assertTrue(sel['media_uncaptioned']['path'].endswith('G901-a1-c1.mp4'))
        self.assertEqual((sel['clip_in'], sel['clip_out'], sel['defects'], sel['captions']['method']), (0.0, 2.0, ['品牌名轻微近音'], 'copied from the film'))
        led = json.loads((target/'generation/jobs/G901-a1.json').read_text())
        self.assertEqual((led['provider'], led['candidates']['c1']['cost_usd']), ('reused_generation', 0))
        self.assertIsNotNone(swap.picture_origin(target, sel['media']['path']))          # recut accepts it as a registered result
        self.assertEqual(json.loads((target/'plan/board_notes.json').read_text()), {'G901': {'issue': '品牌名轻微近音'}})
        self.assertTrue((target/'prompts/G901-a1.final.txt').is_file())
        self.assertEqual(json.loads((target/'state.json').read_text())['stages']['reuse']['adopted'], ['G901'])

        # the film recuts with the reused segment in place (the hook segment keeps its source picture here)
        sels = json.loads((target/'plan/selections.json').read_text())
        sels['G01'] = {'job': 'G01-a1', 'candidate': None, 'picture': 'source', 'generated': False}
        (target/'plan/selections.json').write_text(json.dumps(sels))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            swap.cmd_recut(argparse.Namespace(run=str(target), user_approved_cut=None, content_preserved_review=None))
        tl = json.loads((target/'out/master.mp4.timeline.json').read_text())['segments']
        self.assertEqual([(x['source_in'], x['source_out']) for x in tl if x.get('picture_id') == 'G901'], [(75, 125)])

        # running it again starts from scratch: same result, nothing doubled
        rep2 = self.reuse(target, donor)
        self.assertEqual([x['segment'] for x in rep2['adopted']], ['G901'])
        plan = json.loads((target/'plan/segments.json').read_text())
        self.assertEqual(([x['id'] for x in plan['segments']], len(plan['reused_segments'])), (['G01'], 1))

    def test_segment_with_footage_the_donor_does_not_have_is_kept(self):
        donor = self.donor()
        target = self.run_dir('target2', self.root/'target.mp4', [self.seg('G01', 40, 100)])     # 40-50 is the hook, 50-100 the donor's film
        rep = self.reuse(target, donor)
        self.assertEqual(rep['adopted'], []); self.assertEqual([x['segment'] for x in rep['kept']], ['G01'])
        self.assertIn('代表片里没有的画面', rep['kept'][0]['reason'])
        plan = json.loads((target/'plan/segments.json').read_text())
        self.assertEqual([x['id'] for x in plan['segments']], ['G01']); self.assertNotIn('reused_segments', plan)

    def test_segment_wider_than_the_donors_inside_shared_footage_is_set_aside(self):
        donor = self.donor()
        target = self.run_dir('target3', self.root/'target.mp4', [self.seg('G01', 60, 140)])     # all of it is the donor's film; 75-125 was regenerated there
        rep = self.reuse(target, donor)
        self.assertEqual([x['segment'] for x in rep['adopted']], ['G901'])
        self.assertEqual(rep['set_aside'][0]['donor_left_original'], [[2.4, 3.0], [5.0, 5.6]])     # the donor left these frames original

    def test_unfinished_donor_and_generated_segment_are_refused(self):
        donor = self.donor()
        st = json.loads((donor/'state.json').read_text()); st['stages'].pop('final_screen'); (self.root/'donor_unfinished').mkdir(exist_ok=True)
        target = self.run_dir('target4', self.root/'target.mp4', [self.seg('G01', 80, 120)])
        (donor/'state.json').write_text(json.dumps(st))
        with self.assertRaises(SystemExit) as e: self.reuse(target, donor)
        self.assertIn('还没做完', str(e.exception))
        st['stages']['final_screen'] = {}; (donor/'state.json').write_text(json.dumps(st))
        (target/'generation/jobs/G01-a1.json').write_text(json.dumps({'provider': 'apimart', 'candidates': {}}))    # paid here already
        rep = self.reuse(target, donor)
        self.assertEqual(rep['adopted'], []); self.assertIn('已经自己生成过', rep['kept'][0]['reason'])


if __name__ == '__main__':
    unittest.main()
