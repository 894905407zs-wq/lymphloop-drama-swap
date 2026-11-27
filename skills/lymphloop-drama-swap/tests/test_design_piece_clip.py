import json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import swap_design as sd

class ManualHardCutChild(unittest.TestCase):
    def test_child_extracts_exact_window_and_keeps_source_shot_missing(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d); (r/'shots').mkdir()
            (r/'shots/shots.json').write_text(json.dumps({'fps':24,'source':'/source.mp4','shots':[{'id':'S035'}]}))
            with patch('media_pipeline.binary', return_value='ffmpeg'), patch.object(sd.subprocess,'run') as run:
                p=sd.piece_clip(r,{'id':'S035b','in_frame':7510,'out_frame':7531})
                self.assertEqual(p,r/'design/clips/S035b.mp4')
                args=run.call_args.args[0]
                self.assertEqual(args[args.index('-ss')+1],'312.917')
                self.assertEqual(args[args.index('-t')+1],'0.875')
                run.reset_mock()
                self.assertEqual(sd.piece_clip(r,{'id':'S035','in_frame':7400,'out_frame':7531}),r/'clips/S035.mp4')
                run.assert_not_called()

class IngredientOnlyFallback(unittest.TestCase):
    def test_agent_reviewed_plant_edit_has_no_physical_product(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);(r/'detect').mkdir();(r/'screen').mkdir()
            (r/'screen/S034.json').write_text(json.dumps({'raw':{'competitor_visible':'yes'}}))
            p=r/'detect/overrides.json'
            p.write_text(json.dumps({'S034':{'decision':'replace','kinds':['visual'],'physical_competitor':False}}))
            self.assertFalse(sd.seen_competitor(r,'S034'))
            p.write_text(json.dumps({'S034':{'decision':'replace','kinds':['visual']}}))
            self.assertTrue(sd.seen_competitor(r,'S034'))

class IngestionStartsAtClosedLips(unittest.TestCase):
    def test_no_travel_towards_original_open_mouth(self):
        d=sd.decide({'hand_action':'ingest','items':[{'kind':'dropper','where':'near_mouth','size':'large','count':1,'within_reach_of_person':True,'look':'glass pipette'}]})
        self.assertEqual(d['form'],'opened_packet')
        self.assertIn('从第一帧',d['redesign']);self.assertIn('闭合的双唇',d['redesign'])
        self.assertIn('另一只手空着',d['redesign'])

class PlaceSameCarton(unittest.TestCase):
    def test_reverse_angle_does_not_change_taken_carton_into_packet(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);(r/'plan').mkdir();(r/'plan/segments.json').write_text(json.dumps({'segments':[{'shot_ids':['S153a','S153b']}]}))
            rows={'S153a':{'form':'carton','rule':'R9-纸盒'},'S153b':{'form':'opened_packet','rule':'R7-小而近人','observation':{'hand_action':'place_down'}}}
            out=sd.follow_into_carton(r,rows);self.assertEqual(out['S153b']['form'],'carton');self.assertEqual(out['S153b']['placement'],'standing')
            rows={'S153a':{'form':None,'rule':'无竞品'},'S153b':{'form':'opened_packet','rule':'R7-小而近人','observation':{'hand_action':'place_down'}}}
            self.assertEqual(sd.follow_into_carton(r,rows)['S153b']['form'],'opened_packet')

class CloseContainer(unittest.TestCase):
    def test_recap_is_not_reopening(self):
        d=sd.decide({'hand_action':'close_cap','items':[{'kind':'bottle','where':'in_hand','size':'medium','count':1,'within_reach_of_person':True,'look':'bottle'}]})
        self.assertEqual(d['rule'],'R6b-合盖');self.assertIn('合下',d['redesign']);self.assertIn('已经翻开',d['redesign'])

class PocketIsNotBag(unittest.TestCase):
    def test_pocket_packet_does_not_become_carton_at_reverse_cut(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d);(r/'plan').mkdir();(r/'plan/segments.json').write_text(json.dumps({'segments':[{'shot_ids':['S264a','S264b']}]}))
            rows={'S264a':{'form':'sealed_packet','rule':'R5-口袋','observation':{'retrieval_container':'pocket'}},'S264b':{'form':'carton','rule':'R9-纸盒'}}
            out=sd.follow_into_carton(r,rows);self.assertEqual(out['S264a']['form'],'sealed_packet');self.assertEqual(out['S264b']['form'],'sealed_packet')
            rows={'S264a':{'form':'sealed_packet','rule':'R5-口袋','observation':{'retrieval_container':'bag'}},'S264b':{'form':'carton','rule':'R9-纸盒'}}
            out=sd.follow_into_carton(r,rows);self.assertEqual(out['S264a']['form'],'carton');self.assertEqual(out['S264b']['form'],'carton')
