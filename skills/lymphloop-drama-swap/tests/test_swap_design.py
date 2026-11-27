"""Rule table vs the user's rulings on test film 2412 (2026-11-19/20). Each case: the facts of the original shot (as
they really are) -> what the user ruled. When a new film shows a wrong decision, add the case here and fix RULES."""
import sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))


def obs(action, *items, scale='medium', subject=False):
    return {'hand_action': action, 'shot_scale': scale, 'who_handles': '', 'speaking': False, 'confidence': 1, 'product_is_shot_subject': subject,
            'items': [{'kind': k, 'where': w, 'size': s, 'count': n, 'within_reach_of_person': r, 'look': k} for k, w, s, n, r in items]}


# (shot, facts, accepted forms, user's words)
CASES = [
    ('S152 G01 窗台小瓶', obs('none', ('bottle', 'background_shelf_or_windowsill', 'small', 1, False), scale='medium'),
     {'carton'}, '后面窗台上应该放着盒装lymphloop'),
    ('S153 G02 老人桌前的瓶', obs('none', ('bottle', 'on_table_or_surface', 'small', 1, True), scale='wide', subject=True),
     {'carton', 'opened_packet'}, '（和 S211 事实相同、裁定不同：两种都接受，规则按小而近人统一）白发老人这个镜头手上也应该拿着盒装lymphloop'),
    ('S160 G03 抽滴管', obs('pull_dropper', ('bottle', 'in_hand', 'medium', 1, True), ('dropper', 'in_hand', 'medium', 1, True)),
     {'sealed_packet'}, '所有抽滴管的镜头动作都用同场景下的静态条包特写展示'),
    ('S161 G04 前景虚化滴管', obs('none', ('dropper', 'foreground_blurred', 'medium', 1, False), scale='close_up'),
     {'carton'}, '滴剂管的出现也是属于穿帮（没人拿着：按 R2 后半句在原位立纸盒；平放条包在 1753、1812、2114 三条片子都被画成立着）'),
    ('S167 G05 老人手边立着的瓶', obs('none', ('bottle', 'on_table_or_surface', 'medium', 1, True)),
     {'carton'}, '11-19 逐镜重做：纸盒立在原瓶位置，用户截图认可'),
    ('S168 G05 前景瓶和席卡', obs('none', ('bottle', 'foreground_blurred', 'large', 1, False), scale='close_up'),
     {'carton'}, '同上'),
    ('S169 G05 伸手拿桌上的瓶', obs('reach_grab', ('bottle', 'on_table_or_surface', 'medium', 1, True)),
     {'carton'}, '同上'),
    ('S170 G05 全景手里小瓶', obs('hold_show', ('bottle', 'in_hand', 'small', 1, True), scale='wide'),
     {'opened_packet', 'carton'}, 'G14这种中远景镜头，产品在画面中偏小的，可以改成拿着开封过的条包（用户自己的 G05 版是手里拿纸盒，也采用了）'),
    ('S176 G07 快递箱里提盒', obs('take_from_shipping_box', ('carton_box', 'inside_shipping_box', 'medium', 12, True), scale='close_up'),
     {'carton'}, 'G07需要修改的是从纸箱里抽出盒装的镜头'),
    ('S206 G12 远景拧小瓶', obs('twist_open_cap', ('bottle', 'in_hand', 'small', 1, True), scale='wide'),
     {'opened_packet'}, '中远景、产品偏小 -> 拿着开封过的条包'),
    ('S211 G14 茶杯旁小瓶', obs('none', ('bottle', 'on_table_or_surface', 'small', 1, True), scale='wide'),
     {'opened_packet'}, 'G14可以改为手拿条包（因为是中远景，产品偏小）'),
    ('142 S132 杯架里静置的瓶', obs('none', ('bottle', 'inside_open_container', 'medium', 1, False), scale='close_up'),
     {'carton'}, '11-22 线程3：车内杯架是家具不是产品容器，不能设计成开盒抽条包'),
    ('110 S056 从理发店抽屉拿瓶', obs('pick_up', ('bottle', 'inside_open_container', 'medium', 1, True)),
     {'carton'}, '11-22 线程3：家具抽屉里拿起来，拿起纸盒即可'),
    ('S217 G16 同事放下小盒', obs('place_down', ('carton_box', 'in_hand', 'medium', 1, True), scale='wide'),
     {'carton'}, 'G16 产品反复出现'),
    ('S219 G16 桌上一盒手里一盒', obs('pick_up', ('carton_box', 'on_table_or_surface', 'medium', 1, True), ('carton_box', 'in_hand', 'medium', 1, True)),
     {'carton'}, '用户 G16 版：桌上的2个绿白窄盒替换为1盒'),
    ('S222 G16 双手转盒看侧面', obs('turn_inspect', ('carton_box', 'in_hand', 'large', 1, True), scale='close_up'),
     {'carton'}, '同上'),
    ('S270 G19 开箱', obs('take_from_shipping_box', ('carton_box', 'inside_shipping_box', 'medium', 1, True)),
     {'carton'}, 'G19的开箱也需要重新生成'),
    ('S273 G20 拧瓶抽滴管', obs('pull_dropper', ('bottle', 'in_hand', 'medium', 1, True), ('dropper', 'in_hand', 'small', 1, True)),
     {'sealed_packet'}, '抽滴管 -> 静态条包特写'),
    ('S206b 远景里很小的滴管', obs('drip_or_dropper_use', ('dropper', 'in_hand', 'small', 1, True), scale='wide'),
     {'opened_packet'}, 'Flash 11-20 把 S206 看成小滴管：远景小滴管也走 R7'),
    ('服用', obs('ingest', ('dropper', 'near_mouth', 'medium', 1, True), scale='close_up'),
     {'opened_packet'}, '服用只有吸一种动作'),
    ('口袋', obs('take_from_pocket_or_bag', ('bottle', 'in_pocket_or_bag', 'medium', 1, True)),
     {'sealed_packet'}, '口袋只能掏条包'),
]


class RuleTable(unittest.TestCase):
    def test_user_rulings(self):
        import swap_design as sd
        wrong = []
        for name, o, forms, words in CASES:
            d = sd.decide(o)
            if d['form'] not in forms: wrong.append(f"{name}: got {d['form']} ({d['rule']}), user: {words}")
        self.assertEqual(wrong, [])

    def test_screened_but_not_observed_is_never_dropped(self):
        import swap_design as sd
        d = sd.decide(obs('none', scale='wide'), screened_competitor=True)
        self.assertEqual((d['rule'], d['form']), ('R0-筛查有观察无', 'opened_packet'))

    def test_one_product_except_shipping_box(self):
        import swap_design as sd
        d = sd.decide(CASES[12][1]); self.assertEqual(d['count'], 1); self.assertEqual(d['views'], ['carton_views', 'carton_hand'])
        self.assertIsNone(sd.decide(CASES[8][1])['count'])

    def test_dropper_shot_is_redesigned_never_kept(self):
        import swap_design as sd
        d = sd.decide(CASES[2][1]); self.assertEqual(d['action'], 'redesign'); self.assertEqual(d['views'], ['handheld_photo'])

    def test_skeleton_carries_the_design(self):
        import swap_design as sd
        rows = {s: {**sd.decide(o)} for s, o in (('S219', CASES[12][1]), ('S220', CASES[13][1]))}
        txt = sd.skeleton(rows, ['S219', 'S220'])
        self.assertTrue(txt.startswith('# views: carton_views,carton_hand\n'))
        for part in ('【生成目标】', '【参考素材职责】', '【主体与关系】', '【事件脚本】', '【保持一致】', '完全移除原片中的', '全程仅1盒'):
            self.assertIn(part, txt)
        import swap
        import re
        self.assertEqual(swap.negative_sentences(re.sub(r'【写[^】]*】', '', txt)), [])


class DesignCheck(unittest.TestCase):
    def setUp(self):
        import swap_design as sd
        self.sd = sd
        self.rows = {'S219': sd.decide(CASES[12][1]), 'S220': sd.decide(CASES[13][1]), 'S211': sd.decide(CASES[10][1])}

    def filled(self, ids):
        import re
        return re.sub(r'【写[^】]*】', '室内', self.sd.skeleton(self.rows, ids).replace(self.sd.SCALE_HOLE, '大小参照：产品和他手掌的大小关系同上一镜，手指张开的幅度不变。'))

    def test_filled_skeleton_passes(self):
        self.assertEqual(self.sd.check_against_design(self.filled(['S219']), self.rows), [])
        self.assertEqual(self.sd.check_against_design(self.filled(['S211']), self.rows), [])

    def test_wrong_form_or_images_are_caught(self):
        txt = self.filled(['S211']).replace('# views: opened_packet_hand', '# views: carton_views,carton_hand')
        self.assertTrue(any('views' in e for e in self.sd.check_against_design(txt, self.rows)))
        txt = self.filled(['S211']).replace('条包', '纸盒')
        errs = self.sd.check_against_design(txt, self.rows)
        self.assertTrue(any('没有条包' in e for e in errs) and any('没有纸盒' in e for e in errs))

    def test_old_phrases_are_caught(self):
        txt = self.filled(['S219']) + '\n请注意这不是视频编辑。'
        self.assertTrue(any('旧写法' in e for e in self.sd.check_against_design(txt, self.rows)))


if __name__ == '__main__':
    unittest.main()


class SegmentFiveSection(unittest.TestCase):
    """User 2026-11-20 "统一成五段式": every segment prompt of the main flow is the five-section layout, built from the
    design rows; the Gemini clone-seedance-video lint is enforced before anything is paid."""
    SEG = {'id': 'G16', 'shot_ids': ['S217', 'S218', 'S219'], 'in_frame': 0, 'out_frame': 300, 'duration_s': 10.0,
           'shot_times': [['S217', 0.0, 3.0], ['S218', 3.0, 6.0], ['S219', 6.0, 10.0]], 'product_shots': ['S217', 'S219'],
           'speech': [{'start': 3.2, 'end': 5.0, 'speaker': 'man', 'text': 'Lymphoria works.'}],
           'rewrites': {'dialogue': [{'original': 'Lymphoria', 'rewrite': 'LymphLoop'}], 'vo': []}}

    def setUp(self):
        import swap, swap_design as sd
        self.swap, self.sd = swap, sd
        self.design = {'S217': sd.decide(CASES[11][1]), 'S219': sd.decide(CASES[12][1])}

    def filled(self, design=True):
        import re
        return re.sub(r'【写[^】]*】', '室内', self.swap.prompt_stub(self.SEG, None, self.design if design else None).replace(self.sd.SCALE_HOLE, '大小参照：产品和他手掌的大小关系同上一镜，手指张开的幅度不变。'))

    def test_skeleton_carries_the_design_and_passes_the_lint(self):
        raw = self.filled(); body = self.swap.clean_prompt(raw)
        self.assertIn('# views: carton_views,carton_hand\n# design: S217,S219\n', raw)
        self.assertEqual(self.sd.five_section_problems(body, n_shots=3, speech=True), [])
        self.assertEqual(self.sd.check_against_design(raw, self.design), [])
        self.assertEqual(self.swap.param_sentences(body), [])
        self.assertIn('{LymphLoop works.}', body); self.assertNotIn('Lymphoria', body)
        self.assertNotRegex(body, r'S\d{3}|R\d')                      # shot and rule ids stay in comments
        self.assertEqual(len(self.sd.stages(body)), 3)

    def test_without_design_the_product_parts_are_slots(self):
        raw = self.swap.prompt_stub(self.SEG, None, None)
        self.assertIn('# views: 【写：按设计表】', raw); self.assertIn('swap_design.py observe + decide', raw)

    def test_lint_catches_what_the_gemini_skill_forbids(self):
        body = self.swap.clean_prompt(self.filled())
        self.assertTrue(any('结束时' in e for e in self.sd.five_section_problems(body.replace('结束时', '最后'), 3, True)))
        self.assertTrue(any('阶段' in e for e in self.sd.five_section_problems(body, 4, True)))
        self.assertEqual(self.sd.five_section_problems(body, 2, True), [])            # more stages than detected shots is fine
        self.assertTrue(any('花括号' in e for e in self.sd.five_section_problems(body.replace('{LymphLoop works.}', '“LymphLoop works.”'), 3, True)))
        self.assertTrue(any('镜头号' in e for e in self.sd.five_section_problems(body + '\nS217', 3, True)))
        self.assertTrue(self.swap.param_sentences(body + '\n竖版9:16画面，720P。'))
        self.assertTrue(self.swap.param_sentences(body + '\n全片 8 秒。'))
        self.assertEqual(self.sd.five_section_problems(body, 3, True, n_images=2), [])
        self.assertTrue(any('@图片3' in e for e in self.sd.five_section_problems(body, 3, True, n_images=3)))     # an image without a role
        self.assertTrue(any('只上传了 1' in e for e in self.sd.five_section_problems(body, 3, True, n_images=1)))  # @图片2 not uploaded
        merged = body.replace('@图片2仅用于', '@图片1和@图片2仅用于')
        self.assertTrue(any('一行只写一张图' in e for e in self.sd.five_section_problems(merged, 3, True, n_images=2)))

    def test_competitor_coverage_by_stage(self):
        body = self.swap.clean_prompt(self.filled())
        times = self.SEG['shot_times']
        self.assertEqual(self.swap.unreplaced(body, [{'t': 1.0}, {'t': 8.0}], times), [])
        self.assertEqual(self.swap.unreplaced(body, [{'t': 10.9}], times), [])          # held last frame past the last shot (2210 G03)
        self.assertEqual([x['t'] for x in self.swap.unreplaced(body, [{'t': 4.5}], times)], [4.5])      # S218 names no product
        self.assertEqual(len(self.swap.unreplaced(body.replace('完全移除原片中的', '去掉'), [{'t': 1.0}], times)), 1)

    def test_no_competitor_shot_is_not_given_a_carton(self):
        d = self.sd.decide(obs('none', scale='wide'))
        self.assertIsNone(d['form']); self.assertEqual(d['views'], [])

    def test_decide_rewrites_only_untouched_skeletons(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'prompts').mkdir(); (run/'plan').mkdir()
            other = {**self.SEG, 'id': 'G17'}
            self.swap.save(run/'plan/segments.json', {'fps': 30, 'segments': [self.SEG, other]})
            for s in (self.SEG, other): self.swap.write_stub(run, s, self.swap.prompt_stub(s, None, None))
            authored = run/'prompts/G17-a1.txt'; authored.write_text(authored.read_text() + '\n# 我写了一半')
            from unittest import mock
            with mock.patch.object(self.swap.comp, 'active', return_value=None):
                self.assertEqual(self.swap.rewrite_untouched_stubs(run, self.design), ['G16'])
            self.assertIn('# design: S217,S219', (run/'prompts/G16-a1.txt').read_text())
            self.assertIn('# 我写了一半', authored.read_text())


class CartonSegmentRouting(unittest.TestCase):
    def test_carton_segment_is_prepared_for_apimart_seedance_20(self):
        import tempfile, contextlib, io, re
        from unittest import mock
        import swap, swap_design as sd, apimart_wan
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_parallel_robustness import clip
        seg = {**SegmentFiveSection.SEG, 'speech': [], 'rewrites': {'dialogue': [], 'vo': []}}
        design = {'S217': sd.decide(CASES[11][1]), 'S219': sd.decide(CASES[12][1])}
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)/'run'; (run/'prompts').mkdir(parents=True); (run/'plan').mkdir()
            ref = clip(Path(td)/'G16.hq.mp4', 'gray', 440, frames=240, size='720x1280')
            swap.save(run/'state.json', {'source': str(ref), 'probe': {'video': {'width': 720, 'height': 1280}}, 'stages': {}})
            swap.save(run/'plan/segments.json', {'fps': 24, 'segments': [{**seg, 'out_frame': 240}]})
            swap.save(run/'plan/swap_design.json', {'shots': design})
            (run/'prompts/G16-a1.txt').write_text(re.sub(r'【写[^】]*】', '室内', swap.prompt_stub(seg, None, design).replace(__import__('swap_design').SCALE_HOLE, '大小参照：产品和他手掌的大小关系同上一镜，手指张开的幅度不变。')))
            a = type('A', (), {'run': str(run), 'segment': 'G16', 'attempt': 1, 'views': None, 'mute': None, 'clean_captions': False,
                               'aspect_ratio': None, 'candidates': 1, 'prepare_only': True})()
            with mock.patch.object(apimart_wan, 'credential', side_effect=AssertionError('prepare is offline')), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                swap.cmd_generate(a)
            job = swap.read(run/'jobs/G16-a1.json')
            self.assertEqual((job['model'], job['resolution'], job['aspect_ratio'], job['output_seconds']), ('seedance-2.0', '720p', '9:16', 10))
            self.assertNotIn('x100', job)                      # user 2026-11-20: APIMART seedance-2.0, not 100x by default
            final = Path(job['prompt_file']).read_text()
            self.assertEqual(swap.param_sentences(final), []); self.assertNotIn('9:16', final); self.assertNotIn('720', final)
            self.assertEqual([Path(r['path']).name for r in job['references'] if r['kind'] == 'image'], ['carton-views.jpg', 'carton-hand-scale.jpg'])


class SegmentRewrites(unittest.TestCase):
    """2026-11-20 film 2210: rewrites follow the words actually spoken in each segment."""
    def test_only_spoken_rewrites_and_split_pairs(self):
        import swap
        speech = [{'text': 'Esto es Lymphoria.'}, {'text': 'Un gotero completo debajo de la lengua por la mañana.'}]
        rew = [{'original': 'esto es Lymphoria / un gotero completo debajo de la lengua', 'rewrite': 'esto es LymphLoop / un sobre completo directo a la boca'},
               {'original': 'estilingia', 'rewrite': 'diente de león'}]
        kept, dropped = swap.segment_rewrites(rew, speech)
        self.assertEqual([r['original'] for r in kept], ['esto es Lymphoria', 'un gotero completo debajo de la lengua'])
        self.assertEqual([r['original'] for r in dropped], ['estilingia'])
        self.assertEqual(swap.segment_rewrites(rew, [])[1], [])                 # no transcript: keep all
        self.assertEqual(len(swap.segment_rewrites([{'original': 'Estilingia', 'rewrite': 'x'}], [{'text': 'ESTILINGIA, descompone'}])[0]), 1)


class PlanPieces(unittest.TestCase):
    def test_pieces_get_frames_and_share_the_screening(self):
        import tempfile, json, swap_design as sd
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'shots').mkdir(); (run/'plan').mkdir(); (run/'screen').mkdir()
            (run/'shots/shots.json').write_text(json.dumps({'fps': 30, 'source': 'x.mp4', 'shots': [{'id': 'S055', 'in_frame': 300, 'out_frame': 1200}]}))
            (run/'plan/segments.json').write_text(json.dumps({'fps': 30, 'segments': [
                {'id': 'G01', 'in_frame': 300, 'shot_times': [['S055.1', 0.0, 12.0]]}, {'id': 'G02', 'in_frame': 660, 'shot_times': [['S055.2', 0.0, 18.0]]}]}))
            (run/'screen/S055.json').write_text(json.dumps({'verdict': 'replace', 'raw': {'visible_evidence': 'amber bottle'}}))
            f = sd.planned_frames(run)
            self.assertEqual((f['S055.2']['in_frame'], f['S055.2']['out_frame']), (660, 1200))
            self.assertEqual(sd.screen_hint(run, 'S055.2'), 'amber bottle')


class IngredientInsert(unittest.TestCase):
    def test_ingredient_insert_does_not_invent_a_packet(self):
        import swap_design as sd
        facts = obs('none')
        facts['ingredient_only'] = True
        self.assertIsNone(sd.decide(facts, screened_competitor=True)['form'])
        facts['ingredient_only'] = False
        self.assertEqual(sd.decide(facts, screened_competitor=True)['form'], 'opened_packet')


class Film2210(unittest.TestCase):
    """2026-11-20 film 2210 findings."""
    def test_dropper_into_the_mouth_is_ingest_in_the_question(self):
        import swap_design as sd
        self.assertIn('under the tongue', sd.OBS_PROMPT); self.assertIn('NOT into the mouth', sd.OBS_PROMPT)

    def test_piece_without_competitor_is_no_product(self):
        import swap, swap_design as sd
        seg = {'id': 'G07', 'shot_ids': ['S058.2'], 'in_frame': 0, 'out_frame': 220, 'duration_s': 7.3, 'shot_times': [['S058.2', 0.0, 7.3]],
               'product_shots': ['S058.2'], 'speech': [], 'rewrites': {'dialogue': [], 'vo': []}}
        stub = swap.prompt_stub(seg, None, {'S058.2': sd.decide(obs('none', scale='wide'))})
        self.assertIn('# views: none', stub); self.assertNotIn('完全替换为', stub); self.assertNotIn('设计表还没覆盖', stub)


class DecideCommand(unittest.TestCase):
    def test_decide_runs_and_pieces_do_not_inherit_screening(self):
        import tempfile, json, argparse, swap_design as sd
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'plan').mkdir(); (run/'design/obs').mkdir(parents=True); (run/'screen').mkdir()
            (run/'plan/segments.json').write_text(json.dumps({'fps': 30, 'segments': [{'id': 'G07', 'shot_ids': ['S058.2', 'S059'], 'product_shots': ['S058.2', 'S059']}]}))
            (run/'screen/S058.json').write_text(json.dumps({'verdict': 'replace', 'raw': {'visible_evidence': 'bottle', 'competitor_visible': 'yes'}}))
            (run/'screen/S059.json').write_text(json.dumps({'verdict': 'replace', 'raw': {'visible_evidence': 'bottle', 'competitor_visible': 'yes'}}))
            for sid in ('S058.2', 'S059'):
                (run/'design/obs'/f'{sid}.json').write_text(json.dumps({'shot': sid, **obs('none', scale='wide')}))
            import swap
            with mock.patch.object(swap, 'rewrite_untouched_stubs', return_value=[]), mock.patch('builtins.print'):
                sd.cmd_decide(argparse.Namespace(run=str(run)))
            got = json.loads((run/'plan/swap_design.json').read_text())['shots']
            self.assertEqual(got['S058.2']['form'], 'opened_packet')       # nothing before it in its shot: screening fallback (宁多换不漏换)
            self.assertEqual(got['S059']['rule'], 'R0-筛查有观察无')        # a whole shot the merge marked keeps R0


class ReviewedChildShots(unittest.TestCase):
    def test_real_child_uses_own_screen_and_keeps_recall_fallback(self):
        import tempfile, json, argparse, swap_design as sd
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            for folder in ('plan', 'shots', 'screen', 'design/obs'):
                (run/folder).mkdir(parents=True, exist_ok=True)
            (run/'shots/shots.json').write_text(json.dumps({'shots': [{'id': 'S083.1'}]}))
            (run/'plan/segments.json').write_text(json.dumps({'segments': [{'id': 'G01', 'shot_ids': ['S083.1'], 'product_shots': ['S083.1']}]}))
            (run/'screen/S083.1.json').write_text(json.dumps({'verdict': 'replace', 'raw': {'visible_evidence': 'own child bottle', 'competitor_visible': 'yes'}}))
            (run/'design/obs/S083.1.json').write_text(json.dumps({'shot': 'S083.1', **obs('none')}))
            self.assertEqual(sd.screen_hint(run, 'S083.1'), 'own child bottle')
            import swap
            with mock.patch.object(swap, 'rewrite_untouched_stubs', return_value=[]), mock.patch('builtins.print'):
                sd.cmd_decide(argparse.Namespace(run=str(run)))
            got = json.loads((run/'plan/swap_design.json').read_text())['shots']['S083.1']
            self.assertEqual(got['rule'], 'R0-筛查有观察无')


class ExistingSupportSurface(unittest.TestCase):
    def test_dropper_redesign_does_not_invent_a_table_in_a_car(self):
        import swap_design as sd
        d = sd.decide(obs('pull_dropper', ('dropper', 'in_hand', 'medium', 1, True)))
        self.assertEqual(d['form'], 'sealed_packet')
        self.assertIn('捏住', d['redesign'])           # in a hand: held, no surface at all (film 1753)
        self.assertNotIn('桌上', d['redesign'])
        self.assertNotIn('桌上', sd.WHERE_ZH['on_table_or_surface'])


class UplinkGate(unittest.TestCase):
    def test_slow_tunnel_refuses_before_spending_retries(self):
        import tempfile, json, swap, hostd
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            v = Path(td)/'ref.mp4'; v.write_bytes(b'x' * 2_000_000)
            job = Path(td)/'G01-a1.json'; job.write_text(json.dumps({'references': [{'kind': 'video', 'path': str(v)}]}))
            import apimart_wan
            with mock.patch.object(apimart_wan, 'VIDEO_HOST', 'tunnel'):
                with mock.patch.object(hostd, 'status', return_value={'speed_kbps': 22.0}):
                    self.assertIn('100x', swap.uplink_too_slow([job]))
                with mock.patch.object(hostd, 'status', return_value={'speed_kbps': 400.0}):
                    self.assertIsNone(swap.uplink_too_slow([job]))
            # direct upload to APIMART (the default since 11-20): the tunnel speed does not matter (film 1753)
            with mock.patch.object(apimart_wan, 'VIDEO_HOST', 'apimart'), mock.patch.object(hostd, 'status', return_value={'speed_kbps': 22.0}):
                self.assertIsNone(swap.uplink_too_slow([job]))


class CartonCloseUpException(unittest.TestCase):
    def test_carton_views_alone_only_with_the_mark(self):
        import swap_design as sd
        self.assertTrue(sd.carton_views_ok(['carton_views', 'carton_hand'], ''))
        self.assertFalse(sd.carton_views_ok(['carton_views'], ''))
        self.assertFalse(sd.carton_views_ok(['carton_views'], '# carton-close-up\n'))   # withdrawn 11-20 (G16R16 proportions)
        self.assertFalse(sd.carton_views_ok(['carton_hand', 'carton_views'], '# carton-close-up\n'))
        self.assertTrue(sd.carton_views_ok(['opened_packet_hand'], ''))


class CaptionLinesFromFiveSection(unittest.TestCase):
    def test_braces_are_the_lines(self):
        import caption_burn as cb
        txt = '# views: x\n【事件脚本】\n阶段一：Carlos台词（怀疑，用西班牙语）：{¿me estás diciendo que las respuestas son unos sobres?} 结束时……\n# 原声 {no}\n'
        self.assertEqual(cb.prompt_lines(txt), ['¿me estás diciendo que las respuestas son unos sobres?'])
        self.assertEqual(cb.prompt_lines('镜头1：台词：“Hello.”'), ['Hello.'])


class PieceContinuity(unittest.TestCase):
    """2026-11-20 film 2210 S058: G06 ended inside a continuous wide shot and the bottle flashed back in G07."""
    def test_piece_continues_previous_piece(self):
        import tempfile, json, argparse, swap_design as sd, swap
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'plan').mkdir(); (run/'design/obs').mkdir(parents=True); (run/'screen').mkdir()
            (run/'plan/segments.json').write_text(json.dumps({'fps': 30, 'segments': [{'id': 'G06', 'shot_ids': ['S058.1'], 'product_shots': ['S058.1']},
                                                                                       {'id': 'G07', 'shot_ids': ['S058.2'], 'product_shots': ['S058.2']}]}))
            (run/'design/obs/S058.1.json').write_text(json.dumps({'shot': 'S058.1', **obs('hold_show', ('bottle', 'in_hand', 'medium', 1, True))}))
            (run/'design/obs/S058.2.json').write_text(json.dumps({'shot': 'S058.2', **obs('none', scale='wide')}))
            with mock.patch.object(swap, 'rewrite_untouched_stubs', return_value=[]), mock.patch('builtins.print'):
                sd.cmd_decide(argparse.Namespace(run=str(run)))
            got = json.loads((run/'plan/swap_design.json').read_text())['shots']
            self.assertEqual(got['S058.2']['form'], 'carton'); self.assertIn('同镜接续', got['S058.2']['rule'])

    def test_in_shot_cut_lands_on_the_dissolve(self):
        import speech_map
        sents = [{'start': 410.0, 'end': 431.6}, {'start': 435.2, 'end': 437.0}]
        pieces, _, _ = speech_map.split_long_shots([(420.8, 440.73)], sents, 14.9, transitions=[(431.63, 0.4), (433.0, 0.06)])
        self.assertAlmostEqual(pieces[0][1], 431.63, places=2)
        pieces, _, _ = speech_map.split_long_shots([(420.8, 440.73)], sents, 14.9)
        self.assertAlmostEqual(pieces[0][1], 433.4, places=1)                     # no picture change inside: the middle


class CartonCloseupDesignConsistency(unittest.TestCase):
    def test_close_up_exception_is_also_accepted_by_design_gate(self):
        import swap_design as sd
        row={'form':'carton','views':['carton_views','carton_hand'],'count':1}
        body='# views: carton_views\n# design: S001\n# carton-close-up\n【生成目标】纸盒【参考素材职责】@图片1用于纸盒，不采用背景【主体与关系】只有1纸盒【事件脚本】阶段一：手握纸盒。结束时纸盒正面朝镜头。【保持一致】全程仅1盒纸盒'
        self.assertEqual(sd.check_against_design(body,{'S001':row}),[])
        self.assertTrue(sd.check_against_design(body.replace('# carton-close-up\n',''),{'S001':row}))


class RewriteHeardFuzzy(unittest.TestCase):
    """2026-11-20 film 1753: Pro and Whisper spell differently; exact matching dropped all four rewrites of the film."""
    def test_fuzzy_and_split_lines(self):
        import swap
        f = swap._fold
        ok = [("Lymphoria se agota constantemente", "LymphLoop se agota constantemente", "Linforia se agota constantemente.", True),
              ("Lymphoria se agota constantemente", "LymphLoop se agota constantemente", "La elaboran en lotes pe-", False),
              ("el fresco espinoso reactiva el bombeo", "el diente de león reactiva el bombeo", "El fresno espinoso reactiva el bombeo.", True),
              ("hoy, el paquete de 5 frascos tiene un 70% de descuento", "hoy, el paquete de 5 cajas tiene un 70% de descuento",
               "Hoy, el paquete de cinco frascos tiene un 70 por...", True),
              ("hoy, el paquete de 5 frascos tiene un 70% de descuento", "hoy, el paquete de 5 cajas tiene un 70% de descuento",
               "un 70% de descuento y una garantía de devolución del dinero de 30 días.", False),
              ("estas son las gotas", "estos son los sobres", "Cuatro hierbas.", False),
              ("Medio gotero por la mañana y por la noche, debajo de la lengua.", "Un sobre por la mañana y por la noche, directo a la boca.",
               "Estas son las gotas para el drenaje linfático de Lymphoria. Tienen las cuatro hierbas, han sido probadas por terceros", False),
              ("Medio gotero por la mañana y por la noche, debajo de la lengua.", "Un sobre por la mañana y por la noche, directo a la boca.",
               "Medio gotero por la mañana y por la noche debajo de la lengua. Saben a miel.", True)]
        for o, r, sp, want in ok:
            self.assertEqual(swap.rewrite_heard(o, r, f(sp)), want, o)
        kept, dropped = swap.segment_rewrites([{'original': 'Lymphoria se agota constantemente', 'rewrite': 'LymphLoop se agota constantemente'}],
                                              [{'text': 'Linforia se agota constantemente.'}])
        self.assertEqual((len(kept), len(dropped)), (1, 0))


class R0NeedsASeenCompetitor(unittest.TestCase):
    """2026-11-20 film 1753: shots in a segment only for their lines got an opened packet from R0."""
    def test_speech_only_shot_gets_no_product(self):
        import tempfile, json, argparse, swap_design as sd, swap
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)
            for folder in ('plan', 'shots', 'screen', 'design/obs', 'detect'): (run/folder).mkdir(parents=True, exist_ok=True)
            (run/'shots/shots.json').write_text(json.dumps({'shots': [{'id': 'S140'}, {'id': 'S154'}, {'id': 'S243'}]}))
            (run/'plan/segments.json').write_text(json.dumps({'segments': [{'id': 'G05', 'shot_ids': ['S140', 'S154', 'S243'], 'product_shots': ['S140', 'S154', 'S243']}]}))
            (run/'screen/S140.json').write_text(json.dumps({'verdict': 'replace', 'raw': {'competitor_visible': 'no', 'speech_mentions_product': 'yes'}}))
            (run/'screen/S154.json').write_text(json.dumps({'verdict': 'replace', 'raw': {'competitor_visible': 'yes'}}))
            (run/'detect/overrides.json').write_text(json.dumps({'S243': {'kinds': ['visual', 'vo']}}))
            for sid in ('S140', 'S154', 'S243'): (run/'design/obs'/f'{sid}.json').write_text(json.dumps({'shot': sid, **obs('none')}))
            with mock.patch.object(swap, 'rewrite_untouched_stubs', return_value=[]), mock.patch('builtins.print'):
                sd.cmd_decide(argparse.Namespace(run=str(run)))
            got = json.loads((run/'plan/swap_design.json').read_text())['shots']
            self.assertIsNone(got['S140']['form'])
            self.assertEqual(got['S154']['rule'], 'R0-筛查有观察无')
            self.assertEqual(got['S243']['rule'], 'R0-筛查有观察无')


class BagThenCarton(unittest.TestCase):
    """2026-11-20 film 1753 S132 -> S133: taken out of a handbag, then standing on the counter as a carton."""
    def test_take_from_bag_follows_next_carton(self):
        import tempfile, json, swap_design as sd
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'plan').mkdir()
            (run/'plan/segments.json').write_text(json.dumps({'segments': [{'id': 'G03', 'shot_ids': ['S131', 'S132', 'S133']}]}))
            out = {'S132': {'rule': 'R5-口袋', 'form': 'sealed_packet', 'views': ['handheld_photo']},
                   'S133': {'rule': 'R9-纸盒', 'form': 'carton', 'views': ['carton_views', 'carton_hand'], 'template': 'carton'}}
            sd.follow_into_carton(run, out)
            self.assertEqual((out['S132']['rule'], out['S132']['form'], out['S132']['views']), ('R5b-包里拿出纸盒', 'carton', ['carton_views', 'carton_hand']))
            out2 = {'S132': {'rule': 'R5-口袋', 'form': 'sealed_packet'}, 'S133': {'rule': '无竞品', 'form': None}}
            sd.follow_into_carton(run, out2)
            self.assertEqual(out2['S132']['form'], 'sealed_packet')


class SameObjectAcrossReverseShots(unittest.TestCase):
    """2026-11-22 film 1603578481269031 G02: a carton in the man's hand in five shots; the two reverse shots with the
    bottle on the frame edge went R0 -> an opened packet, so the held thing changed shape at every cut."""
    def rows(self):
        carton = {'rule': 'R9-纸盒', 'form': 'carton', 'placement': 'in_hand', 'action': 'keep', 'redesign': '', 'views': ['carton_views', 'carton_hand'], 'template': 'carton', 'count': 1}
        r0 = {'rule': 'R0-筛查有观察无', 'form': 'opened_packet', 'placement': 'in_hand', 'action': 'redesign', 'redesign': '…', 'views': ['opened_packet_hand'],
              'template': 'packet', 'count': 1, 'removals': ['原片里人手边的小件竞品']}
        return carton, r0

    def test_r0_between_held_cartons_stays_a_carton(self):
        import tempfile, json, swap_design as sd
        carton, r0 = self.rows()
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'plan').mkdir()
            ids = ['S060', 'S061', 'S062', 'S063', 'S064', 'S065', 'S066']
            (run/'plan/segments.json').write_text(json.dumps({'segments': [{'id': 'G02', 'shot_ids': ids}, {'id': 'G03', 'shot_ids': ['S070', 'S071']}]}))
            out = {sid: dict(r0 if sid in ('S063', 'S066') else carton) for sid in ids}
            out.update(S070=dict(r0), S071={'rule': '无竞品', 'form': None})          # no held product near it: R0 stays
            sd.follow_held_object(run, out)
            for sid in ('S063', 'S066'):
                self.assertEqual((out[sid]['rule'], out[sid]['form'], out[sid]['views'], out[sid]['template']),
                                 ('R0c-同物接续', 'carton', ['carton_views', 'carton_hand'], 'carton'))
                self.assertIn('同一个纸盒', out[sid]['redesign']); self.assertEqual(out[sid]['removals'], ['原片里人手边的小件竞品'])
            self.assertEqual((out['S070']['rule'], out['S070']['form']), ('R0-筛查有观察无', 'opened_packet'))
            self.assertEqual(sd.views_of([out[s] for s in ids]), ['carton_views', 'carton_hand'])      # one form in the whole segment

    def test_nearest_product_on_a_table_does_not_pull_r0_along(self):
        import tempfile, json, swap_design as sd
        carton, r0 = self.rows()
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'plan').mkdir()
            (run/'plan/segments.json').write_text(json.dumps({'segments': [{'id': 'G01', 'shot_ids': ['S001', 'S002']}]}))
            out = {'S001': {**carton, 'placement': 'standing'}, 'S002': dict(r0)}
            sd.follow_held_object(run, out)
            self.assertEqual(out['S002']['rule'], 'R0-筛查有观察无')


class OwnIngredientsInSpanish(unittest.TestCase):
    """2026-11-20 film 1753: amor de hortelano / trébol rojo are our Cleavers / Red Clover."""
    def test_aliases(self):
        import speech_gate as sg, json
        facts = json.loads((Path(__file__).resolve().parents[1]/'assets/product/profile.json').read_text())['facts']
        self.assertTrue(sg.own_ingredient('amor de hortelano', facts))
        self.assertTrue(sg.own_ingredient('Trébol rojo', facts))
        self.assertTrue(sg.own_ingredient('raíz de bardana', facts))
        self.assertFalse(sg.own_ingredient('raíz de la reina', facts))
        self.assertFalse(sg.own_ingredient('fresno espinoso', facts))


class CaptionResidueIsCaptionWords(unittest.TestCase):
    """2026-11-20 film 1753 G23: bottle labels in the caption band are not leftover captions."""
    def test_caption_word(self):
        import caption_lama as c
        caps = ['el paquete de 5 frascos tiene un 70% de descuento', 'y una garantía de devolución del dinero de 30 días']
        self.assertFalse(c.caption_word('Dronage', caps)); self.assertFalse(c.caption_word('bmplinta', caps))
        self.assertTrue(c.caption_word('garantia de devoluci', caps)); self.assertTrue(c.caption_word('descuento', caps))


class DropperShotsHoldOrStand(unittest.TestCase):
    """2026-11-20 film 1753: a 'flat' packet in a dropper shot stood up on its own in 4 of 4 shots."""
    def test_hand_holds_packet_or_carton_stands(self):
        import swap_design as sd
        d = sd.decide(obs('pull_dropper', ('bottle', 'in_hand', 'large', 1, True), ('dropper', 'in_hand', 'large', 1, True)))
        self.assertEqual((d['rule'], d['form'], d['placement']), ('R2-滴管', 'sealed_packet', 'in_hand'))
        self.assertIn('捏住', d['redesign'])
        d = sd.decide(obs('none', ('bottle', 'on_table_or_surface', 'large', 1, False), ('dropper', 'on_table_or_surface', 'medium', 1, False)))
        self.assertEqual((d['rule'], d['form'], d['placement']), ('R2-滴管', 'carton', 'standing'))
        self.assertEqual(d['views'], ['carton_views', 'carton_hand'])


class FlatPacketRefused(unittest.TestCase):
    """A stick pack left lying on a surface stood up on three films (1753, 1812 G15, 2114 G05): refused at prepare."""
    def test_flat_packet_on_a_surface_is_refused(self):
        import swap
        for text in ('喝完后把同一支条包放下，条包袋面朝上整支平贴床头柜面。', '一支未开封条包平躺在承托面上，长边贴着台面，正面朝上。',
                     '右手把条包放在桌面上。', '床头柜上平放着一支已开封条包。'):
            hard, _ = swap.copy_risks(text)
            self.assertTrue(any('自己立着' in h for h in hard), text)
        for text in ('右手拇指和食指一直捏着一支已开封条包下半部。', '条包是扁平柔软的薄袋，始终在手里或平放，不自己立着。',
                     '原来瓶子的位置立着1个纸盒，底面着地。'):
            hard, _ = swap.copy_risks(text)
            self.assertFalse(any('自己立着' in h for h in hard), text)

    def test_lone_dropper_becomes_a_standing_carton(self):
        import swap_design as sd
        d = sd.decide({'items': [{'kind': 'dropper', 'where': 'on_table', 'size': 'small', 'count': 1, 'look': ''}], 'hand_action': 'none', 'shot_scale': 'close_up', 'who_handles': 'nobody'})
        self.assertEqual((d['form'], d['placement']), ('carton', 'standing'))


class SameObjectAcrossRules(unittest.TestCase):
    """2026-11-22: one bottle in a dialogue became carton / opened packet / carton (1912 S049-S050, 998 three groups);
    a bottle taken from a pocket became a carton in the next shot (1429 S063/S064)."""
    def row(self, rule, form, placement='in_hand', **obs):
        views = {'carton': ['carton_views', 'carton_hand'], 'opened_packet': ['opened_packet_hand'], 'sealed_packet': ['handheld_photo']}[form]
        return {'rule': rule, 'form': form, 'placement': placement, 'action': 'keep', 'redesign': '', 'views': views, 'count': 1, 'observation': obs}

    def test_r7_between_cartons_becomes_the_carton(self):
        import swap_design as sd
        out = {'S083': self.row('R9-纸盒', 'carton'), 'S084': self.row('R7-小而近人', 'opened_packet'), 'S088': self.row('R9-纸盒', 'carton'),
               'S150': self.row('R7-小而近人', 'opened_packet')}                          # far from any carton: stays
        sd.unify_held_object(None, out)
        self.assertEqual((out['S084']['rule'], out['S084']['form'], out['S084']['views']), ('R7d-同物接续', 'carton', ['carton_views', 'carton_hand']))
        self.assertEqual(out['S150']['form'], 'opened_packet')

    def test_pocket_turns_the_held_carton_next_to_it_into_a_packet(self):
        import swap_design as sd
        out = {'S063': self.row('R5-口袋', 'sealed_packet'), 'S064': self.row('R9-纸盒', 'carton'), 'S066': self.row('R9-纸盒', 'carton'),
               'S070': self.row('R9-纸盒', 'carton', placement='standing'), 'S080': self.row('R9-纸盒', 'carton'),
               'S090': self.row('R5-口袋', 'sealed_packet', retrieval_container='bag'), 'S091': self.row('R9-纸盒', 'carton')}
        sd.unify_held_object(None, out)
        self.assertEqual([out[k]['form'] for k in ('S064', 'S066')], ['sealed_packet', 'sealed_packet'])
        self.assertEqual(out['S064']['rule'], 'R5d-口袋同物')
        self.assertEqual(out['S070']['form'], 'carton')       # standing on a table: not the thing in the hand
        self.assertEqual(out['S080']['form'], 'carton')       # too far from the pocket shot
        self.assertEqual(out['S091']['form'], 'carton')       # from a bag: a carton fits (R5b)


class AfterSipAndReviewedNoProduct(unittest.TestCase):
    def test_after_a_sip_the_held_packet_stays_opened(self):
        import swap_design as sd
        row = SameObjectAcrossRules.row
        out = {'S227': row(None, 'R1-服用', 'opened_packet'), 'S228': row(None, 'R7-小而近人', 'opened_packet'), 'S229': row(None, 'R9-纸盒', 'carton'),
               'S240': row(None, 'R9-纸盒', 'carton')}
        out['S228']['form'] = 'sealed_packet'
        sd.unify_held_object(None, out)
        self.assertEqual([out[k]['form'] for k in ('S228', 'S229')], ['opened_packet', 'opened_packet'])
        self.assertEqual(out['S240']['form'], 'carton')

    def test_reviewed_no_physical_product_wins(self):
        import tempfile, json, swap_design as sd
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'detect').mkdir()
            (run/'detect/overrides.json').write_text(json.dumps({'S211': {'decision': 'replace', 'kinds': ['vo'], 'physical_competitor': False}}))
            self.assertTrue(sd.reviewed_no_product(run, 'S211')); self.assertFalse(sd.reviewed_no_product(run, 'S212'))


class CaptionBox(unittest.TestCase):
    def test_caption_sized_boxes_only(self):
        import swap
        self.assertEqual(swap.parse_erase(['G04:0.30,0.70,0.20,0.05@14.5-end'])['G04'][0][:4], (0.30, 0.70, 0.20, 0.05))
        self.assertGreater(0.30, swap.CAPTION_BOX_MAX_H)        # a box this tall (a bottle, a face) is refused in prepare_jobs


class SplitIngredientLine(unittest.TestCase):
    def test_name_split_by_asr_and_line_cut_by_a_shot(self):
        # 2026-11-22 film 2836804316692183 G15 / G16
        import swap
        f = swap._fold
        self.assertTrue(swap.rewrite_heard('Stillingia breaks down that hardened sludge trapped inside his tissue so it can actually move',
                                           'Burdock root breaks down that hardened sludge trapped inside his tissue so it can actually move', f('Still, India breaks down that hard...')))
        self.assertTrue(swap.rewrite_heard('Prickly Ash restarts the circulation and flow that keep it draining',
                                           'Dandelion restarts the circulation and flow that keep it draining', f('Quickly, Ash restarts the circulation')))
        self.assertFalse(swap.rewrite_heard('Prickly Ash restarts the circulation and flow that keep it draining',
                                            'Dandelion restarts the circulation and flow that keep it draining', f('the circulation and flow that keep it draining')))


class ReviewedNoProductAtSubmit(unittest.TestCase):
    def test_reference_residue_in_a_reviewed_shot_needs_only_the_removal(self):
        # 2026-11-22 film 1659373095741717 S024: a dried drop on a shelf, reviewed as no physical product
        import tempfile, json, swap
        from unittest import mock
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'detect').mkdir(); (run/'jobs').mkdir()
            (run/'detect/overrides.json').write_text(json.dumps({'S024': {'decision': 'replace', 'physical_competitor': False}}))
            vid = run/'ref.mp4'; vid.write_bytes(b'v'); prompt = run/'p.txt'
            prompt.write_text('【生成目标】x【参考素材职责】@视频1用于复刻，完全移除原片中的架上琥珀色残液及字幕。产品的大小不沿用原片里被替换容器的大小和手的张开幅度，按事件脚本里每一镜写的大小参照来画。【主体与关系】x【事件脚本】阶段一：架子特写。结束时灰尘圆印留着。【保持一致】x')
            job = run/'jobs/G01-a1.json'
            job.write_text(json.dumps({'references': [{'kind': 'video', 'path': str(vid)}], 'prompt_file': str(prompt),
                                       'reference_competitor': {'video_sha256': swap.sha(vid), 'blocking': [{'t': 2.0, 'kind': 'dose_unit'}]}}))
            seg = {'id': 'G01', 'shot_times': [['S024', 0.0, 4.0]]}
            self.assertEqual(swap.reference_competitors(run, [seg], [job]), {})
            job.write_text(json.dumps({**json.loads(job.read_text()), 'reference_competitor': {'video_sha256': swap.sha(vid), 'blocking': [{'t': 4.0, 'kind': 'bottle'}]}}))
            seg2 = {'id': 'G01', 'shot_times': [['S012', 0.0, 1.9], ['S024', 1.9, 3.12]]}       # 4.0 s is the held tail of the last shot
            self.assertEqual(swap.reference_competitors(run, [seg2], [job]), {})
            (run/'detect/overrides.json').write_text(json.dumps({}))
            self.assertIn('G01', swap.reference_competitors(run, [seg], [job]))


class ScreenProductPage(unittest.TestCase):
    """User 2026-11-22: a phone or computer screen showing the competitor's page shows our product page instead."""
    def test_screen_only_and_screen_with_a_held_product(self):
        import swap_design as sd
        base = {'hand_action': 'none', 'shot_scale': 'close_up', 'who_handles': 'nobody'}
        d = sd.decide({**base, 'items': [], 'on_screen': 'computer'})
        self.assertEqual((d['rule'], d['form'], d['views']), ('S1-屏幕商品页', 'desktop_page', ['desktop_page']))
        d = sd.decide({**base, 'items': [], 'on_screen': 'phone'})
        self.assertEqual(d['views'], ['phone_page'])
        held = sd.decide({'items': [{'kind': 'bottle', 'where': 'in_hand', 'size': 'medium', 'count': 1, 'within_reach_of_person': True, 'look': ''}],
                          'hand_action': 'hold_show', 'shot_scale': 'medium', 'who_handles': 'woman', 'on_screen': 'phone'})
        self.assertEqual(held['views'][-1], 'phone_page'); self.assertIn('屏幕上的竞品商品页', held['removals'])
        self.assertEqual(sd.decide({**base, 'items': []})['rule'], '无竞品')


class OwnContainer(unittest.TestCase):
    def test_r3_only_when_a_unit_is_taken_out(self):
        import swap_design as sd
        d = sd.decide(obs('take_from_container', ('bottle', 'inside_open_container', 'medium', 1, True)), True)
        self.assertEqual(d['rule'], 'R3-取出')
        for act in ('none', 'pick_up', 'reach_grab'):
            d = sd.decide(obs(act, ('bottle', 'inside_open_container', 'medium', 1, True)), True)
            self.assertNotEqual(d['rule'], 'R3-取出', act)



class ScaleReference(unittest.TestCase):
    """User 2026-11-25: the product grows and shrinks from shot to shot; every product stage names an in-shot size
    reference (hand opening, face, a cup or phone beside it) and the product keeps one size across shots."""
    def setUp(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
        import swap_design
        self.sd = swap_design

    BODY = ('【生成目标】x\n【参考素材职责】@视频1用于复刻。\n【主体与关系】厨房。\n【事件脚本】\n'
            '阶段一：中景，她从包里拿出纸盒。大小参照：纸盒比她的手掌略宽，拇指和四指张开到一整掌宽才包住盒身左右两侧。结束时纸盒在她右手里。\n'
            '阶段二：切至她的近景，她看着镜头。结束时她笑了。\n'
            '【保持一致】全程仅1盒；产品在每一镜里和人脸、手、身边物件的大小关系都一样，不忽大忽小；画面不显示字幕。')

    def test_filled_prompt_passes(self):
        self.assertEqual(self.sd.scale_problems(self.BODY), [])

    def test_product_stage_without_reference_is_refused(self):
        errs = self.sd.scale_problems(self.BODY.replace('大小参照：纸盒比她的手掌略宽，拇指和四指张开到一整掌宽才包住盒身左右两侧。', ''))
        self.assertTrue(any('阶段一' in e and '大小参照' in e for e in errs))

    def test_stage_without_product_needs_no_reference(self):
        self.assertFalse(any('阶段二' in e for e in self.sd.scale_problems(self.BODY)))

    def test_keep_sentence_required(self):
        errs = self.sd.scale_problems(self.BODY.replace('不忽大忽小', '大小不变'))
        self.assertTrue(any('忽大忽小' in e for e in errs))

    def test_stub_holes_carry_the_rule(self):
        self.assertIn('大小参照：【写', self.sd.SCALE_HOLE)
        self.assertIn('不忽大忽小', self.sd.SCALE_KEEP)

    def test_reference_video_role_says_size_is_not_copied(self):
        # user 2026-11-25: put the in-shot size reference into first generation too; with @视频1 attached the model keeps
        # the replaced container's size and the hand's opening unless the @视频1 role says not to
        errs = self.sd.five_section_problems(self.BODY, video=True)
        self.assertTrue(any('不沿用' in e for e in errs))
        ok = self.BODY.replace('@视频1用于复刻。', '@视频1用于复刻。' + self.sd.SCALE_VIDEO)
        self.assertFalse(any('不沿用' in e for e in self.sd.five_section_problems(ok, video=True)))
        self.assertFalse(any('不沿用' in e for e in self.sd.five_section_problems(self.BODY.replace('@视频1用于复刻。', ''), video=False)))

    def test_competitor_is_not_a_size_reference(self):
        # user 2026-11-25: "在当前镜头内找参照物不会是拿竞品做参照物吧，要找人脸，找镜头内离产品最近的实际物体做参照物"
        good = '大小参照：纸盒比她的手掌略宽，拇指和四指张开到一整掌宽才包住盒身左右两侧。'
        for ref in ('大小参照：纸盒比原来的滴剂瓶大一圈，比她的手掌略宽。', '大小参照：纸盒和原片里一样大，比她的手掌略宽。'):
            errs = self.sd.scale_problems(self.BODY.replace(good, ref))
            self.assertTrue(any('不能当参照' in e for e in errs), ref)
        body = self.BODY.replace('@视频1用于复刻。', '@视频1用于复刻，完全移除原片中的绿色玻璃瓶、白色小勺及字幕。')
        errs = self.sd.scale_problems(body.replace(good, '大小参照：纸盒比旁边的绿色玻璃瓶矮一截，比她的手掌略宽。'))
        self.assertTrue(any('绿色玻璃瓶' in e for e in errs))
        self.assertEqual(self.sd.scale_problems(body), [])
        self.assertIn('人脸', self.sd.SCALE_HOLE)
        self.assertIn('不拿竞品', self.sd.SCALE_HOLE)

    def test_skeleton_role_line_carries_the_sentence(self):
        self.assertIn('不沿用', self.sd.SCALE_VIDEO)
        self.assertIn('大小参照', self.sd.SCALE_VIDEO)
