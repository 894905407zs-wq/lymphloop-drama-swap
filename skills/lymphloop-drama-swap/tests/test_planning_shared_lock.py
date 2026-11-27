import json, sys, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import swap

class SharedAnalysisPlanning(unittest.TestCase):
    def test_completed_analysis_can_plan_beside_scan_but_generation_and_apply_cannot(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d); (r/'state.json').write_text(json.dumps({'stages':{'screen':{'done_at':'x'},'detect_pro':{'done_at':'x'},'merge':{'done_at':'x'}}}))
            def a(cmd,**kw): return SimpleNamespace(run=d,cmd=cmd,lock_wait=0,**kw)
            with swap.run_lock(a('watermark',action='scan')):
                with swap.run_lock(a('merge')): pass
                with swap.run_lock(a('plan')): pass
                for args in [a('watermark',action='apply'),a('generate',refs_only=False),a('prepare')]:
                    with self.assertRaises(SystemExit):
                        with swap.run_lock(args): pass
    def test_no_parallel_merge_with_incomplete_analysis_or_plan_after_generation(self):
        with tempfile.TemporaryDirectory() as d:
            r=Path(d); p=r/'state.json';p.write_text(json.dumps({'stages':{'screen':{'done_at':'x'}}}))
            self.assertIsNone(swap.shared_stage(SimpleNamespace(run=d,cmd='merge')))
            p.write_text(json.dumps({'stages':{'merge':{'done_at':'x'}}}));q=r/'generation/jobs';q.mkdir(parents=True);(q/'G01-a1.json').write_text('{}')
            self.assertIsNone(swap.shared_stage(SimpleNamespace(run=d,cmd='plan')))
