"""Card mode for goof redos (user 2026-11-23: "第一次生成采用原片参考的模式，如果第一次出现穿帮了，第二次补生就要采用人物卡模式").
A redo round opens in card mode by default: no reference video is sent, only character cards + product images (and the
speaker's voice clip for a speaking shot); the prompt describes the whole shot."""
import argparse, contextlib, io, json, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent/'scripts'))
sys.path.insert(0, str(HERE))
import apimart_wan as am  # noqa: E402
import shot_regen as sr  # noqa: E402
import swap_design  # noqa: E402
from test_goof_rounds import PROMPT, movie  # noqa: E402

CARD_PROMPT = """# cards: WALT,FRIEND
# views: handheld_photo
# voice: 0-3
【生成目标】
生成一个美式写实剧情镜头：更衣室里灰须男子向胖男子介绍条包。人物严格按人物卡，产品严格按产品图。

【参考素材职责】
@图片1是胖男子，用于他的脸、短发、胡子、体型和深藏青短袖。
@图片2是灰须男子，用于他的脸、花白卷发、灰白短胡和左肩的毛巾。
@图片3用于未开封条包的外观和它捏在指尖时的比例，不采用图中的手和背景。
@音频1只用于灰须男子的音色、语速和语气，台词以下面写出的为准。人物卡的白色背景和排版都不采用。

【主体与关系】
环境：傍晚的健身房更衣室，暖白顶灯从头顶照下；一排米灰色储物柜靠着后墙，柜前一条木长凳，长凳尽头是挂着毛巾的门。坐在长凳上、背对储物柜时，门在右手边。
站位：胖男子坐在长凳靠近门的这一端，灰须男子坐在另一端，侧身朝向他。
机位 A：在长凳正前方略低处朝储物柜拍，拍两人的中景，背景永远是那排米灰色储物柜，门在镜头的左手边。
产品：画面里仅1支未开封的yesnap LymphLoop条包，开始时在胖男子右手指尖，正面朝镜头；长凳上只有一条毛巾。
音色——灰须男子：美国黑人男声，六十岁左右，中音偏低，有颗粒，慢而稳，句尾往下掉，不是播音腔，同@音频1。

【事件脚本】
阶段一：机位 A，双人中景，手持微晃。胖男子右手指尖捏着条包低头看，眉头一挑；灰须男子身体前倾、用手指点了点条包，大小参照：条包约胖男子一根食指长，比他拇指指腹略宽。灰须男子台词（放慢，「one」一字加重，用美式英语）：{This is the one.}说完他靠回柜门。结束时条包仍只在胖男子手里。

【保持一致】
全程仅1支未开封的yesnap LymphLoop条包；产品在每一镜里和人脸、手、身边物件的大小关系都一样，不忽大忽小；人物面容服装同人物卡；环境、光线、站位和机位同上，镜头之间空间方向稳定；产品、桌面和手与人物同一种画面质感；台词只有上面写出的这些，不说话的人闭着嘴做自己的反应（自然闭口），只有写明台词的人张嘴说话；只有轻微的环境声；画面不显示字幕。
"""


def png(path):
    from PIL import Image
    path.parent.mkdir(parents=True, exist_ok=True); Image.new('RGB', (64, 48), 'white').save(path)


class CardRound(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='lymphloop-cards-'); r = cls.rundir = Path(cls.tmp.name)
        for d in ('clips', 'plan', 'prompts', 'jobs', 'generation/results', 'generation/jobs'): (r/d).mkdir(parents=True)
        movie(r/'clips/G01.hq.clean.ref.mp4'); (r/'clips/G01.hq.clean.ref.json').write_text('{}')
        movie(r/'generation/results/G01-a1-c1.mp4', fps=24)
        (r/'prompts/G01-a1.txt').write_text(PROMPT.replace('# design: S002\n', ''))
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

    def test_round_opens_in_card_mode_and_prepares_a_job_without_video(self):
        r = self.rundir
        opened = json.loads(self.call(sr.cmd_round_open, file=None, round=None, goof=['G01@2.8:side:纸盒侧面当正面', 'G01@5.0:shape:纸盒像长条']))
        a, b = opened['items']
        self.assertEqual((a['mode'], b['mode']), ('cards', 'cards'))
        meta = json.loads((r/'shots-regen'/f"{a['name']}.json").read_text())
        self.assertEqual((meta['mode'], meta['sound'], meta['seconds']), ('cards', 'lip_sync', 4.0))
        stub = (r/'shots-regen'/f"{a['name']}.txt").read_text()
        self.assertTrue(stub.startswith('# cards:')); self.assertIn('# voice:', stub); self.assertNotIn('@视频', stub)
        for part in ('机位 A：', '轴线锚点', '空间方向稳定', '画面质感', '音色——', '说话前的触发动作'): self.assertIn(part, stub)
        self.assertIn('{This is the one.}', stub)
        self.assertNotIn('# voice:', (r/'shots-regen'/f"{b['name']}.txt").read_text())   # nobody speaks in that shot
        self.assertTrue(Path(a['strip']).is_file())

        # a filled prompt without the cards built is refused; with them it becomes a card-mode job with the voice clip
        (r/'shots-regen'/f"{a['name']}.txt").write_text(CARD_PROMPT)
        pa = argparse.Namespace(run=str(r), name=a['name'], user_quote=None, goof=True, channel='apimart', model=None, again=False, why=None, note=None)
        with self.assertRaises(SystemExit) as e: sr.cmd_prepare(pa)
        self.assertIn('人物卡', str(e.exception))
        png(r/'cards/WALT.png'); png(r/'cards/FRIEND.png'); png(r/'cards/LOCKER_ROOM.png')
        (r/'cards/cast.json').write_text(json.dumps({'characters': {'WALT': {}, 'FRIEND': {}}}))
        # a scene image is not a character card (user 2026-11-23); the old flat stub without a camera table is refused
        (r/'shots-regen'/f"{a['name']}.txt").write_text(CARD_PROMPT.replace('# cards: WALT,FRIEND', '# cards: WALT,FRIEND,LOCKER_ROOM'))
        with self.assertRaises(SystemExit) as e: sr.cmd_prepare(pa)
        self.assertIn('场景图', str(e.exception))
        (r/'shots-regen'/f"{a['name']}.txt").write_text(CARD_PROMPT.replace('机位 A：', '').replace('机位 A，', ''))
        with self.assertRaises(SystemExit) as e: sr.cmd_prepare(pa)
        self.assertIn('机位表', str(e.exception))
        (r/'shots-regen'/f"{a['name']}.txt").write_text(CARD_PROMPT)
        with contextlib.redirect_stdout(io.StringIO()): sr.cmd_prepare(pa)
        job = json.loads((r/'jobs'/f"G01-{a['name']}.json").read_text())
        self.assertEqual(job['generation_mode'], am.MODE_CARDS)
        self.assertEqual([x['kind'] for x in job['references']], ['image', 'image', 'image', 'audio'])
        self.assertEqual([x.get('card') for x in job['references'][:2]], ['WALT', 'FRIEND'])
        self.assertEqual((job['shot_regen']['mode'], job['shot_regen']['sound']), ('cards', 'lip_sync'))
        self.assertIn('人物卡模式', job['shot_regen']['quote'])
        body = am.payload(job, 'p', ['i1', 'i2', 'i3'], [], ['a1'])
        self.assertNotIn('video_urls', body); self.assertEqual(body['audio_urls'], ['a1']); self.assertEqual(body['duration'], 4)

        # the reference way is still there when asked for
        out = json.loads(self.call(sr.cmd_round_open, file=None, round=9, goof=['G01@5.0:shape:纸盒像长条'], mode='reference'))
        self.assertEqual(out['items'][0]['mode'], 'slow')


class Adapter(unittest.TestCase):
    def job(self, **kw):
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as f: pass
        png(Path(f.name))
        return {'job_id': 'G01-x', 'run_dir': '/tmp', 'prompt_file': '/tmp/p', 'model': am.MODEL, 'output_seconds': 4, 'generation_mode': am.MODE_CARDS,
                'references': [{'kind': 'image', 'path': f.name}], 'shot_regen': {'quote': 'q'}, **kw}

    def test_card_jobs_are_redos_only_and_carry_no_video(self):
        am.validate(self.job())
        with self.assertRaises(Exception): am.validate(self.job(shot_regen={}))
        with self.assertRaises(Exception): am.validate(self.job(references=[{'kind': 'video', 'path': '/tmp/v.mp4'}]))
        with self.assertRaises(Exception): am.payload(self.job(), 'p', ['i'], ['v'])
        with self.assertRaises(Exception): am.validate({**self.job(), 'generation_mode': 'reference'})   # a reference job needs its video


class Splice(unittest.TestCase):
    def shot(self, name, sound, real=2.0):
        return {'name': f'G01-{name}-c1', 'short': name, 'mode': 'cards', 'sound': sound, 'seconds': 4, 'real_s': real, 'gen_fps': 24.0, 'gen_frames': 97}

    def test_quiet_card_shot_fills_its_frames_and_speaking_one_runs_at_its_length(self):
        pics, _ = sr.into_pieces('B', 180, [self.shot('q', 'source'), self.shot('t', 'lip_sync')], ['q=24-71', 't=120-167'])
        self.assertEqual(pics, ['B:0-23', 'G01-q-c1:0-47@48', 'B:72-119', 'G01-t-c1:0-47', 'B:168-179'])


class Lint(unittest.TestCase):
    def test_card_prompt_may_not_point_at_a_video(self):
        body = CARD_PROMPT.split('\n', 3)[3]
        self.assertEqual(swap_design.five_section_problems(body, speech=True, n_images=3, video=False), [])
        self.assertTrue(any('人物卡模式' in e for e in swap_design.five_section_problems(body + '\n@视频1', speech=True, n_images=3, video=False)))


if __name__ == '__main__':
    unittest.main()
