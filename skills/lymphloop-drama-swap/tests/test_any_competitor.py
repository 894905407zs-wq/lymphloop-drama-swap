"""Any competitor supplement (user, 2026-11-11): the run's own competitor profile drives detection, QA, the final screen
and the watermark word; every target segment is generated once and used; the reference picture is never repaired."""
import re
import argparse, json, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import competitor as comp  # noqa: E402
import flash_screen as fs  # noqa: E402
import swap  # noqa: E402
import watermark_brand as wb  # noqa: E402

CAPSULES = {'type': 'lymphloop_competitor_profile', 'source': 'flash', 'reviewed': True,
            'brand_names': ['VitaGlow Lymph+'], 'product_name': 'lymph support capsules', 'form': 'hard_capsule',
            'secondary_forms': ['softgel'], 'container_look': 'white HDPE bottle with an orange cap and an orange label',
            'dose_unit_look': 'two-tone orange/white capsules', 'ingestion_actions': ['tips two capsules into the palm, swallows with water'],
            'hard_cases': ['capsules loose in an open palm without the bottle'], 'ingredients_named': [],
            'watermark_text': '@vitaglow', 'anchors': [], 'anchor_images': []}


def write_profile(run, data):
    p = comp.path(run); p.parent.mkdir(parents=True, exist_ok=True); p.write_text(json.dumps(data)); return p


class Profile(unittest.TestCase):
    def test_lymphoria_keeps_the_proven_pattern_and_others_get_four_letter_pieces(self):
        self.assertEqual(comp.fuzzy_pattern('LYMPHORIA'), r'(PHOR|HORI|RHOR|MPHO(?!O))')
        pat = comp.fuzzy_pattern('VITAGLOW')
        self.assertEqual(pat, '(VITA|ITAG|TAGL|AGLO|GLOW)')
        # pieces shared with our own brand are dropped, or the stamped LYMPHLOOP would read as the competitor
        self.assertNotIn('LYMP', comp.fuzzy_pattern('LYMPHAGUMMY')); self.assertIn('HAGU', comp.fuzzy_pattern('LYMPHAGUMMY'))

    def test_watermark_word_names_and_facts(self):
        self.assertEqual(comp.watermark_target(CAPSULES), 'VITAGLOW')
        self.assertEqual(comp.watermark_target({**CAPSULES, 'watermark_text': ''}), 'VITAGLOW')   # falls back to the brand
        self.assertEqual(comp.names({'brand_names': ['VitaGlow Lymph+', 'yesnap']}), {'vitaglowlymph'})
        # a sister product of our own brand: the watermark word is never YESNAP
        self.assertEqual(comp.watermark_target({'brand_names': ['yesnap', 'Peach Pout'], 'watermark_text': ''}), 'PEACH')

    def test_anchor_times_are_read_as_minutes_and_seconds(self):
        self.assertEqual([comp.anchor_seconds(x) for x in ('02:59', '4:17', '1:02:03', '27.5', 12, 'soon')], [179.0, 257.0, 3723.0, 27.5, 12.0, None])
        text = comp.facts_text(CAPSULES)
        self.assertIn('hard_capsule', text); self.assertIn('capsules loose in an open palm', text)

    def test_an_unreviewed_profile_is_not_used(self):
        with tempfile.TemporaryDirectory() as td:
            write_profile(td, {**CAPSULES, 'reviewed': False})
            self.assertIsNone(comp.load(td)); self.assertEqual(comp.active(td)['brand_names'], ['Lymphoria'])
            write_profile(td, CAPSULES); self.assertEqual(comp.active(td)['form'], 'hard_capsule')


class FlashUsesTheRunProfile(unittest.TestCase):
    def raw(self, **kw):
        return {'competitor_visible': 'no', 'brand_text': [], 'ingestion_action': 'none', 'speech_present': False,
                'speech_mentions_product': 'no', 'speech_transcript': '', **kw}

    def test_any_competitor_dose_form_is_a_conflict_but_plain_water_is_not(self):
        for action in ('capsule_or_tablet', 'softgel', 'gummy', 'powder_or_drink_mix', 'dropper'):
            self.assertEqual(fs.verdict_for(self.raw(ingestion_action=action))[0], 'replace', action)
        self.assertEqual(fs.verdict_for(self.raw(ingestion_action='other'))[0], 'keep')

    def test_prompts_describe_this_runs_competitor(self):
        with tempfile.TemporaryDirectory() as td:
            write_profile(td, CAPSULES)
            self.assertIn('VitaGlow', fs.screen_prompt(td)); self.assertIn('loose in a palm', fs.screen_prompt(td))
            self.assertIn('VitaGlow', fs.qa_prompt('visual', td))
            # speech naming the competitor is a conflict even with the target name in it; the target alone is not
            self.assertFalse(fs._target_speech('switch from VitaGlow Lymph+ to yesnap LymphLoop', td))
            self.assertTrue(fs._target_speech('it is called yesnap LymphLoop', td))


class WatermarkWord(unittest.TestCase):
    def tearDown(self): wb.set_target('LYMPHORIA')

    def test_set_target_moves_every_matcher(self):
        wb.set_target('VITAGLOW')
        self.assertTrue(wb.is_watermark_text('@VITAGL0W'))          # faint misread still has VITA/ITAG/TAGL
        self.assertFalse(wb.is_watermark_text('@LYMPHORIA'))
        self.assertTrue(wb.caption_line('VITAGLOW 30 CAPSULES')); self.assertFalse(wb.caption_line('@ VITA GLOW'))
        wb.set_target('LYMPHORIA'); self.assertTrue(wb.is_watermark_text('LYMRHORIA'))


class CompetitorCommand(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.run = Path(self.td.name)/'RUN'; self.run.mkdir()
        (self.run/'state.json').write_text(json.dumps({'stages': {'prepare': {'working_master': {'path': '/x.mp4'}}}}))

    def tearDown(self): self.td.cleanup()

    def accept(self, **kw):
        a = argparse.Namespace(**{'run': str(self.run), 'accept': True, 'note': 'looked at 6 anchors', 'force': False, 'out_of_scope_ok': False, **kw})
        swap.cmd_competitor(a)

    def test_accept_marks_the_profile_reviewed_and_commits_the_stage(self):
        write_profile(self.run, {**CAPSULES, 'reviewed': False})
        self.accept()
        st = json.loads((self.run/'state.json').read_text())['stages']['competitor']
        self.assertEqual((st['form'], st['watermark_target']), ('hard_capsule', 'VITAGLOW'))
        self.assertTrue(comp.load(self.run)['reviewed'])

    def test_out_of_scope_forms_wait_for_the_user(self):
        write_profile(self.run, {**CAPSULES, 'form': 'other', 'reviewed': False})
        with self.assertRaisesRegex(SystemExit, 'tell the user'): self.accept()
        self.accept(out_of_scope_ok=True)

    def test_screen_needs_an_accepted_profile(self):
        with self.assertRaisesRegex(SystemExit, 'accept the competitor profile'):
            swap.cmd_screen(argparse.Namespace(run=str(self.run), ids=None))


class OneShotReferences(unittest.TestCase):
    def test_caption_cleaned_references_are_gone(self):
        r = subprocess.run([sys.executable, str(SCRIPTS/'swap.py'), 'generate', '--run', '/nonexistent', '--prepare-only', '--clean-captions'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2); self.assertIn('--clean-captions', r.stderr)

    def test_prompt_stub_names_the_competitor_and_asks_for_the_inventory(self):
        seg = {'id': 'G03', 'shot_ids': ['S010'], 'in_frame': 0, 'out_frame': 150, 'duration_s': 5.0,
               'shot_times': [['S010', 0.0, 5.0]], 'product_shots': ['S010'], 'speech': [],
               'rewrites': {'dialogue': [], 'vo': []}}
        stub = swap.prompt_stub(seg, CAPSULES)
        self.assertIn('VitaGlow Lymph+', stub); self.assertIn('竞品盘点', stub); self.assertIn('闭唇轻抿', stub)
        self.assertNotIn('--clean-captions', stub)


class InkGate(unittest.TestCase):
    """Old brand text left on a light prop (Metabolae card, 2026-11-11): only the ink is inpainted."""
    def test_ink_on_a_card_is_masked_and_skin_and_background_are_not(self):
        import numpy as np, region_inpaint as ri
        roi = np.zeros((90, 170, 3), np.uint8); roi[:] = (40, 30, 90)            # dark red cloth
        roi[20:80, 30:150] = (225, 228, 230)                                       # the card
        roi[42:54, 50:120] = (150, 150, 152)                                       # grey printing
        roi[60:90, 0:40] = (120, 150, 200)                                         # a finger over the card corner
        m = ri.gate_mask(roi, 'ink')
        self.assertGreater(int((m[42:54, 50:120] > 0).mean() * 100), 95)
        self.assertEqual(int(m[0:15].max()), 0); self.assertEqual(int(m[62:90, 0:28].max()), 0)
        self.assertEqual(int(m[24:36, 36:144].max()), 0)                           # blank card stays
        self.assertIsNone(ri.gate_mask(np.full((60, 60, 3), 30, np.uint8), 'ink'))  # no light surface: untouched
        self.assertEqual(ri.parse_jobs(['1,2,3,4:ink'], ['5,6'])[0][-1], 'ink')


class SpokenCaptions(unittest.TestCase):
    """Auto-decaption of a generated clip must not erase packaging print (Lymphoria 293, 2026-11-11)."""
    def test_one_short_shared_word_in_garbled_print_is_not_a_caption(self):
        import caption_clean as cc
        words = cc.spoken_words(["If that's you, it's not fat you're carrying.", "You used to be the man they called when something was heavy."])
        for text in ('NOT HE DIDIE 10tel', 'Hoe Ironing. The', '30 PACKETS', 'yesnap LymphLoop'):
            self.assertFalse(cc.spoken_caption(text, words), text)
        for text in ('YOU USED TO BE', 'CALLED WHEN', 'LYMPCALLED WHEN', 'HEAVY', "IF THAT'S YOU"):
            self.assertTrue(cc.spoken_caption(text, words), text)

    def test_a_line_listed_under_the_next_segment_counts_while_it_is_heard_here(self):
        plan = {'fps': 24, 'segments': [
            {'id': 'G06', 'in_frame': 5178, 'out_frame': 5475, 'speech': [{'start': 9.1, 'end': 11.9, 'text': 'Your wife stops looking.'}]},
            {'id': 'G07', 'in_frame': 5475, 'out_frame': 5563, 'speech': [{'start': -0.13, 'end': 1.77, 'text': "If that's you, it's not fat."},
                                                                            {'start': 1.87, 'end': 3.7, 'text': 'It is waste.'}]}]}
        self.assertEqual(swap.spoken_in_window(plan, plan['segments'][0]), ['Your wife stops looking.', "If that's you, it's not fat."])
        self.assertEqual(len(swap.spoken_in_window(plan, plan['segments'][1])), 2)   # the previous line ended well before this segment


class SoundUnderTheGeneratedPicture(unittest.TestCase):
    """Seedance edit keeps the source mouths but its sound drifts (2026-11-11: 6 of 18 segments, 4 of them 1.0 s early)."""
    def test_lag_of_the_same_speech_shifted_earlier(self):
        import numpy as np, sound_sync
        rng = np.random.default_rng(3); src = np.repeat(rng.random(120), 10).astype(np.float32)        # 12 s of syllable-like bursts
        self.assertEqual(sound_sync.lag_ms(src, src)[0], 0)
        early = np.concatenate([src[100:], np.zeros(100, np.float32)])                                 # everything 1.0 s early
        self.assertEqual(sound_sync.lag_ms(src, early)[0], -1000)
        self.assertEqual(sound_sync.lag_ms(src[:80], src[:80])[0], 0)                                  # short clips do not crash
        self.assertEqual(sound_sync.lag_ms(src[:20], src[:20]), (0, 0.0))

    def test_the_generated_picture_keeps_its_own_sound_and_the_drift_is_recorded(self):
        """User, 2026-11-11 / 2026-11-21: picture and sound are generated together and stay together, rewritten line or not;
        no automatic switch to the source sound; the source sound under on-screen dialogue only with --no-mouth."""
        a = argparse.Namespace(sound='candidate', keep_source_picture=False, audio_only=False, no_mouth=False)
        with tempfile.TemporaryDirectory() as td:
            master = Path(td)/'m.mp4'; master.write_bytes(b'x'); cand = Path(td)/'G01-a1-c1.mp4'; cand.write_bytes(b'y')
            state = {'stages': {'prepare': {'working_master': {'path': str(master)}}}}
            plan = {'fps': 24}; seg = {'id': 'G01', 'in_frame': 48, 'duration_s': 4.0, 'speech': [{'start': 0, 'end': 1, 'text': 'hi'}], 'rewrites': {'dialogue': [], 'vo': []}}
            probe = {'audio': {'codec_name': 'aac'}, 'video': {'width': 480}, 'video_duration': 4.0}
            import sound_sync
            with mock.patch.object(swap, 'working_master', return_value=master), mock.patch.object(swap.media, 'probe', return_value=probe), \
                    mock.patch.object(sound_sync, 'drift', return_value={'ms': -1000, 'corr': 0.9, 'ok': False}), \
                    mock.patch.object(sound_sync, 'with_source_sound', side_effect=lambda c, m, start, out: Path(out).write_bytes(b'z') and out) as mux:
                got = swap.sound_for(Path(td), state, plan, seg, {}, cand, a)
                self.assertEqual(got['sound'], 'candidate'); self.assertNotIn('path', got); mux.assert_not_called()
                self.assertEqual(got['sound_drift']['ms'], -1000); self.assertIn('1000 ms early', got['sound_note'])
                a.sound = None                                                  # lines unchanged, voice off: still its own sound
                got = swap.sound_for(Path(td), state, plan, seg, {}, cand, a)
                self.assertEqual(got['sound'], 'candidate'); self.assertNotIn('sound_auto', got); mux.assert_not_called()
                seg['kinds'] = ['dialogue']; a.sound = 'source'                 # source sound under talking mouths: refused
                with self.assertRaises(SystemExit): swap.sound_for(Path(td), state, plan, seg, {}, cand, a)
                a.no_mouth = True                                               # the agent looked: no mouth speaks
                got = swap.sound_for(Path(td), state, plan, seg, {}, cand, a)
                self.assertEqual((got['sound'], Path(got['path']).name, mux.call_args[0][2]), ('source', 'G01-a1-c1.srcaudio.mp4', 2.0))
                a.sound = 'candidate'
                self.assertEqual(swap.sound_for(Path(td), state, plan, seg, {'provider': 'local_splice'}, cand, a), {})   # a splice keeps its own sound

    def test_splice_finds_sound_that_is_not_the_pictures_own(self):
        """User 2026-11-21: Codex laid first-generation sound 0.2-1.5 s off its own mouths and over other pictures."""
        a1, fix = '/r/G08-a1-c1.mp4', '/r/G08-fix-c1.mp4'
        pieces = [{'path': a1, 'start_s': 0.0, 'out_frames': 48, 'fit': None}, {'path': fix, 'start_s': 0.0, 'out_frames': 48, 'fit': None},
                  {'path': '/r/G08-a1-c1.captioned.mp4', 'start_s': 4.0, 'out_frames': 48, 'fit': None}]
        self.assertEqual(swap.foreign_sound(pieces, 24, 0, None, []), [])                                     # each picture with its own
        self.assertEqual(swap.foreign_sound(pieces, 24, 0, a1, []), [[2.0, 4.0]])                            # a1 under the fix shot
        self.assertEqual(swap.foreign_sound(pieces, 24, 0, None, [{'path': a1, 'from': [4.0, 5.0], 'at': 4.2}]), [[4.2, 5.2]])   # shifted 0.2 s
        self.assertEqual(swap.foreign_sound(pieces, 24, 0, None, [{'path': a1, 'from': [4.5, 5.5], 'at': 4.5}]), [])            # same moment
        slow = [dict(pieces[0]), {'path': fix, 'start_s': 0.0, 'out_frames': 24, 'fit': 24}]
        self.assertEqual(swap.foreign_sound(slow, 24, 12, a1, []), [[2.0, 3.5]])                              # retimed shot and held tail

    def test_the_prompt_template_writes_every_line_for_regeneration(self):
        seg = {'id': 'G02', 'shot_ids': ['S066'], 'in_frame': 0, 'out_frame': 228, 'duration_s': 9.5, 'shot_times': [['S066', 0.0, 9.5]], 'product_shots': ['S066'],
               'speech': [{'start': 0.57, 'end': 1.67, 'speaker': 'man B', 'text': 'Lymphoria.'}, {'start': 1.67, 'end': 2.67, 'speaker': 'man B', 'text': 'Four herbs.'}],
               'rewrites': {'dialogue': [{'original': 'Lymphoria.', 'rewrite': 'LymphLoop.'}], 'vo': []}}
        body = '\n'.join(l for l in swap.prompt_stub(seg, CAPSULES).splitlines() if not l.startswith('#'))
        self.assertIn('{LymphLoop.}', body); self.assertIn('{Four herbs.}', body); self.assertNotIn('Lymphoria', body)   # the old word never reaches the model
        self.assertIn('说话口型节奏', body); self.assertIn('阶段一：', body); self.assertNotIn('秒', body)    # five-section, no timestamps (11-20)
        for sec in ('【生成目标】', '【参考素材职责】', '【主体与关系】', '【事件脚本】', '【保持一致】'): self.assertIn(sec, body)
        self.assertEqual(swap.negative_sentences(re.sub(r'【写[^】]*】', '', body)), [])                 # the template itself is positive
        seg['speech'] = []; seg['rewrites'] = {'dialogue': [], 'vo': []}
        self.assertNotIn('台词只有上面写出的这些', swap.prompt_stub(seg, CAPSULES))


class WordTimings(unittest.TestCase):
    def test_largest_shift_matches_shared_words_in_order(self):
        import words
        src = [{'w': 'Keep', 'a': 0.0, 'b': 0.3}, {'w': 'They', 'a': 1.56, 'b': 1.7}, {'w': 'make', 'a': 1.72, 'b': 1.9}, {'w': 'Thanks.', 'a': 9.2, 'b': 9.4}]
        gen = [{'w': 'They', 'a': 0.56, 'b': 0.7}, {'w': 'make', 'a': 0.70, 'b': 0.9}, {'w': 'thanks', 'a': 8.24, 'b': 8.4}]
        self.assertEqual(words.largest_shift(src, gen), -1.02)             # Nuflos G05: the sound came 1 s early
        self.assertEqual(words.largest_shift(src, src), 0.0)
        self.assertIsNone(words.largest_shift(src, [{'w': 'packet', 'a': 1.0, 'b': 1.2}]))


class OfficialPromptRules(unittest.TestCase):
    """User, 2026-11-11, after reading the official Seedance 2.5 prompt guide: positive descriptions only, decimal
    timestamps rounded up, the reference video goes out without captions, generation is video-reference mode."""

    def test_decimal_timestamps_round_up(self):
        text, n = swap.ceil_timestamps('镜头1（0.00–0.87 秒）；3.33–4.68秒；在 2.5 秒处；8 g 条包；2–4.21 秒；0–2 秒；10 × 7 × 13 cm')
        self.assertEqual(text, '镜头1（0–1 秒）；4–5秒；在 3 秒处；8 g 条包；2–5 秒；0–2 秒；10 × 7 × 13 cm')
        self.assertEqual(n, 6)
        self.assertEqual(swap.ceil_timestamps('at 1.2s he turns; 4.0-6.5 s')[0], 'at 2s he turns; 4-7 s')

    def test_only_captions_and_audio_may_be_negated(self):
        bad = swap.negative_sentences('镜头1：他捏着条包。画面里看不到产品，不要添加任何产品。嘴唇不张开；条包绝不自己立着。')
        self.assertEqual(bad, ['画面里看不到产品，不要添加任何产品', '嘴唇不张开', '条包绝不自己立着'])
        ok = '镜头1：他捏着条包，其余保持不变。台词：“I don\'t need drops, never.”不要字幕。不要背景音乐。请注意这不是视频编辑。'
        self.assertEqual(swap.negative_sentences(ok), [])

    def test_prepare_refuses_a_prompt_with_negative_picture_wording(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td)/'run'; (run/'prompts').mkdir(parents=True); (run/'plan').mkdir()
            swap.save(run/'state.json', {'source': str(Path(td)/'missing.mp4'), 'stages': {}})
            swap.save(run/'plan/segments.json', {'fps': 24, 'segments': [{'id': 'G01', 'in_frame': 0, 'out_frame': 48, 'duration_s': 2.0, 'kinds': ['visual'],
                                                                           'rewrites': {'dialogue': [], 'vo': []}}]})
            (run/'prompts/G01-a1.txt').write_text('# views: none\n【生成目标】\n复刻@视频1的厨房画面。\n\n【参考素材职责】\n@视频1用于复刻人物和厨房，完全移除原片字幕。产品的大小不沿用原片里被替换容器的大小和手的张开幅度，按事件脚本里每一镜写的大小参照来画。\n\n'
                                                  '【主体与关系】\n厨房台面。\n\n【事件脚本】\n阶段一：中景。他把条包放在台面上，条包不得自己立着。大小参照：条包比旁边的手机短一截、窄得多。结束时条包在台面上。\n\n【保持一致】\n产品在每一镜里和人脸、手、身边物件的大小关系都一样，不忽大忽小；画面不显示字幕。\n')
            a = type('A', (), {'run': str(run), 'segment': 'G01', 'attempt': 1, 'views': None, 'mute': None, 'aspect_ratio': None, 'candidates': 1, 'prepare_only': True})()
            with self.assertRaisesRegex(SystemExit, '正向描述[\\s\\S]*条包不得自己立着'):
                swap.cmd_generate(a)

    def test_watermark_boxes_follow_the_reference_clip(self):
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'watermark').mkdir()
            master = run/'master.mp4'; master.write_bytes(b'x')
            swap.save(run/'watermark/scan.json', {'source': {'path': str(master)}, 'detections': [
                {'frame': 100, 'classification': 'floating_overlay', 'bbox': [10, 20, 30, 8]},
                {'frame': 100, 'classification': 'printed_label_keep', 'bbox': [0, 0, 5, 5]},
                {'frame': 300, 'classification': 'floating_overlay', 'bbox': [1, 1, 1, 1]}]})
            with mock.patch.object(swap.media, 'probe', return_value={'video': {'width': 360, 'height': 640}}):
                got = swap.watermark_boxes(run, {'in_frame': 90, 'out_frame': 150}, 30, 24, (720, 1280))
            self.assertEqual(got, {8: [[20, 40, 60, 16]]})          # (100-90)/30 s -> frame 8 at 24 fps, boxes scaled x2


class ReferenceWatermarkBoxes(unittest.TestCase):
    def test_held_copies_of_label_and_caption_boxes_are_dropped(self):
        # Lymphoria G09, 2026-11-11: the bottle label's brand word was a 'floating_overlay' and its 0.8 s hold landed
        # on the man's forearm in the next shot; LaMa erased the arm in the reference.
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); (run/'watermark').mkdir(); src = run/'m.mp4'; src.write_bytes(b'x')
            dets = [{'frame': 100, 'classification': 'floating_overlay', 'bbox': [137, 259, 95, 19]},                       # label, read by OCR: kept
                    {'frame': 101, 'classification': 'floating_overlay', 'bbox': [137, 259, 95, 19], 'synthetic': 'fade_pad'},  # its hold: dropped
                    {'frame': 102, 'classification': 'floating_overlay', 'bbox': [335, 10, 14, 68], 'synthetic': 'fade_pad'},   # vertical edge mark's hold: kept
                    {'frame': 103, 'classification': 'floating_overlay', 'bbox': [10, 10, 200, 60], 'synthetic': 'gap_fill'}]   # thick line at the top: not a watermark
            swap.save(run/'watermark/scan.json', {'source': {'path': str(src)}, 'detections': dets})
            with mock.patch.object(swap.media, 'probe', return_value={'video': {'width': 360, 'height': 640}}):
                got = swap.watermark_boxes(run, {'in_frame': 100, 'out_frame': 110}, 24, 24, (720, 1280))
            self.assertEqual(sorted(got), [0, 2])
            self.assertEqual(got[2], [[670, 20, 28, 136]])


    def test_pieces_of_the_mark_read_on_the_reference_are_masked(self):
        import caption_clean as cc, watermark_brand as wb
        with tempfile.TemporaryDirectory() as td:
            run = Path(td); clip = run/'G02.hq.ref.mp4'; clip.write_bytes(b'x')
            ocr = {'detections': [{'frame': 10, 'text': '@LУMPE SRI', 'bbox': [107, 205, 121, 21]},          # faint mark, Cyrillic У
                                  {'frame': 10, 'text': 'LYMPHORIA', 'bbox': [276, 522, 190, 38]},           # bottle label in caption position: the OCR pass handles it
                                  {'frame': 10, 'text': 'HERBS', 'bbox': [100, 100, 80, 20]}]}
            with mock.patch.object(swap, 'watermark_target', return_value='LYMPHORIA'), mock.patch.object(wb, 'edge_scan', return_value={'detections': []}), \
                 mock.patch.object(cc, '_scan', return_value=ocr), mock.patch.object(swap, 'hard_cuts', return_value=[]), \
                 mock.patch.object(swap.media, 'probe', return_value={'video_duration': 1.0}):
                got = swap.watermark_boxes(run, {'in_frame': 0, 'out_frame': 24}, 24, 24, (720, 1280), clip=clip)
            self.assertIn([107, 205, 121, 21], got[10])
            self.assertTrue(all(b[1] < 300 for boxes in got.values() for b in boxes))        # only the mark and its held copies
            self.assertGreater(len(got), 10)                                                 # held before and after the hit


class BrandStampsOnGeneratedPicture(unittest.TestCase):
    def test_label_and_caption_word_boxes_are_not_stamped_on_a_generated_segment(self):
        import watermark_brand as wb
        stamps = {'5': [{'box': [335, 10, 14, 68], 'orient': 'v_down'},        # the vertical mark at the edge
                        {'box': [113, 259, 120, 22], 'orient': 'h'},           # brand word on the bottle label
                        {'box': [98, 424, 136, 23], 'orient': 'h'}]}           # brand word alone in a caption
        seg = {'source_in': 0, 'source_out': 10, 'out_in': 0, 'out_out': 10}
        tl = lambda pid: {'width': 360, 'height': 640, 'segments': [{**seg, 'picture_id': pid}]}
        kept = wb.remap_source_stamps({'stamps': stamps}, tl('G01'))
        self.assertEqual([d['bbox'] for d in kept], [[335, 10, 14, 68]])
        self.assertEqual(len(wb.remap_source_stamps({'stamps': stamps}, tl(None))), 3)   # the original picture keeps every stamp


class LamaCaptionMask(unittest.TestCase):
    def test_the_mask_is_the_padded_text_line(self):
        import caption_lama as cl
        m = cl.line_mask((1280, 720, 3), [[211, 852, 293, 33]])
        ys, xs = m.nonzero()
        self.assertEqual((xs.min(), xs.max() + 1, ys.min(), ys.max() + 1), (197, 518, 840, 899))

    def test_the_window_is_a_square_inside_the_frame(self):
        import caption_lama as cl
        m = cl.line_mask((1280, 720, 3), [[40, 1200, 640, 40]])
        x, y, side = cl.window(m)
        self.assertEqual(side, 720); self.assertEqual((x, y), (0, 560))

    def test_a_caption_and_a_far_watermark_get_a_window_each(self):
        # Lymphoria G06, 2026-11-11: caption at y 852 and the vertical watermark in the top right corner in one mask;
        # the single 720 px window ended at y 860 and the caption stayed on 80 frames of the reference.
        import caption_lama as cl, numpy as np
        m = cl.line_mask((1280, 720, 3), [[149, 848, 417, 49], [671, 21, 28, 135]])
        parts = cl.groups(m)
        self.assertEqual(len(parts), 2)
        self.assertTrue(np.array_equal(np.maximum.reduce(parts), m))             # every masked pixel is in some part
        for sub in parts:
            x, y, side = cl.window(sub); ys, xs = sub.nonzero()
            self.assertTrue(x <= xs.min() and xs.max() < x + side and y <= ys.min() and ys.max() < y + side)
        near = cl.line_mask((1280, 720, 3), [[149, 848, 417, 49], [200, 700, 300, 60]])
        self.assertEqual(len(cl.groups(near)), 1)                                # label line right above the caption: one pass

    def test_a_mask_taller_than_a_window_is_tiled_and_still_covered(self):
        import caption_lama as cl, numpy as np
        m = np.zeros((1280, 720), np.uint8); m[100:1100, 300:340] = 255
        parts = cl.groups(m)
        self.assertGreater(len(parts), 1)
        self.assertTrue(np.array_equal(np.maximum.reduce(parts), m))
        for sub in parts:
            x, y, side = cl.window(sub); ys, xs = sub.nonzero()
            self.assertTrue(y <= ys.min() and ys.max() < y + side)

    def test_a_caption_box_is_not_held_across_a_cut(self):
        import caption_lama as cl
        scan = {'width': 720, 'height': 1280, 'frame_count': 20, 'detections': [{'frame': 9, 'text': 'lymphatic', 'bbox': [269, 700, 201, 74]}]}
        self.assertEqual(sorted(cl.held_boxes(scan)[0]), [6, 7, 8, 9, 10, 11, 12])
        self.assertEqual(sorted(cl.held_boxes(scan, cuts=[10])[0]), [6, 7, 8, 9])          # frame 10 opens the next shot
        self.assertEqual(sorted(cl.held_boxes(scan, cuts=[8, 10])[0]), [8, 9])

    def test_text_left_inside_a_cleaned_area_is_reported(self):
        import caption_lama as cl
        with tempfile.TemporaryDirectory() as td:
            ocr = Path(td)/'fake_scan'
            ocr.write_text('#!/bin/sh\nprintf \'{"width":720,"height":1280,"frame_count":3,"detections":['
                           '{"frame":0,"text":"AND YOUR OWN FAMILY","confidence":1,"bbox":[151,852,415,37]},'
                           '{"frame":1,"text":"lymphatic","confidence":1,"bbox":[269,300,201,74]},'
                           '{"frame":2,"text":"AND YOUR OWN FAMILY","confidence":1,"bbox":[151,852,415,37]}]}\' > "$2"\n'); ocr.chmod(0o755)
            held = {0: [[149, 848, 417, 49]], 1: [[149, 848, 417, 49]]}          # frame 2 was never masked, frame 1's text is elsewhere
            with mock.patch.object(cl.cc, 'scanner', return_value=ocr):
                left = cl.survivors(Path(td)/'clean.mp4', held, td)
            self.assertEqual(left, {0: ['AND YOUR OWN FAMILY']})

    def test_extra_boxes_reach_the_model_and_untouched_frames_pass_through(self):
        import caption_lama as cl, numpy as np, media_pipeline as media
        with tempfile.TemporaryDirectory() as td:
            src = Path(td)/'in.mp4'; out = Path(td)/'out.mp4'
            media.ff(['-y', '-f', 'lavfi', '-i', 'color=c=gray:s=180x320:r=24:d=0.5', '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono',
                      '-shortest', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(src)])
            ocr = Path(td)/'fake_scan'                                   # stands in for the Vision OCR binary: no text found
            ocr.write_text('#!/bin/sh\nprintf \'{"width":180,"height":320,"frame_count":12,"detections":[]}\' > "$2"\n'); ocr.chmod(0o755)
            seen = []
            def fake(frames, masks):
                seen.append(int((masks[0] > 0).sum())); return [np.full_like(frames[0], 200)]
            with mock.patch.object(cl, 'inpaint_batch', side_effect=fake), mock.patch.object(cl.cc, 'scanner', return_value=ocr):
                report = cl.clean(src, out, extra={3: [[10, 10, 40, 10]], 4: [[10, 10, 40, 10]]})
            self.assertEqual((report['frames'], report['frames_cleaned'], report['watermark_frames']), (12, 2, 2))
            self.assertEqual(len(seen), 2); self.assertTrue(all(n > 0 for n in seen)); self.assertTrue(out.is_file())


if __name__ == '__main__':
    unittest.main()
