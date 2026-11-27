"""Goof redos in rounds (user 2026-11-22: "补穿帮一轮一批"): every goof of a film located, prepared and sent together,
each segment's redone shots put back in one splice, and the reason of every redo recorded."""
import argparse, contextlib, io, json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'scripts'))
import media_pipeline as media  # noqa: E402
import shot_regen as sr  # noqa: E402

PROMPT = """# G01 — 五段式
# views: handheld_photo
# design: S002
# 阶段一 = 镜头1 S001 原片 0.00–2.00 秒
# 阶段二 = 镜头2 S002 原片 2.00–4.00 秒；设计 R5
# 阶段三 = 镜头3 S003 原片 4.00–6.00 秒
【生成目标】
复刻@视频1的厨房三个镜头与英语台词，将原片中的棕色滴管瓶完全替换为1支未开封的青绿色条包。

【参考素材职责】
@视频1用于复刻人物长相服装、厨房陈设、机位和说话口型节奏，完全移除原片中的棕色滴管瓶及字幕。产品的大小不沿用原片里被替换容器的大小和手的张开幅度，按事件脚本里每一镜写的大小参照来画。
@图片1用于未开封条包的外观和它捏在指尖时的比例，不采用图中的手和背景。

【主体与关系】
暖光厨房，穿白衬衫的女士。画面里只有1支未开封的青绿色条包，原片竞品所在的其他位置是空的。音色——女士：成熟美式英语女声，自然口语。

【事件脚本】
阶段一：厨房中景。女士走到岛台前。结束时她站在岛台后。
阶段二：切至岛台近景。女士指尖捏着一支未开封条包举到胸前，正面朝镜头。大小参照：条包约她一根食指长，比拇指指腹略宽，捏住的手指只盖住下半截。女士台词（轻快，用英语）：{This is the one.}。结束时条包停在胸前。
阶段三：切至门口中景。女士转身离开。结束时门口空了。

【保持一致】
全程仅1支未开封的青绿色条包；产品在每一镜里和人脸、手、身边物件的大小关系都一样，不忽大忽小；人物面容服装、道具、空间方向与光线同@视频1一致；听者自然闭口聆听；无bgm；画面不显示字幕。
"""


def movie(path, fps=30):
    """Three 2-second shots with hard cuts and a tone."""
    cols = ['red', 'blue', 'green']
    args = ['-y']
    for c in cols: args += ['-f', 'lavfi', '-i', f'color=c={c}:s=720x1280:r={fps}:d=2']
    args += ['-f', 'lavfi', '-i', 'sine=frequency=330:sample_rate=48000:duration=6', '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]',
             '-map', '[v]', '-map', '3:a', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-shortest', str(path)]
    media.ff(args)


class Pieces(unittest.TestCase):
    def shot(self, name, mode='hold', seconds=4, real=2.0):
        return {'name': f'G01-{name}-c1', 'short': name, 'mode': mode, 'seconds': seconds, 'real_s': real, 'gen_fps': 24.0, 'gen_frames': 97}

    def test_two_shots_of_a_segment_go_back_in_one_splice(self):
        pics, spans = sr.into_pieces('G01-a1-c1', 180, [self.shot('late'), self.shot('early', mode='slow')], ['late=120-167', 'early=24-47'])
        # the real-speed shot goes in at its own length (ripple), the slowed one fills its window
        self.assertEqual(pics, ['G01-a1-c1:0-23', 'G01-early-c1:0-95@24', 'G01-a1-c1:48-119', 'G01-late-c1:0-47', 'G01-a1-c1:168-179'])
        self.assertEqual([(a, b) for a, b, *_ in spans], [(24, 48), (120, 168)])

    def test_single_range_keeps_working_and_overlap_is_refused(self):
        pics, _ = sr.into_pieces('B', 100, [self.shot('x')], ['10-57'])
        self.assertEqual(pics, ['B:0-9', 'G01-x-c1:0-47', 'B:58-99'])
        with self.assertRaises(SystemExit): sr.into_pieces('B', 100, [self.shot('x'), self.shot('y')], ['x=10-57', 'y=50-80'])
        with self.assertRaises(SystemExit): sr.into_pieces('B', 100, [self.shot('x'), self.shot('y')], ['x=10-57'])


class Reasons(unittest.TestCase):
    def test_kinds_by_name_or_checklist_number(self):
        self.assertEqual(sr.goof_kind('1'), 'competitor'); self.assertEqual(sr.goof_kind('Side'), 'side')
        self.assertEqual(len(sr.GOOF_KINDS), 8)
        with self.assertRaises(SystemExit): sr.goof_kind('ugly')

    def test_goof_spec(self):
        self.assertEqual(sr.parse_goof('G05@3.2-4.0:competitor:台面左侧旧瓶：还在'),
                         {'seg': 'G05', 'at': '3.2-4.0', 'why': 'competitor', 'note': '台面左侧旧瓶：还在'})

    def test_reasons_are_counted_over_runs(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td); (r/'jobs').mkdir()
            for name, g in (('G01-a', {'kind': 'competitor', 'note': 'x'}), ('G01-b', {'kind': 'competitor', 'note': 'y'}), ('G02-c', {'kind': 'side', 'note': 'z'}), ('G02-old', None)):
                (r/'jobs'/f'{name}.json').write_text(json.dumps({'shot_regen': {'real_s': 1.0, **({'goof': g} if g else {})}}))
            (r/'jobs'/'G03-a1.json').write_text(json.dumps({'job_id': 'G03-a1'}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out): sr.cmd_reasons(argparse.Namespace(runs=[str(r)]))
            d = json.loads(out.getvalue())
            self.assertEqual((d['redos'], d['by_kind'], d['without_reason']), (4, {'competitor': 2, 'side': 1}, 1))


class Stub(unittest.TestCase):
    def test_segment_prompt_cut_down_to_the_goof_shot(self):
        self.assertEqual([sid for sid, _ in sr.stage_blocks(PROMPT)], ['S001', 'S002', 'S003'])
        stub = sr.stub_from_segment_prompt(PROMPT, ['S002'], ['handheld_photo'])
        self.assertTrue(stub.startswith('# views: handheld_photo\n# design: S002\n'))
        self.assertIn('阶段一：岛台近景。女士指尖捏着一支未开封条包', stub)          # renumbered, "切至" dropped
        self.assertNotIn('阶段二', stub); self.assertNotIn('转身离开', stub)
        self.assertIn('【保持一致】\n全程仅1支', stub); self.assertIn('音色——女士', stub)
        import swap, swap_design
        body = swap.clean_prompt(stub)
        self.assertEqual(swap_design.five_section_problems(body, speech=True, n_images=1), [])

    def test_a_shot_outside_the_design_keeps_no_design_line(self):
        stub = sr.stub_from_segment_prompt(PROMPT, ['S003'])
        self.assertNotIn('# design', stub); self.assertIn('阶段一：门口中景', stub)

    def test_other_images_or_unmatched_stages_fall_back(self):
        self.assertIsNone(sr.stub_from_segment_prompt(PROMPT, ['S002'], ['carton_views', 'carton_hand']))
        self.assertIsNone(sr.stub_from_segment_prompt(PROMPT, ['S009']))

    def test_stages_without_comments_follow_shot_order(self):
        bare = '\n'.join(l for l in PROMPT.splitlines() if not l.startswith('# 阶段'))
        self.assertEqual([sid for sid, _ in sr.stage_blocks(bare, ['S001', 'S002', 'S003'])], ['S001', 'S002', 'S003'])
        self.assertEqual([sid for sid, _ in sr.stage_blocks(bare, ['S001', 'S002'])], [None, None, None])


class Round(unittest.TestCase):
    """open -> submit (dry) -> splice (dry) on a synthetic RUN: two goofs of one segment."""
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='lymphloop-round-'); r = cls.rundir = Path(cls.tmp.name)
        for d in ('clips', 'plan', 'prompts', 'jobs', 'generation/results', 'generation/jobs'): (r/d).mkdir(parents=True)
        movie(r/'clips/G01.hq.clean.ref.mp4'); (r/'clips/G01.hq.clean.ref.json').write_text('{}')
        movie(r/'generation/results/G01-a1-c1.mp4', fps=24)
        (r/'prompts/G01-a1.txt').write_text(PROMPT.replace('# design: S002\n', ''))     # no design table in this RUN
        seg = {'id': 'G01', 'in_frame': 300, 'out_frame': 480, 'duration_s': 6.0, 'shot_times': [['S001', 0.0, 2.0], ['S002', 2.0, 4.0], ['S003', 4.0, 6.0]],
               'speech': [{'start': 2.3, 'end': 3.4, 'text': 'This is the one.', 'speaker': 'Woman', 'kind': 'dialogue'}], 'rewrites': {'dialogue': [], 'vo': []}}
        (r/'plan/segments.json').write_text(json.dumps({'fps': 30, 'segments': [seg]}))
        (r/'plan/selections.json').write_text(json.dumps({'G01': {'job': 'G01-a1', 'candidate': 'c1', 'clip_in': 0.0, 'clip_out': 6.0}}))

    @classmethod
    def tearDownClass(cls): cls.tmp.cleanup()

    def call(self, fn, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): fn(argparse.Namespace(run=str(self.rundir), **kw))
        return out.getvalue()

    def test_round(self):
        r = self.rundir
        opened = json.loads(self.call(sr.cmd_round_open, file=None, round=None, mode='reference', goof=[
            'G01@2.8:side:条包拿侧面当正面', 'G01@5.0:competitor:门边柜子上旧瓶还在']))
        self.assertEqual(opened['round'], 1)
        a, b = opened['items']
        self.assertEqual((a['name'], a['kind'], a['sound']), ('S002_side_r1', 'side', 'lip_sync'))
        self.assertEqual((b['name'], b['kind'], b['sound']), ('S003_competitor_r1', 'competitor', 'none'))
        rec = json.loads((r/'shots-regen/rounds/round-1.json').read_text())
        self.assertEqual([it['base_frames'] for it in rec['items']], [[48, 95], [96, 143]])
        self.assertEqual([it['mode'] for it in rec['items']], ['hold', 'slow'])       # a speaking shot stays at real speed
        for it in rec['items']:
            self.assertTrue(Path(it['strip']).is_file()); self.assertTrue((r/'shots-regen'/f"{it['name']}.ref.mp4").is_file())
            self.assertEqual(json.loads((r/'shots-regen'/f"{it['name']}.json").read_text())['goof']['round'], 1)
        stub = (r/'shots-regen/S002_side_r1.txt').read_text()
        self.assertIn('# 穿帮（side）', stub); self.assertIn('条包拿侧面当正面', stub); self.assertIn('阶段一：岛台近景', stub)
        self.assertTrue(stub.startswith('# views: handheld_photo\n'))

        # the reason travels into the job; a prompt that fails the checks stops the whole batch, nothing is sent
        (r/'shots-regen/S003_competitor_r1.txt').write_text('# views: handheld_photo\n【生成目标】\n【写：没写完】\n【参考素材职责】\n@视频1\n')
        with self.assertRaises(SystemExit) as e:
            self.call(sr.cmd_round_submit, round=None, only=None, partial=False, dry_run=False)
        self.assertIn('S003_competitor_r1', str(e.exception)); self.assertNotIn('== S002_side_r1', str(e.exception))
        self.assertEqual(list((r/'generation/jobs').glob('*')), [])                       # nothing was sent
        self.assertEqual(json.loads((r/'jobs/G01-S002_side_r1.json').read_text())['shot_regen']['goof'],
                         {'kind': 'side', 'note': '条包拿侧面当正面', 'round': 1})

        # both generated: one splice for the segment with both shots, each on its exact base frames
        for it in rec['items']:
            job = r/'jobs'/f"G01-{it['name']}.json"
            if not job.is_file():
                meta = json.loads((r/'shots-regen'/f"{it['name']}.json").read_text())
                job.write_text(json.dumps({'shot_regen': {k: meta[k] for k in ('mode', 'pieces', 'ref_fps', 'seconds', 'real_s')}}))
            (r/'generation/results'/f"G01-{it['name']}-c1.mp4").write_bytes((r/'shots-regen'/f"{it['name']}.ref.mp4").read_bytes())
            (r/'generation/jobs'/f"G01-{it['name']}.json").write_text(json.dumps({'candidates': {'c1': {'status': 'downloaded'}}}))
            it['state'] = 'generated'
        (r/'shots-regen/rounds/round-1.json').write_text(json.dumps(rec))
        out = self.call(sr.cmd_round_splice, round=None, drop=None, partial=False, no_select=False, dry_run=True, finish=False)
        cmd = out.splitlines()[0]
        self.assertIn('--out-attempt a2', cmd)
        # the speaking shot goes in at its own length, to the end of its sound (the stand-in result sounds for all 4 s),
        # with its own sound; everything after it moves; the slowed one takes the base window's sound at its new place
        # (the stand-in is the reference itself: held still from 2 s with a tone and no words, so it goes in to 2 s,
        # not with its 2 s of still picture -- 2026-11-22, user: "生成段的最后都会被定格一下")
        self.assertIn('--picture G01-a1-c1:0-47 --picture G01-S002_side_r1-c1:0-59 --picture G01-S003_competitor_r1-c1:0-119@48', cmd)
        self.assertNotIn('--audio G01-a1-c1', cmd)
        self.assertIn('--audio-window G01-a1-c1:4.000-6.000@4.000', cmd)
        self.assertIn('--no-mouth 4.000-6.000', cmd)

        # a dropped shot (still wrong) stays out of the splice and waits for the next round
        out = self.call(sr.cmd_round_splice, round=None, drop=['S003_competitor_r1'], partial=False, no_select=False, dry_run=True, finish=False)
        self.assertNotIn('S003_competitor_r1-c1', out.splitlines()[0])


    def test_z_no_mouth_shot_is_slowed(self):
        r = self.rundir; f = r/'goofs_nm.json'
        f.write_text(json.dumps([{'seg': 'G01', 'at': '2.8', 'why': 'competitor', 'note': '屏幕特写，没有人脸', 'no_mouth': True, 'name': 'screen_nm'}]))
        out = json.loads(self.call(sr.cmd_round_open, file=str(f), round=7, goof=None, mode='reference'))
        it = out['items'][0]
        self.assertEqual((it['sound'], it['mode']), ('source', 'slow'))


    def test_z_shifted_cuts_warn_and_source_window_is_taken(self):
        # 2026-11-22 film 1659373095741717 G01: the generation dropped a source cut, the shot at the goof is the source's
        # earlier shot; locate warns, and the agent's checked source window replaces the same-seconds guess
        r = self.rundir
        cuts = lambda path: [3.96, 5.79] if 'results' in str(path) else [1.97, 3.97, 5.83]
        with patch.object(sr, 'cut_times', side_effect=cuts):
            info = sr.locate(argparse.Namespace(run=str(r), seg='G01', base=None, at='4.5', snap=0.6, from_s=None, to_s=None))
            self.assertIn('切点对不上', info['timing_warning'])
            info = sr.locate(argparse.Namespace(run=str(r), seg='G01', base=None, at='4.5', snap=0.6, from_s=None, to_s=None,
                                                source_window='1.97-3.97'))
        self.assertEqual(info['source_s'], [1.97, 3.97]); self.assertNotIn('timing_warning', info)
        self.assertEqual(info['ref_frames'], [59, 119]); self.assertEqual(info['shots'], ['S002'])
        with patch.object(sr, 'cut_times', side_effect=cuts), self.assertRaises(SystemExit):
            sr.locate(argparse.Namespace(run=str(r), seg='G01', base=None, at='4.5', snap=0.6, from_s=None, to_s=None, source_window='5-7'))


class ReviewedNoCut(unittest.TestCase):
    def test_reviewed_continuous_shot_passes_the_cut_check(self):
        # 2026-11-22 film 1409783017240926 G03_webhero: a push-in on a laptop page read as a cut at 0.24 s
        with tempfile.TemporaryDirectory() as td:
            r = Path(td); d = r/'shots-regen'; d.mkdir()
            ref = d/'x.ref.mp4'; ref.write_bytes(b'0')
            meta = {'segment': 'G01', 'name': 'x', 'pieces': [[0, 30]], 'ref_fps': 30.0, 'mode': 'hold', 'seconds': 4.0, 'real_s': 1.0,
                    'ref': str(ref), 'ref_sha256': sr.sha(ref), 'goof': {'kind': 'competitor', 'note': 'n', 'round': 1}}
            (d/'x.json').write_text(json.dumps(meta)); (d/'x.txt').write_text(sr.stub_from_segment_prompt(PROMPT.replace('# design: S002\n', ''), ['S002'], ['handheld_photo']))
            a = argparse.Namespace(run=str(r), name='x', user_quote=None, goof=True, channel='apimart', model=None, again=False, why=None, note=None)
            with patch.object(sr, 'cut_times', return_value=[0.24]), self.assertRaises(SystemExit) as e: sr.cmd_prepare(a)
            self.assertIn('硬切', str(e.exception))
            meta['goof']['no_cut_reviewed'] = True; (d/'x.json').write_text(json.dumps(meta))
            with patch.object(sr, 'cut_times', return_value=[0.24]):
                try: sr.cmd_prepare(a); msg = ''
                except Exception as e: msg = str(e)                      # stops later (the 1-byte stand-in video), never at the cut
            self.assertNotIn('硬切', msg)


class PrepareNeedsReason(unittest.TestCase):
    def test_goof_redo_without_a_reason_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            r = Path(td); d = r/'shots-regen'; d.mkdir()
            ref = d/'x.ref.mp4'; ref.write_bytes(b'0')
            (d/'x.json').write_text(json.dumps({'segment': 'G01', 'name': 'x', 'pieces': [[0, 30]], 'ref_fps': 30.0, 'mode': 'hold', 'seconds': 4.0, 'real_s': 1.0,
                                                'ref': str(ref), 'ref_sha256': sr.sha(ref)}))
            body = sr.stub_from_segment_prompt(PROMPT.replace('# design: S002\n', ''), ['S002'], ['handheld_photo'])
            (d/'x.txt').write_text(body)
            a = argparse.Namespace(run=str(r), name='x', user_quote=None, goof=True, channel='apimart', model=None, again=False, why=None, note=None)
            with patch.object(sr, 'cut_times', return_value=[]), self.assertRaises(SystemExit) as e: sr.cmd_prepare(a)
            self.assertIn('--why', str(e.exception))


if __name__ == '__main__':
    unittest.main()
