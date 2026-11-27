import unittest,sys
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import watermark_cleanup as wc,watermark_brand as wb
class MeasuredMasks(unittest.TestCase):
 def test_oversized_native_ocr_cannot_erase_extra_forehead(self):
  place={'id':5,'box':[107,202,129,25],'vertical':False}
  data={'positions':[place],'detections':[{'frame':6126,'bbox':[68,200,169,32],'classification':'floating_overlay'},{'frame':6126,'bbox':place['box'],'classification':'floating_overlay'},{'frame':6126,'bbox':[200,600,120,30],'classification':'printed_label_keep'}]}
  self.assertEqual(wc.overlay_groups(data),{6126:[place['box']]})
  self.assertEqual(data['detections'][0]['bbox'],[68,200,169,32])
  p={'stamps':{'6126':[{'box':[68,200,169,32],'orient':'h','track':0}]},'track_list':[{'id':0,'box':[68,200,169,32]}]};wb.fix_stamps(p,[place]);st=p['stamps']['6126'][0]
  self.assertEqual(st['mark'],[68,200,169,32]);self.assertEqual(st['mask_box'],place['box'])
  rng=np.random.default_rng(7);f=rng.integers(0,256,(1280,720,3),dtype=np.uint8);out=wb.apply_stamps(f,[st]);np.testing.assert_array_equal(out[190:235,65:104],f[190:235,65:104])
 def test_unmatched_manual_region_still_cleared(self):
  d={'positions':[],'detections':[{'frame':3,'bbox':[10,20,30,40],'classification':'floating_overlay'}]};self.assertEqual(wc.overlay_groups(d),{3:[[10,20,30,40]]})
if __name__=='__main__':unittest.main()
