import unittest,sys,tempfile
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import compare_board as cb
class NestedBoardCosts(unittest.TestCase):
 def test_nested_picture_and_audio_paid_leaves_count_once(self):
  def paid(cost,shot=False):return {'provider':'apimart','job':{'model':'seedance-2.0',**({'shot_regen':{'pieces':[[0,60]]}} if shot else {})},'candidates':{'c1':{'cost_usd':cost}}}
  def local(pics,windows=()):return {'provider':'local_splice','job':{'model':'local_splice'},'recipe':{'pictures':[{'source':x} for x in pics],'audio_windows':[{'source':x} for x in windows]},'candidates':{'c1':{'cost_usd':0}}}
  led={'G14-a1':paid(2),'G14-custom':paid(3,True),'G14-next':paid(5,True),'G14-a2':local(['G14-a1-c1','G14-custom-c1']),'G14-a3':local(['G14-a2-c1','G14-next-c1']),'G14-a4':local(['G14-a3-c1'],['G14-next-c1'])}
  self.assertEqual(set(cb.paid_dependencies('G14-a4',led)),{'G14-a1','G14-custom','G14-next'})
  plan={'segments':[{'id':'G14','in_frame':0,'out_frame':300,'rewrites':{'dialogue':[],'vo':[]}}]};sel={'G14':{'job':'G14-a4'}};tl={'segments':[{'picture_id':'G14','source_in':0,'source_out':300,'out_in':0,'out_out':300}]}
  with tempfile.TemporaryDirectory() as td:row=cb.segment_rows(Path(td),plan,sel,tl,30,{},led)[0]
  self.assertEqual(row['baseKind'],'regen');self.assertEqual(row['cost'],10);self.assertEqual(row['regenCost'],8)
 def test_two_free_splices_are_not_paid_regeneration(self):
  led={'G01-a1':{'provider':'apimart','job':{'model':'seedance-2.0'},'candidates':{'c1':{'cost_usd':2}}},'G01-a2':{'provider':'local_splice','recipe':{'pictures':[{'source':'G01-a1-c1'}]}},'G01-a3':{'provider':'local_splice','recipe':{'pictures':[{'source':'G01-a2-c1'}]}}}
  deps=cb.paid_dependencies('G01-a3',led);self.assertEqual(set(deps),{'G01-a1'});self.assertFalse(any(cb.is_supplement(j,l) for j,l in deps.items()))
