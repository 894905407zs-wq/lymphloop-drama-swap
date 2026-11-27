"""11-19 test film 2412: single-shot regeneration, the 100x channel and the finish chain, made skill commands."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))


class RefPlan(unittest.TestCase):
    def setUp(self):
        import shot_regen
        self.sr = shot_regen

    def test_short_shot_is_slowed_to_four_seconds(self):
        p = self.sr.plan_ref([[342, 402]], 30)                       # 2 s
        self.assertEqual(p['mode'], 'slow'); self.assertAlmostEqual(p['factor'], 2.0); self.assertEqual(p['seconds'], 4.0)

    def test_long_shot_keeps_speed_and_holds_to_whole_second(self):
        p = self.sr.plan_ref([[79, 230]], 30)                        # 5.03 s
        self.assertEqual((p['mode'], p['factor'], p['seconds']), ('hold', 1.0, 6.0))

    def test_joined_shots_run_at_real_speed(self):
        p = self.sr.plan_ref([[149, 233], [289, 343]], 30)           # 2.8 + 1.8 s (G16 product shots)
        self.assertEqual((p['mode'], p['seconds']), ('join', 5.0))

    def test_joined_graph_mutes_the_second_piece_only(self):
        p = self.sr.plan_ref([[149, 233], [289, 343]], 30)
        g, amap = self.sr.ref_graph([[149, 233], [289, 343]], 30, p, {0})
        self.assertIn('asetpts=PTS-STARTPTS[a0]', g); self.assertIn('asetpts=PTS-STARTPTS,volume=0[a1]', g); self.assertEqual(amap, '[a]')

    def test_mute_window_silences_the_old_word(self):
        p = self.sr.plan_ref([[0, 138]], 30)
        g, _ = self.sr.ref_graph([[0, 138]], 30, p, {0}, [(0.9, 2.15)])
        self.assertIn('hflip', self.sr.ref_graph([[0, 138]], 30, p, {0}, hflip=True)[0])
        self.assertIn("volume=enable='between(t\\,0.900\\,2.150)':volume=0[a]", g)

    def test_bad_range(self):
        with self.assertRaises(SystemExit): self.sr.parse_range('40-30')


class SplicePlan(unittest.TestCase):
    def setUp(self):
        import shot_regen
        self.sr = shot_regen

    def test_slowed_shot_lands_on_its_base_frames(self):
        shot = {'name': 'G05-S170-c1', 'mode': 'slow', 'pieces': [[342, 402]], 'ref_fps': 30.0, 'seconds': 4.0, 'real_s': 2.0,
                'gen_fps': 24.0, 'gen_frames': 97}
        pics, _ = self.sr.splice_pieces('G05-a1-c1', 337, 24.0, [shot])
        self.assertEqual(pics, ['G05-a1-c1:0-273', 'G05-S170-c1:0-95@48', 'G05-a1-c1:322-336'])

    def test_joined_shots_are_split_back_in_place(self):
        shot = {'name': 'G16-p-c1', 'mode': 'join', 'pieces': [[149, 233], [289, 343]], 'ref_fps': 30.0, 'seconds': 5.0,
                'real_s': 4.6, 'gen_fps': 24.0, 'gen_frames': 121}
        pics, _ = self.sr.splice_pieces('G16-a3-c1', 337, 24.0, [shot])
        self.assertEqual(pics, ['G16-a3-c1:0-118', 'G16-p-c1:0-66@67', 'G16-a3-c1:186-230', 'G16-p-c1:67-109@43', 'G16-a3-c1:274-336'])

    def test_one_frame_gap_between_new_shots_is_absorbed(self):
        s = lambda n, a, b: {'name': n, 'mode': 'slow', 'pieces': [[a, b]], 'ref_fps': 30.0, 'seconds': 4.0, 'real_s': 1,
                             'gen_fps': 24.0, 'gen_frames': 97}
        pics, _ = self.sr.splice_pieces('G05-a2-c1', 337, 24.0, [s('A', 231, 270), s('B', 271, 316)])
        self.assertEqual(pics, ['G05-a2-c1:0-184', 'A:0-95@32', 'B:0-95@36', 'G05-a2-c1:253-336'])

    def test_overlapping_shots_are_refused(self):
        a = {'name': 'A', 'mode': 'slow', 'pieces': [[0, 60]], 'ref_fps': 30.0, 'seconds': 4.0, 'real_s': 2, 'gen_fps': 24.0, 'gen_frames': 97}
        b = {**a, 'name': 'B', 'pieces': [[50, 90]]}
        with self.assertRaises(SystemExit): self.sr.splice_pieces('X', 200, 24.0, [a, b])

    def test_copy_the_action_is_refused(self):
        errs, _ = self.sr.check_prompt('@视频1 是这个镜头放慢后的样子，人物动作和节奏照它。阶段一：他右手捏着条包。')
        self.assertTrue(any('照它' in e for e in errs))
        errs, _ = self.sr.check_prompt('表情和口型照@视频1。阶段一：他右手捏着一支条包。不要字幕。')
        self.assertEqual(errs, [])
        import swap
        _, warns = swap.copy_risks('白发老人坐在桌边，动作照@视频1')
        self.assertTrue(any(r == '§4.4 动作' for r, _, _ in warns))

    def test_prompt_checks(self):
        for bad in ('竖版 9:16。阶段一：他手里拿着纸盒。', '镜头1（0–4 秒）：他手里拿着纸盒。', '阶段一：720P画面，他手里拿着纸盒。'):
            errs, _ = self.sr.check_prompt(bad)                 # user 2026-11-20: ratio, duration, resolution are parameters only
            self.assertTrue(any('接口参数' in e for e in errs), bad)
        errs, _ = self.sr.check_prompt('阶段一：桌上没有瓶子。')
        self.assertTrue(any('正向' in e for e in errs))
        errs, _ = self.sr.check_prompt('阶段一：他右手捏着一支已开封条包。不要字幕。')
        self.assertEqual(errs, [])


class CartonTemplate(unittest.TestCase):
    """User 2026-11-20: carton segments use the five-section template, carton_views + carton_hand, 100x seedance-2.0 720P 9:16."""
    BODY = ('【生成目标】\n复刻@视频1的两镜头，将桌上的2个绿白窄盒完全替换为1盒LymphLoop纸盒。\n\n'
            '【参考素材职责】\n@视频1用于复刻人物和机位，完全移除原片中的2个绿白窄盒与字幕。\n@图片1用于纸盒外观，不采用九宫格版式和白底。\n\n'
            '【主体与关系】\n木桌面上仅底面着地稳立1盒纸盒（无第二个盒子）。\n\n'
            '【事件脚本】\n阶段一：过肩中景。他右手拿起纸盒，台词：{¿Y si a mí no me} 同事自然闭口。结束时纸盒在他右手里。\n'
            '阶段二：中近景。说完后双唇紧抿，不再说话。\n\n'
            '【保持一致】\n全程仅1盒纸盒，硬挺无挤压变形；无bgm；画面不显示字幕。')

    def test_removals_allowed_outside_the_event_script(self):
        import swap, shot_regen
        self.assertEqual(swap.negative_sentences(self.BODY), [])
        errs, _ = shot_regen.check_prompt(self.BODY, '100x-seedance-2.0')
        self.assertEqual(errs, [])                                   # no "竖版 9:16" needed on 2.0 (9:16 is a parameter)

    def test_event_script_still_positive(self):
        import swap
        bad = self.BODY.replace('结束时纸盒在他右手里。', '桌上没有瓶子。结束时纸盒在他右手里。')
        self.assertTrue(swap.negative_sentences(bad))

    def test_100x_is_only_seedance_20(self):
        import shot_regen
        self.assertEqual(shot_regen.X100_DEFAULT_MODEL, '100x-minimax-h3')
        self.assertEqual(list(shot_regen.X100_SETTINGS), ['100x-minimax-h3', '100x-seedance-2.0'])   # 2.5 has no 9:16 parameter

    def test_x100_settings_from_the_job(self):
        import x100_gen
        self.assertEqual(x100_gen.job_settings({'x100': {'model': '100x-seedance-2.0', 'resolution': '720P', 'aspect_ratio': '9:16'}}),
                         ('100x-seedance-2.0', '720P', '9:16'))
        self.assertEqual(x100_gen.job_settings({'model': 'seedance-2.0'}), ('100x-minimax-h3', '768P', '9:16'))
        self.assertEqual(x100_gen.job_settings({'x100': {'model': '100x-minimax-h3'}}), ('100x-minimax-h3', '768P', '9:16'))
        with self.assertRaises(SystemExit):
            x100_gen.job_settings({'job_id': 'G01-a1', 'x100': {'model': '100x-seedance-2.5'}})


class X100Approval(unittest.TestCase):
    def setUp(self):
        import x100_gen
        self.x = x100_gen

    def test_first_attempt_needs_no_quote(self):
        self.assertIsNone(self.x.approval_problem({'job_id': 'G06-a1'}, None))

    def test_regeneration_needs_the_users_words(self):
        self.assertIn('user', self.x.approval_problem({'job_id': 'G06-a2'}, None))
        self.assertIsNone(self.x.approval_problem({'job_id': 'G05-S170', 'shot_regen': {'quote': '一个个镜头生成'}}, None))

    def test_paid_result_is_never_paid_again(self):
        led = {'candidates': {'c1': {'status': 'downloaded', 'output_path': '/x.mp4'}}}
        self.assertIn('already', self.x.approval_problem({'job_id': 'G06-a1'}, led))


class FinishChain(unittest.TestCase):
    def test_steps_and_board_args(self):
        import finish, argparse
        a = argparse.Namespace(full=True, previous='v1.mp4', previous_timeline=None, previous_label='第一版')
        cmd = finish.step_cmd('final-screen', a, Path('/r'))
        self.assertEqual(cmd[-3:], ['--run', str(Path('/r')), '--full'])
        cmd = finish.step_cmd('board', a, Path('/r'))
        self.assertIn('--previous', cmd); self.assertIn('第一版', cmd); self.assertNotIn('--previous-timeline', cmd)


if __name__ == '__main__':
    unittest.main()


class GoofShot(unittest.TestCase):
    """User 2026-11-20: a goof in the first generation -> find that shot against the source, redo only that shot."""
    def test_shot_span_and_once_per_shot(self):
        import shot_regen as sr, tempfile, json
        self.assertEqual(sr.shot_span([3.25, 5.04, 7.46], 6.0, 13.0), (5.04, 7.46))
        self.assertEqual(sr.shot_span([], 8.0, 11.1), (0.0, 11.1))
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'jobs').mkdir(); (run/'generation/jobs').mkdir(parents=True); (run/'plan').mkdir()
            (run/'plan/segments.json').write_text(json.dumps({'fps': 30, 'segments': [{'id': 'G10', 'in_frame': 21419}]}))
            (run/'jobs/G10-S090tail.json').write_text(json.dumps({'shot_regen': {'segment': 'G10', 'pieces': [[99, 333]], 'ref_fps': 30}}))
            (run/'generation/jobs/G10-S090tail.json').write_text(json.dumps({'candidates': {'c1': {'status': 'downloaded'}}}))
            m = lambda name, pieces: {'segment': 'G10', 'name': name, 'pieces': pieces, 'ref_fps': 30}
            self.assertEqual([j for j, _ in sr.earlier_regen(run, m('S090b', [[200, 300]]))], ['G10-S090tail'])
            self.assertEqual(sr.earlier_regen(run, m('S090c', [[0, 90]])), [])
            # user 2026-11-20: a second redo of the same place must be narrower (one action), and two redos is the limit
            self.assertIn('缩小范围', sr.redo_problem(run, m('S090b', [[99, 333]])))
            self.assertIsNone(sr.redo_problem(run, m('S090b', [[200, 260]])))
            (run/'jobs/G10-S090b.json').write_text(json.dumps({'shot_regen': {'segment': 'G10', 'pieces': [[200, 260]], 'ref_fps': 30}}))
            (run/'generation/jobs/G10-S090b.json').write_text(json.dumps({'candidates': {'c1': {'status': 'downloaded'}}}))
            # third redo allowed (user 2026-11-21), not longer than the narrowest before it
            self.assertIsNone(sr.redo_problem(run, m('S090c', [[200, 260]])))
            self.assertIn('不能比之前最窄', sr.redo_problem(run, m('S090c', [[150, 260]])))
            (run/'jobs/G10-S090c.json').write_text(json.dumps({'shot_regen': {'segment': 'G10', 'pieces': [[200, 260]], 'ref_fps': 30}}))
            (run/'generation/jobs/G10-S090c.json').write_text(json.dumps({'candidates': {'c1': {'status': 'downloaded'}}}))
            self.assertIn('最多补做 3 次', sr.redo_problem(run, m('S090d', [[210, 240]])))
            # a --source-s redo across two segments compares in source seconds
            self.assertEqual(sr.abs_windows(run, {'source_s': '431.6-440.733'}), [[431.6, 440.733]])

    def test_mirror_check(self):
        import shot_regen as sr, tempfile, subprocess
        with tempfile.TemporaryDirectory() as td:
            src, flip = Path(td)/'s.mp4', Path(td)/'f.mp4'
            subprocess.run([sr.ff(), '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=s=180x320:d=2:r=24', '-pix_fmt', 'yuv420p', str(src)], check=True)
            subprocess.run([sr.ff(), '-v', 'error', '-y', '-i', str(src), '-vf', 'hflip', str(flip)], check=True)
            self.assertTrue(sr.mirrored(flip, src, [0.5, 1.0]))
            self.assertFalse(sr.mirrored(src, src, [0.5, 1.0]))


class ExactGoofSplice(unittest.TestCase):
    def test_into_without_reference_tuple_accepts_native_sound(self):
        import shot_regen as sr, tempfile, json, argparse
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as td:
            r=Path(td); (r/'plan').mkdir(); (r/'jobs').mkdir(); (r/'generation/results').mkdir(parents=True)
            (r/'plan/selections.json').write_text('{}')
            (r/'jobs/G01-fix.json').write_text(json.dumps({'shot_regen':{'mode':'hold','seconds':5,'real_s':4.5,'pieces':[[0,135]],'ref_fps':30}}))
            (r/'generation/results/G01-fix-c1.mp4').touch()
            a=argparse.Namespace(run=str(r),seg='G01',base='G01-a1-c1',shots=['fix'],into='24-131',out_attempt='a2',note=None,force=False,shot_sound=['fix'],dry_run=False)
            info={'fps':'24','frame_count':145}
            with patch('swap.splice_source',return_value=Path('/base.mp4')),patch.object(sr.media,'probe',return_value=info),patch.object(sr.subprocess,'run') as run,\
                    patch.object(sr,'voice_end',return_value=4.6):
                res=sr.cmd_splice(a)
            cmd=run.call_args.args[0]
            # real speed, own length to the end of its speech (4.6 + 0.15 s, capped at the 5 s it was generated for), own sound
            self.assertIn('G01-fix-c1:0-113',cmd); self.assertNotIn('--audio',cmd)
            self.assertAlmostEqual(res['delta_s'], (24+114+13-145)/24)


class RedoSound(unittest.TestCase):
    """User 2026-11-20: the sound of a redone shot, three cases; a short lip-sync shot is not slowed."""
    def test_sound_plan(self):
        import shot_regen as sr
        self.assertEqual(sr.sound_plan([], [], False), 'none')
        self.assertEqual(sr.sound_plan(['Hi.'], [], False), 'source')
        self.assertEqual(sr.sound_plan(['Esto es Lymphoria.'], [('Lymphoria', 'LymphLoop')], False), 'first_generation')
        self.assertEqual(sr.sound_plan(['Hi.'], [], True), 'lip_sync')

    def test_short_lip_sync_shot_stays_real_speed(self):
        import shot_regen as sr
        self.assertEqual(sr.plan_ref([[0, 60]], 30)['mode'], 'slow')
        p = sr.plan_ref([[0, 60]], 30, real_speed=True)
        self.assertEqual((p['mode'], p['seconds'], p['factor']), ('hold', 4.0, 1.0))


class NativeReferencePadding(unittest.TestCase):
    def test_actual_frames_keep_native_speed_and_pad_the_video_stream(self):
        import shot_regen as sr, tempfile, subprocess, json
        for fps in (24, 30):
            with self.subTest(fps=fps), tempfile.TemporaryDirectory() as td:
                source=Path(td)/'source.mkv'
                subprocess.run([sr.ff(),'-v','error','-y','-f','lavfi','-i',f'testsrc2=size=96x160:rate={fps}:duration=8',
                                '-f','lavfi','-i','sine=frequency=431:sample_rate=48000:duration=8',
                                '-c:v','ffv1','-c:a','pcm_s16le',str(source)],check=True)
                def frames(path):
                    raw=subprocess.check_output([sr.ff(),'-v','error','-i',str(path),'-map','0:v:0','-pix_fmt','gray','-f','rawvideo','-'])
                    size=96*160;return [raw[i:i+size]for i in range(0,len(raw),size)]
                original=frames(source)
                for pieces in ([[53,118]], [[0,146]], [[2,23],[47,81]]):
                    plan=sr.plan_ref(pieces,fps,real_speed=True);graph,amap=sr.ref_graph(pieces,fps,plan,set(range(len(pieces))))
                    target=Path(td)/'padded.mkv'
                    subprocess.run([sr.ff(),'-v','error','-y','-i',str(source),'-filter_complex',graph,
                                    '-map','[v]','-map',amap,'-t',str(plan['seconds']),'-r',str(fps),'-c:v','ffv1','-c:a','pcm_s16le',str(target)],check=True)
                    actual=frames(target);selected=[f for a,b in pieces for f in original[a:b]]
                    self.assertEqual(len(actual),round(plan['seconds']*fps))
                    self.assertEqual(actual[:len(selected)],selected)
                    self.assertTrue(all(f==selected[-1]for f in actual[len(selected):]))
