"""User 2026-11-14: generated pictures must not be patched (贴图) to hide goofs."""
import json, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import region_inpaint as ri  # noqa: E402


class NoPatchingGeneratedPictures(unittest.TestCase):
    def timeline(self, d):
        src = Path(d) / 'master.mp4'; src.write_bytes(b'')
        segs = [{'out_in': 0, 'out_out': 100, 'picture_id': None, 'path': '/x/run/master-clean.mp4'},
                {'out_in': 100, 'out_out': 200, 'picture_id': 'G01', 'path': '/x/run/generation/results/G01-a1-c1.mp4'},
                {'out_in': 200, 'out_out': 300, 'picture_id': 'G02', 'path': '/x/run/master-clean.mp4'}]
        Path(str(src) + '.timeline.json').write_text(json.dumps({'segments': segs}))
        return src

    def test_generated_ranges_only_generation_results(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(ri.generated_ranges(self.timeline(d)), [(100, 200, 'G01')])

    def test_whole_box_fill_on_generated_picture_refused(self):
        with tempfile.TemporaryDirectory() as d:
            jobs = ri.parse_jobs(['10,10,20,20'], ['150,160'])
            bad = ri.forbidden_jobs(jobs, ri.generated_ranges(self.timeline(d)))
            self.assertEqual([pid for _, pid in bad], ['G01'])

    def test_text_gates_and_source_pictures_allowed(self):
        with tempfile.TemporaryDirectory() as d:
            ranges = ri.generated_ranges(self.timeline(d))
            self.assertEqual(ri.forbidden_jobs(ri.parse_jobs(['10,10,20,20:purple'], ['150,160']), ranges), [])
            self.assertEqual(ri.forbidden_jobs(ri.parse_jobs(['10,10,20,20:ink'], ['150,160']), ranges), [])
            self.assertEqual(ri.forbidden_jobs(ri.parse_jobs(['10,10,20,20'], ['20,60']), ranges), [])
            self.assertEqual(ri.forbidden_jobs(ri.parse_jobs(['10,10,20,20'], ['210,260']), ranges), [])

    def test_no_timeline_no_check(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / 'clip.mp4'; src.write_bytes(b'')
            self.assertEqual(ri.generated_ranges(src), [])


class CopyRiskLint(unittest.TestCase):
    def setUp(self):
        import swap; self.swap = swap

    def test_spoon_in_description_is_hard(self):
        hard, _ = self.swap.copy_risks('镜头2（3–5 秒）：她右手用金属小茶匙搅着杯里的咖啡，然后把茶匙放到台面上。')
        self.assertTrue(hard)

    def test_spoon_inside_spoken_line_ignored(self):
        hard, _ = self.swap.copy_risks("男士台词：“One scoop, then you are done.” 他把条包举在胸前。")
        self.assertEqual(hard, [])

    def test_packet_set_on_palm_warns(self):
        _, warn = self.swap.copy_risks('镜头4（11–12 秒）：女士摊开的手掌特写，掌心上平躺着同一支青绿色条包，正面朝上。')
        self.assertTrue(any('第 1 类' in w[0] for w in warn))

    def test_hands_cover_product_warns(self):
        _, warn = self.swap.copy_risks('银白短发女士的双手覆在纸盒的顶盖上，把它轻轻推回她手里。')
        self.assertTrue(any('第 3 类' in w[0] for w in warn))

    def test_clean_prompt_passes(self):
        hard, warn = self.swap.copy_risks('镜头1（0–3 秒）：她右手拇指和食指捏着已开条包的下半部，在杯口上方轻轻一挤；左手握着杯子。')
        self.assertEqual((hard, warn), ([], []))


class MoreLint(unittest.TestCase):
    def setUp(self):
        import swap; self.swap = swap

    def test_falling_carton_warns(self):
        _, warn = self.swap.copy_risks('镜头3（6–8 秒）：一个青绿纸盒平稳落在台面上。')
        self.assertTrue(any('第 7 类' in w[0] for w in warn))

    def test_packet_count_missing_warns(self):
        _, warn = self.swap.copy_risks('青绿色密封条包正面朝上横放在纸盒前方')
        self.assertTrue(any(w[0] == '数量' for w in warn))
        _, warn = self.swap.copy_risks('一支青绿色密封条包正面朝上横放在纸盒前方')
        self.assertFalse(any(w[0] == '数量' for w in warn))

    def test_serrated_leaves_warn(self):
        _, warn = self.swap.copy_risks('锯齿绿蒲公英叶近景，叶子在木台上自然铺展')
        self.assertTrue(any(w[0] == '成分植物' for w in warn))

    def test_foreign_ingredient_hits(self):
        with tempfile.TemporaryDirectory() as d:
            hits = self.swap.foreign_ingredient_hits(Path(d), '画外旁白台词：“Rutin rounds out the four featured ingredients.”')
            self.assertEqual(hits, ['rutin'])
            self.assertEqual(self.swap.foreign_ingredient_hits(Path(d), '台词：“Dandelion and burdock root.”'), [])

    def test_unscripted_reference_words(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d); (run/'plan/words').mkdir(parents=True)
            (run/'plan/words/G01.json').write_text(json.dumps([
                {'w': "Here's", 'a': 0.0, 'b': 0.3}, {'w': 'what', 'a': 0.3, 'b': 0.5}, {'w': 'actually', 'a': 0.5, 'b': 0.9},
                {'w': 'happens', 'a': 0.9, 'b': 1.3}, {'w': 'Old', 'a': 2.0, 'b': 2.4}, {'w': 'line', 'a': 2.4, 'b': 2.8}]))
            body = '画外旁白台词：“If you have swelling, this helps.”'
            seg = {'id': 'G01'}
            self.assertEqual(len(self.swap.unscripted_reference_words(run, seg, [], body)), 6)
            self.assertEqual(self.swap.unscripted_reference_words(run, seg, [(0.0, 3.0)], body), [])


class SlackLint(unittest.TestCase):
    def test_speech_slack(self):
        import swap
        need, slack = swap.speech_slack('画外旁白台词：“This is yesnap LymphLoop, every knuckle back to normal.”', 9)
        self.assertGreater(slack, 3)
        need, slack = swap.speech_slack('台词：“One packet, every morning. It tastes great. Try it.”', 5)
        self.assertLess(slack, 2)
        self.assertEqual(swap.speech_slack('全片安静，只有环境声。', 8), (0.0, 0.0))


class NoContentCuts(unittest.TestCase):
    def test_content_cuts(self):
        import swap
        plan = {'G01': {'in_frame': 0, 'out_frame': 300}, 'G02': {'in_frame': 300, 'out_frame': 600}, 'G03': {'in_frame': 600, 'out_frame': 900}}
        sel = {'G01': {'clip_in': 0.0, 'clip_out': 9.8}, 'G02': {'clip_in': 2.0, 'clip_out': 10.0}, 'G03': {'picture': 'source'}}
        self.assertEqual([g for g, _, _ in swap.content_cuts(plan, sel, 30)], ['G02'])
        self.assertEqual(swap.content_cuts(plan, sel, '30/1', {'G02'}), [])


class SizeAndCountLint(unittest.TestCase):
    def test_centimetres_are_refused(self):
        """User 2026-11-20: "模型不理解尺寸，要用比例来说明"."""
        import swap
        hard, _ = swap.copy_risks('纸盒高 13、宽 10、厚 7 厘米，立在台面上')
        self.assertTrue(any('比例' in h for h in hard))
        hard, _ = swap.copy_risks('正面的高约为宽的一倍三，和他的手掌一样宽，比水杯略高')
        self.assertEqual(hard, [])
        hard, _ = swap.copy_risks('台词：“30 días”')
        self.assertEqual(hard, [])

    def test_vague_count_warns(self):
        import swap
        _, warn = swap.copy_risks('后方同向排布几盒纸盒')
        self.assertTrue(any(w[0] == '数量' for w in warn))


class PhoneticLint(unittest.TestCase):
    def test_ipa_and_reading_notes_are_hard(self):
        import swap
        hard, _ = swap.copy_risks('品牌读作 /ˈlɪmf luːp/，镜头里他举着一支条包')
        self.assertTrue(hard)
        hard, _ = swap.copy_risks('品牌的 Lymph 和 Loop 两段连读，末尾闭唇收在 p')
        self.assertTrue(hard)
        hard, _ = swap.copy_risks('镜头1（0–3 秒）：他右手拇指和食指捏着一支条包举在胸前，正面朝镜头')
        self.assertEqual(hard, [])


class TurnLint(unittest.TestCase):
    def test_packet_turning_warns(self):
        import swap
        _, warn = swap.copy_risks('她把条包翻转过来看背面')
        self.assertTrue(any('第 16 类' in w[0] for w in warn))


if __name__ == "__main__":
    unittest.main()


class PictureProvenance(unittest.TestCase):
    """2026-11-14 NV0560: Codex built its own clips (original zoomed 6x, another segment's picture cropped in) and
    pointed selections at them. Only ledger outputs and the skill's own derivatives may go into recut or splice."""
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        import swap
        self.swap = swap
        self.tmp = tempfile.TemporaryDirectory(); self.run = run = Path(self.tmp.name) / 'run'
        res = run / 'generation/results'; res.mkdir(parents=True); (run / 'generation/jobs').mkdir()
        self.out = res / 'G01-a1-c1.mp4'; self.out.write_bytes(b'paid')
        swap.save(run / 'generation/jobs/G01-a1.json', {'candidates': {'c1': {'output_path': str(self.out), 'output_sha256': swap.sha(self.out)}}})
        self.master = run / 'master-clean.mp4'; self.master.write_bytes(b'orig')
        swap.save(run / 'state.json', {'stages': {'prepare': {'working_master': {'path': str(self.master)}}}})

    def tearDown(self): self.tmp.cleanup()

    def test_ledger_output_and_own_derivatives_pass(self):
        for name in ('G01-a1-c1.mp4', 'G01-a1-c1.decaptioned.mp4', 'G01-a1-c1.decaptioned.captioned.mp4', 'G01-a1-c1.srcaudio.decaptioned.mp4'):
            p = self.out.with_name(name); p.exists() or p.write_bytes(b'x')
            self.assertIsNotNone(self.swap.picture_origin(self.run, p), name)

    def test_own_built_clip_refused(self):
        qa = self.run / 'qa/local'; qa.mkdir(parents=True)
        for p in (qa / 'G01-car-angles.mp4', qa / 'G01-a1-c1.captioned.mp4'):
            p.write_bytes(b'x'); self.assertIsNone(self.swap.picture_origin(self.run, p))

    def test_overwritten_result_refused(self):
        self.out.write_bytes(b'reframed in place')
        self.assertIsNone(self.swap.picture_origin(self.run, self.out))

    def test_splice_source_only_results_and_original(self):
        self.assertEqual(self.swap.splice_source(self.run, str(self.master)), self.master.resolve())
        self.assertEqual(self.swap.splice_source(self.run, 'G01-a1-c1'), self.out)
        zoom = self.run / 'zoom.mp4'; zoom.write_bytes(b'z')
        with self.assertRaises(SystemExit): self.swap.splice_source(self.run, str(zoom))


class ReferenceCropsPeople(unittest.TestCase):
    """11-14 batch6: references zoom-cropped so that a person left the frame all failed (new person, clothes, scene)."""
    def test_crop_window_found(self):
        import numpy as np
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        import cv2, ref_framing as rf
        rng = np.random.default_rng(0)
        S = cv2.GaussianBlur((rng.random((1920, 1080, 3)) * 255).astype('uint8'), (0, 0), 6)
        R = cv2.resize(S[200:200 + 1152, 300:300 + 648], (480, 854))       # a 0.6 crop, scaled to 480p
        score, box = rf.crop_window(S, R)
        self.assertGreater(score, 0.9)
        for got, want in zip(box, (300 / 1080, 200 / 1920, 0.6, 0.6)): self.assertAlmostEqual(got, want, delta=0.03)

    def test_face_mostly_outside_counts_as_lost(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        import ref_framing as rf
        window = (0.4, 0.1, 0.55, 0.55)
        self.assertFalse(rf.covered((0.10, 0.2, 0.15, 0.08), window, 0.25))   # the other man in the handshake (NV0401 G06)
        self.assertTrue(rf.covered((0.60, 0.15, 0.25, 0.14), (0.05, 0.02, 0.69, 0.64), 0.25))  # face at the edge, a third in (NV0436 G04, fine)


class ReferenceCompetitorResidue(unittest.TestCase):
    """11-15 batch7: 8 of 10 severe goofs were competitor objects left in the reference (mid/wide hands, frame edges,
    a bottle standing on the counter, unscrewing, pouring into a spoon); the prompt did not overrule them."""
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        import swap
        self.swap = swap

    def test_blocking_rule(self):
        b = self.swap.residue_blocking
        close_held = {'t': 1, 'kind': 'container', 'size': 'large', 'state': 'held', 'where': 'centre'}
        self.assertEqual(b([close_held]), [])
        for x in ({**close_held, 'size': 'medium'}, {**close_held, 'size': 'small'}, {**close_held, 'state': 'opening'},
                  {**close_held, 'state': 'pouring'}, {**close_held, 'state': 'standing'}, {**close_held, 'kind': 'spoon'}):
            self.assertEqual(b([x]), [x], x)

    def test_parse_erase(self):
        e = self.swap.parse_erase(['G04:0.1,0.5,0.2,0.2@0-3', 'G04:0.6,0.6,0.1,0.1@2.5-end'])
        self.assertEqual(e['G04'][0], (0.1, 0.5, 0.2, 0.2, 0.0, 3.0))
        self.assertEqual(e['G04'][1][5], 1e9)
        for bad in ('G04:0.9,0.5,0.2,0.2@0-3', 'G04:0.1,0.5,0.2@0-3', 'G04:0.1,0.5,0.2,0.2@3-1'):
            with self.assertRaises(SystemExit): self.swap.parse_erase([bad])

    def test_erase_boxes_frames(self):
        out = self.swap.erase_boxes([(0.5, 0.5, 0.25, 0.25, 1.0, 2.0)], (480, 854), 24, first=2, n_frames=96)
        self.assertEqual(min(out), 22); self.assertEqual(max(out), 45)
        self.assertEqual(out[30], [[240, 427, 120, 214]])
        tail = self.swap.erase_boxes([(0, 0, 0.1, 0.1, 3.0, 1e9)], (480, 854), 24, n_frames=96)
        self.assertEqual(max(tail), 95)


class UserApprovedPatch(unittest.TestCase):
    """2026-11-21 film 1753 G17: the user allowed one local erase on a generated picture; the refusal stays the default."""
    def test_flag_exists(self):
        src = (Path(__file__).resolve().parents[1]/'scripts/region_inpaint.py').read_text()
        self.assertIn('--user-approved', src)
        self.assertIn("'user_approved': a.user_approved", src)
