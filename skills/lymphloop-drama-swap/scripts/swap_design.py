#!/usr/bin/env python3
"""Per-shot swap design without a human in the loop (user 2026-11-20: "我最终是要让agent自己能够判断，不要给我审，
因为我会批量跑很多视频").

Three parts:
  observe   Flash answers fixed, factual questions about every replacement shot (what competitor item, where, how big in
            the frame, what the hand does, shot scale). Facts, not decisions. RUN/design/obs/S###.json (cached).
  decide    A fixed rule table (RULES below, built from the user's rulings on test film 2412) turns the facts into the
            target form (carton / sealed packet / opened packet), whether the action is kept or redesigned, the count,
            the reference images and the prompt template. RUN/plan/swap_design.json. The agent does not improvise here;
            when a decision is wrong, the rule table is fixed (and its test), not the one shot.
  skeleton  Writes the five-section prompt skeleton of a segment or shot group from the design (carton-swap.md for
            cartons, the same structure for packets), with the decided form, count, images, removals and per-shot
            actions already filled; the agent fills scene, people, voices and the exact lines.

    swap_design.py observe  --run RUN [--shots S167,S168]      # default: every shot of every planned segment
    swap_design.py decide   --run RUN
    swap_design.py skeleton --run RUN --seg G16 --shots S219,S220 --name S219g   # -> RUN/shots-regen/S219g.txt
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, json, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

KINDS = ['bottle', 'dropper', 'carton_box', 'capsule_or_pill', 'gummy', 'powder_tub', 'pouch_or_sachet', 'other']
WHERE = ['in_hand', 'on_table_or_surface', 'near_mouth', 'inside_open_container', 'inside_shipping_box', 'in_pocket_or_bag',
         'background_shelf_or_windowsill', 'foreground_blurred']
SIZE = ['large', 'medium', 'small']          # height in the frame: large > 1/5, medium 1/15-1/5, small < 1/15
ACTIONS = ['none', 'hold_show', 'turn_inspect', 'place_down', 'pick_up', 'reach_grab', 'hand_over', 'twist_open_cap',
           'open_box_lid', 'close_cap', 'close_box_lid', 'return_dropper', 'pull_dropper', 'drip_or_dropper_use', 'pour_or_shake_out', 'ingest', 'take_from_container',
           'take_from_shipping_box', 'take_from_pocket_or_bag']
SCALE = ['extreme_close', 'close_up', 'medium', 'wide']
WHERE_ZH = {'in_hand': '在手里', 'on_table_or_surface': '在原有承托表面上', 'near_mouth': '在嘴边', 'inside_open_container': '在打开的容器里',
            'inside_shipping_box': '在快递箱里', 'in_pocket_or_bag': '在口袋或包里', 'background_shelf_or_windowsill': '在背景架子或窗台上',
            'foreground_blurred': '在前景虚化处'}

OBS_SCHEMA = {
    'type': 'OBJECT',
    'properties': {
        'items': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
            'kind': {'type': 'STRING', 'enum': KINDS}, 'where': {'type': 'STRING', 'enum': WHERE},
            'size': {'type': 'STRING', 'enum': SIZE}, 'count': {'type': 'INTEGER'},
            'within_reach_of_person': {'type': 'BOOLEAN'}, 'look': {'type': 'STRING'}},
            'required': ['kind', 'where', 'size', 'count', 'within_reach_of_person', 'look']}},
        'hand_action': {'type': 'STRING', 'enum': ACTIONS},
        'shot_scale': {'type': 'STRING', 'enum': SCALE},
        'who_handles': {'type': 'STRING'},
        'product_is_shot_subject': {'type': 'BOOLEAN'},
        'speaking': {'type': 'BOOLEAN'},
        'ingredient_only': {'type': 'BOOLEAN'},
        'on_screen': {'type': 'STRING', 'enum': ['none', 'phone', 'computer']},
        'confidence': {'type': 'NUMBER'},
    },
    'required': ['items', 'hand_action', 'shot_scale', 'who_handles', 'product_is_shot_subject', 'speaking', 'confidence', 'ingredient_only'],
}

OBS_PROMPT = """You inspect ONE shot of a supplement video ad. List every competitor product item visible in it (the competitor
is described below; the competitor may be a bottle, dropper, carton/box, capsules, gummies, powder tub or pouch).
Report facts only:
- items: one entry per kind of item. kind; where it is (in_hand, on_table_or_surface, near_mouth, inside_open_container,
  inside_shipping_box, in_pocket_or_bag, background_shelf_or_windowsill, foreground_blurred; inside_open_container is
  only the competitor product's OWN opened box or package -- a bottle in a drawer, cabinet, cup holder or basket is
  on_table_or_surface); size = its height in the
  frame (large: more than one fifth of the frame height, medium: one fifteenth to one fifth, small: less than one
  fifteenth); count of that kind; within_reach_of_person = a person's hand touches it or is within a hand's length of it;
  look = colours and shape in a few words, IN CHINESE (e.g. "琥珀色玻璃滴管瓶、黑色滴管帽、米色标签").
  Look carefully at small items standing next to cups or hands: they count.
- hand_action: the main thing a hand does with the competitor product in this shot (none if nobody touches it).
  pull_dropper = drawing a dropper/pipette out of a bottle; drip_or_dropper_use = squeezing drops into a drink, onto
  food or a hand -- NOT into the mouth; ingest = putting the product in the mouth or drinking it, INCLUDING a dropper put
  to the lips, into the mouth or under the tongue (that is ingest even if the dropper was drawn earlier in the shot); take_from_container = taking a dose or a unit out of the
  product's own container; twist_open_cap = unscrewing a cap.
- shot_scale of the whole picture; who_handles (short description of the person, or "nobody");
  product_is_shot_subject = the shot presents the competitor product as a subject (set in front of a person or near the
  frame centre as part of what the shot is about, or held up / shown), false when it is an incidental object beside
  cups, food or other props; speaking = someone in the picture talks; confidence 0-1.
- on_screen = phone / computer when the competitor product is shown ON a phone or a laptop / desktop / tablet screen
  in the picture (a shopping page, a product page, an ad, a video on the screen); the product inside the screen is not
  a physical item, do not list it in items. none otherwise.
- ingredient_only = true only for a botanical ingredient insert showing a plant and NO product packaging, dropper or dose object. A plant insert is not a physical competitor product. Otherwise false.
Return one JSON object."""


# ------------------------------------------------------------------ rule table (decisions; no Flash, no agent judgment)
CARTON_VIEWS = ['carton_views', 'carton_hand']
RULES = [  # (id, user ruling it comes from) -- the order below is the order decide() applies them
    ('R0-筛查有观察无', '筛查说有竞品、观察没看到：按人手边的小件处理，换手捏已开封条包，绝不当没有'),
    ('R0c-同物接续', '同物前后一致（2026-11-22 片子 1603578481269031 G02）：同一段里相邻镜头手里拿着纸盒/条包，正反打里竞品只在画边、观察没看到的镜头沿用同一件东西，不换成 R0 的已开封条包'),
    ('R7d-同物接续', '同物前后一致（2026-11-22 片子 1912538379721657 S049/S050、998024963378000 三组）：同一段里 R7（小而近人、手捏已开条包）紧挨着手持纸盒的镜头，是同一件东西换了机位，跟着变成手持纸盒'),
    ('R5d-口袋同物', '产品物理优先（2026-11-22 片子 1429516565935964 S063/S064、S074/S075）：从口袋掏出或收进口袋的那件东西，紧挨着的手持纸盒镜头是同一件，改成手捏未开封条包——口袋装不下纸盒'),
    ('S1-屏幕商品页', '用户 2026-11-22：手机或电脑屏幕上显示竞品的购物页 / 落地页，整页换成我方商品页（手机 phone_page、电脑 desktop_page）；屏幕外手里拿着的实物照常按别的规则换，两张图一起给'),
    ('R1-服用', '用户 11-17：服用只有吸一种动作——已开封条包，嘴唇含住开口吸'),
    ('R1b-服用后接续', '产品状态连续（2026-11-22 片子 140 G05、283 G17）：服用之后紧接着手里拿着的同一件东西是已开封条包，不变回未开封条包或纸盒'),
    ('R2-滴管', '用户 11-19：所有抽滴管的镜头都换掉滴管，滴管出现就是穿帮（远景里很小的滴管走 R7，11-20 S206）。2026-11-20 片子 1753：“静态条包平放”4 个镜头全被画成条包自己立着（参考片里瓶子是竖的）——有手就让原来拿瓶的手一直捏着未开封条包，没有手就在原位立 1 个纸盒'),
    ('R3-取出', '用户 11-19：条包不能从封闭盒顶抽出；从容器里取东西改成从已打开的纸盒里抽出一支未开条包'),
    ('R4-快递箱', '用户 11-19 G07：快递箱里提纸盒，箱里整齐竖放 LymphLoop 纸盒'),
    ('R5-口袋', '产品物理：口袋只能掏条包，不能掏整盒'),
    ('R5b-包里拿出纸盒', '同物前后一致（2026-11-20 片子 1753 S132→S133、S209→S210）：从包里拿出、下一镜（同段）就是纸盒的同一件东西，这一镜也拿出纸盒——手提包装得下手掌大的盒子，口袋装不下'),
    ('R5c-口袋条包接续', '产品物理与同物连续：明确从衣服口袋取出的是未开条包，下一镜接过查看的同一件物也保持未开条包，不把手提袋例外套到口袋上'),
    ('R6-拧盖', '拧盖/倒出：小而远时改手捏已开封条包；看得清时改成翻开纸盒顶盖'),
    ('R6b-合盖', '产品状态连续：旋紧瓶盖、归还滴管和合盒盖属于收尾动作；改为合上已经打开的纸盒，不倒写成重新开盒'),
    ('R7-小而近人', '用户 11-19 G14：中远景、产品偏小、有人拿着或就在手边、又不是镜头主体 -> 手捏已开封条包，条包始终在手里'),
    ('R7b-小而是主体', '用户 11-19 G02：开场全景里摆在老人面前的小瓶 -> 纸盒（拿在手里或立在原位）'),
    ('R7c-同物放下', '产品状态连续：上一镜已取出纸盒，紧接下一镜把同一物体放到承托面时保持纸盒，不因机位变远突然改成条包；独立小物镜头仍按R7'),
    ('R8-小而无人', '用户 11-19 G01：窗台、背景里没人碰的小瓶 -> 原位立一个纸盒'),
    ('R9-纸盒', '看得清的瓶/盒：拿在手里 -> 手握纸盒两侧；立在桌上 -> 原位立 1 个纸盒；原片几个都只放 1 个（用户 11-19 G16）'),
]


def _max_size(items):
    order = {s: i for i, s in enumerate(SIZE)}
    return min((it['size'] for it in items), key=lambda s: order[s], default='small')


def decide(obs, screened_competitor=False):
    """obs (OBS_SCHEMA) -> {'rule', 'form', 'placement', 'action', 'count', 'views', 'template', 'removals'}.
    screened_competitor: the per-shot screening found a competitor here. If the observation then lists nothing, the item
    is taken as a small one in a person's reach (11-20 S206: seen in one Flash run, missed in the next) -- never as no
    competitor (G12 was left unreplaced that way)."""
    items = obs.get('items') or []
    scr = obs.get('on_screen') if obs.get('on_screen') in ('phone', 'computer') else None
    if scr:
        page = 'phone_page' if scr == 'phone' else 'desktop_page'
        screen_text = ('原片' + ('手机' if scr == 'phone' else '电脑') + '屏幕上显示的竞品页面整页换成@图片里的yesnap LymphLoop商品页（版式、主图里的纸盒和条包、标题），'
                       '屏幕本身、拿着它的手和画面其余部分不变')
        if not items:
            return {'removals': ['屏幕上的竞品商品页'], 'count': 1, 'rule': 'S1-屏幕商品页', 'form': page, 'placement': 'on_screen', 'action': 'redesign',
                    'redesign': screen_text, 'views': [page], 'template': 'page'}
        d = decide({**obs, 'on_screen': 'none'}, screened_competitor)
        return {**d, 'removals': list(d.get('removals') or []) + ['屏幕上的竞品商品页'], 'views': list(d.get('views') or []) + [page],
                'screen': page, 'redesign': (d.get('redesign') or act_of(d)) + '；' + screen_text, 'action': 'redesign'}
    if not items and obs.get('ingredient_only'):
        return {'removals': [], 'count': None, 'rule': '成分植物无包装', 'form': None, 'placement': None, 'action': 'keep',
                'redesign': '', 'views': [], 'template': None}
    if not items and screened_competitor:
        return {'removals': ['原片里人手边的小件竞品'], 'count': 1, 'rule': 'R0-筛查有观察无', 'form': 'opened_packet', 'placement': 'in_hand',
                'action': 'redesign', 'redesign': '人物手里（原来拿着东西的那只手）拇指和食指一直捏着已开封条包下半部，开口朝上',
                'views': ['opened_packet_hand'], 'template': 'packet'}
    if not items:      # nothing seen and nothing expected here: no product in this shot (was falling through to R9 carton)
        return {'removals': [], 'count': None, 'rule': '无竞品', 'form': None, 'placement': None, 'action': 'keep',
                'redesign': '', 'views': [], 'template': None}
    act = obs.get('hand_action', 'none')
    if act != 'take_from_container' and any(it['where'] == 'inside_open_container' for it in items):
        # an open drawer, a car cup holder, a cabinet is where the bottle rests, not the product's own package: R3 (open
        # carton, pull a packet) only when a hand takes a unit out (2026-11-22: 142 S132 a bottle standing in a cup
        # holder, nobody touching it; 110 S056 a bottle picked up from a barber's drawer -- both became "open the carton")
        items = [{**it, 'where': 'on_table_or_surface'} if it['where'] == 'inside_open_container' else it for it in items]
    where = {it['where'] for it in items}; kinds = {it['kind'] for it in items}
    size = _max_size(items)
    near = any(it.get('within_reach_of_person') or it['where'] in ('in_hand', 'near_mouth') for it in items)
    removals = [f"{WHERE_ZH.get(it['where'], '')}的{it['count']}个{it['look'] or it['kind']}" for it in items]
    base = {'removals': removals, 'count': 1}
    packet = lambda opened: (['opened_packet_hand'] if opened else ['handheld_photo'])
    if act == 'ingest' or 'near_mouth' in where:
        return {**base, 'rule': 'R1-服用', 'form': 'opened_packet', 'placement': 'at_mouth', 'action': 'redesign',
                'redesign': '从第一帧起单手持已开封条包，短平口贴在闭合的双唇上，闭唇轻抿后持包手自然放下，另一只手空着', 'views': packet(True), 'template': 'packet'}
    if (act in ('pull_dropper', 'drip_or_dropper_use') or 'dropper' in kinds) and size != 'small':
        # a packet laid "flat" where the reference has an upright bottle came out standing on its own in all four shots
        # of film 1753 (S129, S165, S166, S251): a hand keeps holding it, or a carton stands where the bottle stood
        if 'in_hand' in where or 'near_mouth' in where:
            return {**base, 'rule': 'R2-滴管', 'form': 'sealed_packet', 'placement': 'in_hand', 'action': 'redesign',
                    'redesign': '原来拿瓶子的那只手，拇指和食指一直捏住一支未开封条包的下半部，条包竖着停在原来瓶子的位置，正面朝镜头',
                    'views': packet(False), 'template': 'packet'}
        if kinds & {'bottle', 'box', 'jar', 'container'}:
            return {**base, 'rule': 'R2-滴管', 'form': 'carton', 'placement': 'standing', 'action': 'redesign',
                    'redesign': '原来瓶子的位置立着1个纸盒，底面着地，正面朝镜头', 'views': CARTON_VIEWS, 'template': 'carton'}
        # only a dropper, nobody holding it (user 11-19 S161: a blurred dropper in the foreground -> a packet)
        # was a packet lying flat on the surface: drawn standing on three films (1753, 1812 G15, 2114 G05) -> a carton
        return {**base, 'rule': 'R2-滴管', 'form': 'carton', 'placement': 'standing', 'action': 'redesign',
                'redesign': '原来滴管所在的平整承托面上立着1个纸盒，底面着地，正面朝镜头，固定机位', 'views': CARTON_VIEWS, 'template': 'carton'}
    if act == 'take_from_container' or 'inside_open_container' in where:
        return {**base, 'rule': 'R3-取出', 'form': 'carton+sealed_packet', 'placement': 'open_carton', 'action': 'redesign',
                'redesign': '纸盒顶盖已经翻开，手从盒里竖直抽出一支未开封条包', 'views': CARTON_VIEWS + ['handheld_photo'], 'template': 'carton'}
    if act == 'take_from_shipping_box' or 'inside_shipping_box' in where:
        return {**base, 'rule': 'R4-快递箱', 'form': 'carton', 'placement': 'shipping_box', 'action': 'keep', 'count': None,
                'redesign': '', 'views': CARTON_VIEWS, 'template': 'carton'}
    if act == 'take_from_pocket_or_bag' or 'in_pocket_or_bag' in where:
        return {**base, 'rule': 'R5-口袋', 'form': 'sealed_packet', 'placement': 'in_hand', 'action': 'keep',
                'redesign': '', 'views': packet(False), 'template': 'packet'}
    if act in ('close_cap', 'close_box_lid', 'return_dropper'):
        return {**base, 'rule': 'R6b-合盖', 'form': 'carton', 'placement': 'in_hand', 'action': 'redesign',
                'redesign': '一只手托住顶盖已经翻开的纸盒，另一只手把顶盖合下，再将纸盒底面稳放在原瓶所在承托面',
                'views': CARTON_VIEWS, 'template': 'carton'}
    if act in ('twist_open_cap', 'pour_or_shake_out'):
        if size == 'small':
            return {**base, 'rule': 'R6-拧盖', 'form': 'opened_packet', 'placement': 'in_hand', 'action': 'redesign',
                    'redesign': '双手停在胸前，一只手拇指和食指捏住已开封条包下半部、开口朝上，另一只手扶着', 'views': packet(True), 'template': 'packet'}
        return {**base, 'rule': 'R6-拧盖', 'form': 'carton', 'placement': 'in_hand', 'action': 'redesign',
                'redesign': '一只手托住纸盒，另一只手翻开纸盒顶盖', 'views': CARTON_VIEWS, 'template': 'carton'}
    if size == 'small' and items:
        if near and obs.get('product_is_shot_subject'):
            return {**base, 'rule': 'R7b-小而是主体', 'form': 'carton', 'placement': 'in_hand' if 'in_hand' in where else 'standing',
                    'action': 'keep', 'redesign': '', 'views': CARTON_VIEWS, 'template': 'carton'}
        if near:
            return {**base, 'rule': 'R7-小而近人', 'form': 'opened_packet', 'placement': 'in_hand', 'action': 'redesign',
                    'redesign': '原物所在那只手（或原物旁边的那只手）拇指和食指一直捏着已开封条包下半部，开口朝上，离开杯口和桌面',
                    'views': packet(True), 'template': 'packet'}
        return {**base, 'rule': 'R8-小而无人', 'form': 'carton', 'placement': 'standing', 'action': 'keep',
                'redesign': '', 'views': CARTON_VIEWS, 'template': 'carton'}
    in_hand = 'in_hand' in where or act in ('hold_show', 'turn_inspect', 'pick_up', 'reach_grab', 'hand_over', 'place_down')
    return {**base, 'rule': 'R9-纸盒', 'form': 'carton', 'placement': 'in_hand' if in_hand else 'standing', 'action': 'keep',
            'redesign': '', 'views': CARTON_VIEWS, 'template': 'carton'}


# ------------------------------------------------------------------ run plumbing
def read(p): return json.loads(Path(p).read_text())
def save(p, d): Path(p).parent.mkdir(parents=True, exist_ok=True); Path(p).write_text(json.dumps(d, ensure_ascii=False, indent=1))


def planned_shots(run):
    segs = read(Path(run)/'plan/segments.json')['segments']
    return {sid: s['id'] for s in segs for sid in (s.get('shot_ids') or [])}


def stills(run, shot):
    """Three full-resolution frames (1/4, 1/2, 3/4): small items next to cups or in a wide shot are missed at the clip's
    video resolution (11-20: S211 cup-side bottle, S206 wide-shot bottle seen in one run and not in the next)."""
    import media_pipeline as media
    rec = read(Path(run)/'shots/shots.json'); fps = float(rec.get('fps') or 30); outs = []
    for q in (0.03, 0.25, 0.5, 0.75):     # opening frame too (2210 S058.2: the bottle showed only in its first 1.4 s)
        out = Path(run)/'design/frames'/f"{shot['id']}-{int(q * 100)}.jpg"
        if not out.is_file():
            out.parent.mkdir(parents=True, exist_ok=True)
            t = (shot['in_frame'] + (shot['out_frame'] - shot['in_frame']) * q) / fps
            subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-y', '-ss', f'{t:.3f}', '-i', rec['source'], '-frames:v', '1', '-q:v', '2', str(out)], check=True)
        outs.append(out)
    return outs


def screen_hint(run, sid):
    f = Path(run)/'screen'/f"{sid}.json"
    if not f.is_file(): f = Path(run)/'screen'/f"{sid.split('.')[0]}.json"   # only virtual plan pieces need the parent
    if not f.is_file(): return None
    s = read(f); return (s.get('raw') or {}).get('visible_evidence') if s.get('verdict') == 'replace' else None


def planned_frames(run):
    """{shot or plan piece id: {'id', 'in_frame', 'out_frame'}} on the source frame grid. `plan` splits a shot longer
    than a segment into pieces (S055.1, S055.2 ...) that are not in shots.json and have no screening clip (2026-11-20
    film 2210: one 30 s shot became three segments)."""
    run = Path(run); rec = read(run/'shots/shots.json'); out = {s['id']: s for s in rec['shots']}
    plan = read(run/'plan/segments.json'); fps = float(plan.get('fps') or rec.get('fps') or 30)
    for seg in plan['segments']:
        for sid, a, b in seg.get('shot_times') or []:
            if sid not in out:
                out[sid] = {'id': sid, 'in_frame': seg['in_frame'] + round(float(a) * fps), 'out_frame': seg['in_frame'] + round(float(b) * fps)}
    return out


def piece_clip(run, shot):
    """The screening clip of a shot, or a clip cut from the source for a plan piece (RUN/design/clips/)."""
    clip = Path(run)/'clips'/f"{shot['id']}.mp4"
    if clip.is_file(): return clip
    import media_pipeline as media
    rec = read(Path(run)/'shots/shots.json'); fps = float(rec.get('fps') or 30)
    # A manual hard-cut child (S035a/b), like a long-shot piece (S025.1),
    # has no screening clip: extract its exact planned source window.
    if shot['id'] in {s['id'] for s in rec['shots']}: return clip
    out = Path(run)/'design/clips'/f"{shot['id']}.mp4"
    if not out.is_file():
        out.parent.mkdir(parents=True, exist_ok=True)
        a = shot['in_frame'] / fps; d = (shot['out_frame'] - shot['in_frame']) / fps
        subprocess.run([media.binary('ffmpeg'), '-v', 'error', '-y', '-ss', f'{a:.3f}', '-t', f'{d:.3f}', '-i', rec['source'],
                        '-vf', 'scale=-2:640', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-c:a', 'aac', '-b:a', '96k', str(out)], check=True)
    return out


def cmd_observe(a):
    import agent_native
    run = Path(a.run).resolve()
    shots = planned_frames(run)
    want = a.shots.split(',') if a.shots else sorted(planned_shots(run))
    if getattr(a, 'import_obs', None):
        raw = read(a.import_obs)
        rows = ({r['shot']: r for r in raw if isinstance(r, dict) and r.get('shot')}
                if isinstance(raw, list) else dict(raw))
        saved = []
        for sid, obs in rows.items():
            rec = {'shot': sid, **obs}
            save(run/'design/obs'/f'{sid}.json', rec)
            saved.append(sid)
        print(json.dumps({'mode': 'antigravity_native', 'observed': len(saved), 'shots': sorted(saved),
                          'dir': str(run/'design/obs'), 'next': f'swap_design.py decide --run {run}'}, ensure_ascii=False, indent=2))
        return
    if agent_native.is_native():
        still_paths = []
        clips = {}
        for sid in want:
            if sid not in shots:
                continue
            still_paths.extend(stills(run, shots[sid]))
            c = piece_clip(run, shots[sid])
            if c.is_file():
                clips[sid] = str(c)
        grids = agent_native.tile_sheets(still_paths, run/'agent_review/design_obs_grids', prefix='obs_grid', cols=4, per_grid=16) if still_paths else []
        print(json.dumps({
            'mode': 'antigravity_native',
            'shots': want,
            'obs_grids': grids,
            'clips': clips,
            'schema': OBS_SCHEMA,
            'dir': str(run/'design/obs'),
            'next': f'用 read_file 看 obs_grids（可疑镜头直接看 clips[sid]），按 schema 写出观察 JSON {{"S###": {{...}}}} 后执行 swap_design.py observe --run {run} --import-obs FILE.json，再跑 swap_design.py decide --run {run}',
        }, ensure_ascii=False, indent=2))
        return
    import flash_screen as fs, google_api as api
    def one(sid):
        clip = piece_clip(run, shots[sid])
        if not clip.is_file(): raise SystemExit(f'{clip} missing: run swap.py screen first')
        hint = screen_hint(run, sid)
        parts = [('text', OBS_PROMPT), *fs.competitor_anchor_parts(run), ('text', 'COMPETITOR: ' + fs.competitor_facts(run)),
                 ('text', 'SHOT:'), ('video', str(clip)), ('text', 'THREE FRAMES OF THIS SHOT AT FULL RESOLUTION:'),
                 *[('image', str(f)) for f in stills(run, shots[sid])]]
        if hint: parts.append(('text', 'An earlier screening of this shot noted (check it, it may be wrong about the kind): ' + hint))
        for attempt in range(3):                     # transport hiccups: retry; one bad shot never stops the batch
            try:
                st = fs.call_flash(parts, OBS_SCHEMA, run, None, fs.DEFAULT_FPS); break
            except api.AdapterError as exc:
                err = str(exc)[:200]
        else:
            return sid, err
        save(run/'design/obs'/f'{sid}.json', {'shot': sid, **st['result']})
        return sid, None
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex: done = list(ex.map(one, want))
    failed = {s: e for s, e in done if e}
    print(json.dumps({'observed': len(done) - len(failed), 'failed': failed, 'dir': str(run/'design/obs')}, ensure_ascii=False))
    if failed: raise SystemExit('some shots failed; run observe again (finished shots are cached)')


def product_shots(run):
    """Shots the merge (Pro + Flash + Claude, 宁多换不漏换) put in a segment's product shots."""
    return {sid for s in read(Path(run)/'plan/segments.json')['segments'] for sid in (s.get('product_shots') or [])}


def reviewed_no_product(run, sid):
    """True when detect/overrides.json marks the shot "physical_competitor": false (the agent's reviewed finding)."""
    ov = Path(run)/'detect/overrides.json'
    if not ov.is_file(): return False
    o = read(ov); r = o.get(sid) or o.get(sid.split('.')[0]) or {}
    return r.get('physical_competitor') is False


def seen_competitor(run, sid):
    """R0 needs a competitor SEEN in the shot: Flash's screening answered competitor_visible=yes, or Claude's override
    marks the shot visual. A shot in a segment only for its lines (Flash speech_mentions_product, a Pro dialogue/VO
    rewrite) or under a Pro node's loose timecode is not one (2026-11-20 film 1753: R0 put an opened packet in the hand
    of 11 shots that show no product at all -- four talking heads flagged for "unas gotas", a fairy animation, faces
    under a Pro node that was 6 s off)."""
    run = Path(run); base = sid.split('.')[0]
    ov = run/'detect/overrides.json'
    overrides = read(ov) if ov.is_file() else {}
    reviewed = overrides.get(sid) or overrides.get(base) or {}
    # A reviewed ingredient close-up can need picture/word changes without
    # containing a bottle. Do not let a watermark false positive invoke R0.
    if reviewed.get('physical_competitor') is False: return False
    f = run/'screen'/f'{sid}.json'
    if not f.is_file(): f = run/'screen'/f'{base}.json'
    if f.is_file() and str(((read(f).get('raw') or {}).get('competitor_visible') or '')).lower() == 'yes': return True
    ov = run/'detect/overrides.json'
    o = (read(ov) if ov.is_file() else {}).get(base) or {}
    return o.get('decision') != 'keep' and 'visual' in (o.get('kinds') or []) and bool(o)


def follow_into_carton(run, out):
    """R5b: a take-from-the-bag shot (R5) whose next shot in the same segment shows the item as a carton takes a carton
    out of the bag (2026-11-20 film 1753: S132 a hand lifts the bottle out of a handbag and stands it on the counter,
    S133 shows it standing; R5 made it a packet and R9 a carton -- the product would change shape at the cut)."""
    segs = read(Path(run)/'plan/segments.json')['segments']
    for seg in segs:
        ids = [sid for sid in (seg.get('shot_ids') or []) if sid in out]
        for sid, nxt in zip(ids, ids[1:]):
            d, n = out[sid], out[nxt]
            pocket = (d.get('observation') or {}).get('retrieval_container') == 'pocket'
            if d.get('rule') == 'R5-口袋' and n.get('form') == 'carton' and pocket:
                out[nxt] = {**n, 'rule': 'R5c-口袋条包接续', 'form': 'sealed_packet', 'placement': 'in_hand', 'action': 'redesign',
                            'redesign': '接过条包的人拇指和食指捏着同一支未开封条包下半部，正面朝镜头，自然转着查看',
                            'views': ['handheld_photo'], 'template': 'packet', 'count': 1}
            if d.get('rule') == 'R5-口袋' and n.get('form') == 'carton' and not pocket:
                out[sid] = {**d, 'rule': 'R5b-包里拿出纸盒', 'form': 'carton', 'placement': 'in_hand', 'action': 'keep', 'redesign': '',
                            'views': list(n.get('views') or CARTON_VIEWS), 'template': n.get('template') or 'carton', 'count': 1}
            if d.get('form') == 'carton' and n.get('rule') == 'R7-小而近人'                     and (n.get('observation') or {}).get('hand_action') == 'place_down':
                out[nxt] = {**n, 'rule': 'R7c-同物放下', 'form': 'carton', 'placement': 'standing',
                            'action': 'keep', 'redesign': '', 'views': list(CARTON_VIEWS), 'template': 'carton', 'count': 1}
    return out


def follow_held_object(run, out):
    """R0c: an R0 shot (the screening or the agent saw a competitor, the observation listed nothing) inside a segment
    whose nearest product shot holds the item in a hand is that same object seen from the other side, not a new opened
    packet (2026-11-22 film 1603578481269031 G02: S060-S062 and S064-S065 a carton in the man's hand; in the reverse
    shots S063 / S066 the bottle sat on the frame edge, R0 made it an opened packet, and the thing in his hand changed
    shape at every cut). The shot before wins over the shot after."""
    segs = read(Path(run)/'plan/segments.json')['segments']
    held = lambda d: bool(d.get('form')) and d.get('placement') == 'in_hand' and not str(d.get('rule', '')).startswith('R0')
    product = lambda d: bool(d.get('form')) and not str(d.get('rule', '')).startswith('R0')
    text = {'carton': '人物手里拿着和相邻镜头同一个纸盒：原来拿着东西的那只手握住盒身下半部，正面朝镜头；画面只带到这只手的一部分时，纸盒也只露出一部分',
            'sealed_packet': '人物手里捏着和相邻镜头同一支未开封条包：拇指和食指捏住条包下半部，正面朝镜头；画面只带到这只手的一部分时，条包也只露出一部分',
            'opened_packet': '人物手里捏着和相邻镜头同一支已开封条包：拇指和食指捏住条包下半部，开口朝上；画面只带到这只手的一部分时，条包也只露出一部分'}
    for seg in segs:
        ids = [sid for sid in (seg.get('shot_ids') or []) if sid in out]
        for i, sid in enumerate(ids):
            d = out[sid]
            if d.get('rule') != 'R0-筛查有观察无': continue
            before = next((out[x] for x in reversed(ids[:i]) if product(out[x])), None)
            after = next((out[x] for x in ids[i + 1:] if product(out[x])), None)
            src = before if before and held(before) else (after if after and held(after) else None)
            if not src or src['form'] not in text: continue
            out[sid] = {**d, 'rule': 'R0c-同物接续', 'form': src['form'], 'placement': 'in_hand', 'action': 'redesign', 'redesign': text[src['form']],
                        'views': list(src.get('views') or []), 'template': src.get('template'), 'count': src.get('count') or 1}
    return out


def _shot_no(sid):
    m = re.match(r'S(\d+)(?:\.(\d+))?', sid)
    return (int(m.group(1)), int(m.group(2) or 0)) if m else (10 ** 9, 0)


def unify_held_object(run, out, reach=2):
    """R7d / R5d: one object across neighbouring shots keeps one form. Product shots are taken in film order (across
    segment edges: 1429516565935964 S063 in G01, S064 in G02); two are neighbours when no more than `reach` shots
    apart. An R7 opened-packet fallback next to a carton becomes that carton, held (R7d); a pocket shot (R5, physics:
    only a packet fits) turns the held cartons next to it into the sealed packet (R5d). Sipping (R1) and taking from an
    open carton (R3) are left alone: there the packet is a different object."""
    carton_text = '人物手里握着和相邻镜头同一个纸盒：原来拿着东西的那只手握住盒身下半部，正面朝镜头；画面只带到这只手的一部分时，纸盒也只露出一部分'
    packet_text = '人物手里捏着和相邻镜头同一支未开封条包：拇指和食指捏住条包下半部，正面朝镜头；画面只带到这只手的一部分时，条包也只露出一部分'
    held = lambda d: bool(d.get('form')) and d.get('placement') == 'in_hand'
    pocket = lambda d: str(d.get('rule', '')).startswith('R5-') and (d.get('observation') or {}).get('retrieval_container') in (None, 'pocket')
    ids = sorted((sid for sid, d in out.items() if d.get('form')), key=_shot_no)
    def neighbours(i):
        return [out[ids[j]] for j in (i - 1, i + 1) if 0 <= j < len(ids) and abs(_shot_no(ids[j])[0] - _shot_no(ids[i])[0]) <= reach]
    for i, sid in enumerate(ids):
        d = out[sid]
        if d.get('rule') == 'R7-小而近人' and d.get('placement') == 'in_hand' and any(n.get('form') == 'carton' for n in neighbours(i)):
            out[sid] = {**d, 'rule': 'R7d-同物接续', 'form': 'carton', 'action': 'redesign', 'redesign': carton_text,
                        'views': list(CARTON_VIEWS), 'template': 'carton', 'count': 1}
    # after a sip (R1) the packet in that hand is opened: the held packet / carton in the next shots is that opened
    # packet (2026-11-22 film 140 G05: S227 sips, S228 sealed packet, S229 a carton; 283 G17 sealed again)
    opened_text = '人物手里捏着和上一镜同一支已开封条包：拇指和食指捏住条包下半部，开口朝上；画面只带到这只手的一部分时，条包也只露出一部分'
    for i, sid in enumerate(ids):
        if out[sid].get('rule') != 'R1-服用': continue
        for j in range(i + 1, len(ids)):
            n = out[ids[j]]
            if abs(_shot_no(ids[j])[0] - _shot_no(ids[j - 1])[0]) > reach or not held(n) or str(n.get('rule', '')).startswith(('R1', 'R3', 'R4', 'R5')): break
            if n.get('form') != 'opened_packet':
                out[ids[j]] = {**n, 'rule': 'R1b-服用后接续', 'form': 'opened_packet', 'action': 'redesign', 'redesign': opened_text,
                               'views': ['opened_packet_hand'], 'template': 'packet', 'count': 1}
    changed = True
    while changed:
        changed = False
        for i, sid in enumerate(ids):
            d = out[sid]
            if not (d.get('form') == 'carton' and held(d)) or str(d.get('rule', '')).startswith(('R3', 'R4', 'R5b')): continue
            if any(pocket(n) or n.get('rule') == 'R5d-口袋同物' for n in neighbours(i)):
                out[sid] = {**d, 'rule': 'R5d-口袋同物', 'form': 'sealed_packet', 'action': 'redesign', 'redesign': packet_text,
                            'views': ['handheld_photo'], 'template': 'packet', 'count': 1}
                changed = True
    return out


def cmd_decide(a):
    run = Path(a.run).resolve(); seg_of = planned_shots(run); merged = {s for s in product_shots(run) if seen_competitor(run, s)}; out = {}
    source_list = run/'shots/shots.json'
    source_ids = {s['id'] for s in read(source_list)['shots']} if source_list.is_file() else None
    ov_path = run/'detect/overrides.json'; ovs = read(ov_path) if ov_path.is_file() else {}
    for f in sorted((run/'design/obs').glob('S*.json')):
        obs = read(f); sid = obs['shot']
        o = ovs.get(sid) or ovs.get(sid.split('.')[0]) or {}
        if o.get('screen') in ('phone', 'computer'): obs = {**obs, 'on_screen': o['screen']}     # the agent's reviewed finding wins
        # a plan piece (S058.2) shares its shot's screening, which covers the whole shot, not this part of it
        # (2026-11-20 film 2210: the man had stood up without the bottle; R0 would have put a packet in his hand)
        whole_source_shot = sid in source_ids if source_ids is not None else '.' not in sid
        screened = whole_source_shot and sid in merged
        d = decide(obs, screened_competitor=screened)
        if reviewed_no_product(run, sid):
            # the agent looked at the sheets and recorded no physical product (a laptop back, a dried drop, a
            # picture on paper); an observation that "sees" bottles there does not put a product in (2026-11-22 film
            # 1409783017240926 S211 -> carton standing; 165 S024 -> carton on an empty ring)
            d = {'removals': [], 'count': None, 'rule': '核实无实体产品', 'form': None, 'placement': None, 'action': 'keep',
                 'redesign': '', 'views': [], 'template': None}
        prev = f"{sid.rsplit('.', 1)[0]}.{int(sid.rsplit('.', 1)[1]) - 1}" if not whole_source_shot and sid.rsplit('.', 1)[-1].isdigit() else None
        if not d.get('form') and prev in out and out[prev].get('form'):
            # a plan piece continues the shot of the piece before it: same product, same form (2026-11-20 film 2210:
            # S058.2 looked empty in its stills, yet its first 1.4 s still showed the bottle S058.1 held -> carton)
            d = {**{k: v for k, v in out[prev].items() if k not in ('segment', 'observation')}, 'rule': f'同镜接续（沿用 {prev}）'}
        elif not d.get('form') and not whole_source_shot and sid in merged:
            d = decide(obs, screened_competitor=True)      # first piece of its shot with nothing seen: screening fallback
        out[sid] = {'segment': seg_of.get(sid), **d, 'observation': {**{k: obs[k] for k in ('items', 'hand_action', 'shot_scale', 'who_handles')}, **({'retrieval_container': obs['retrieval_container']} if 'retrieval_container' in obs else {})}}
    follow_into_carton(run, out)
    unify_held_object(run, out)
    follow_held_object(run, out)
    save(run/'plan/swap_design.json', {'rules': RULES, 'shots': out})
    for sid, d in out.items(): print(sid, d['segment'], d['rule'], d['form'], d['placement'], d['action'])
    import swap      # segment prompt skeletons nobody has started writing are rewritten from the design (user 2026-11-20)
    print(json.dumps({'skeletons_rewritten': swap.rewrite_untouched_stubs(run, out)}, ensure_ascii=False))


FORM_ZH = {'carton': '1盒yesnap LymphLoop青绿色纸盒', 'sealed_packet': '1支未开封的yesnap LymphLoop青绿色条包',
           'opened_packet': '1支已开封的yesnap LymphLoop青绿色条包', 'carton+sealed_packet': '1盒已翻开顶盖的LymphLoop纸盒和从里面抽出的1支未开封条包',
           'phone_page': '手机屏幕上的yesnap LymphLoop商品页', 'desktop_page': '电脑屏幕上的yesnap LymphLoop商品页'}
IMG_ROLE = {'carton_views': '用于提取单个yesnap LymphLoop青绿色竖长纸盒的三维外观、颜色、正面标签、侧面竖排标志与顶盖，不采用九宫格多视图版式和白底。',
            'carton_hand': '仅用于锁定单个纸盒相对成年人手掌的真实尺寸比例（盒宽撑满虎口）及手指包握盒身左右两侧的抓握方式，不采用图中的手与浅灰色背景。',
            'handheld_photo': '用于未开封条包的外观（扁平青绿色长条软袋、正面白色品牌标签）和它捏在指尖时相对手指的长短宽窄，不采用图中的手与浅灰色背景。',
            'opened_packet_hand': '用于已开封条包的外观（顶端撕开后短而略皱的平口、青绿袋身、白色标签）和拇指食指捏住袋身下半部的握法，不采用图中的手与浅灰色背景。',
            'phone_page': '用于手机屏幕里显示的商品页：页面版式、主图里的LymphLoop纸盒和条包、标题文字，整页替换原片手机屏幕上的竞品页面，不采用图中的价格数字和状态栏时间。',
            'desktop_page': '用于电脑屏幕里显示的商品页：页面版式、主图里的LymphLoop纸盒和条包、标题文字，整页替换原片电脑屏幕上的竞品页面，不采用图中的价格数字。'}


CLOSE_UP_MARK = '# carton-close-up'


def carton_views_ok(views, raw):
    """The carton images of a prompt: both, in the order carton_views, carton_hand. One exception (user 2026-11-20,
    film 2210 G01): a close-up of a carton held in a hand, framed like carton_hand itself, may send carton_views alone
    when the prompt carries the line '# carton-close-up' -- carton_hand is a hand holding the carton in close-up on a grey
    carpet, and in such a shot the model took its carpet as the background despite '不采用地毯背景'."""
    # The close-up exception (carton_views alone with '# carton-close-up') was withdrawn the same day: without the hand
    # image the carton lost its proportions (Codex 30542865505302802 G16R16: tall and thin, a second header block).
    # The user replaces carton_hand with a copy whose carpet background is gone instead.
    if not any(v in views for v in CARTON_VIEWS): return True
    return views[:2] == CARTON_VIEWS


SECTIONS = ('【生成目标】', '【参考素材职责】', '【主体与关系】', '【事件脚本】', '【保持一致】')
CN_NUM = '一二三四五六七八九十'


def stage_label(i):
    """1 -> 一, 12 -> 十二 (the user's Gemini prompts number stages 阶段一, 阶段二)."""
    if i <= 10: return CN_NUM[i - 1]
    if i < 20: return '十' + CN_NUM[i - 11]
    return str(i)


def views_of(rows):
    views = []
    for r in rows:
        for v in r.get('views') or []:
            if v not in views: views.append(v)
    if 'carton_views' in views: views = CARTON_VIEWS + [v for v in views if v not in CARTON_VIEWS]
    return views


def forms_of(rows):
    forms = []
    for r in rows:
        if r.get('form') and FORM_ZH[r['form']] not in forms: forms.append(FORM_ZH[r['form']])
    return forms


def physics_of(views):
    out = ''
    if 'carton_views' in views: out += '纸盒与人物、桌面同一种写实画面质感，直角硬挺不变形。'
    if {'handheld_photo', 'opened_packet_hand'} & set(views): out += '条包是扁平柔软的薄袋，始终在手里或平放，不自己立着。'
    return out


# 用户 2026-11-25："产品在每个镜头里都是忽大忽小的……人眼看产品到底是偏大还是偏小，基本都是有参照物，例如和人脸的比例，
# 和手的比例，抓握时候手的开合是大还是小……每个生成的镜头，都要去找当下镜头里的某个参照物"。九视图和手持图只管正面和握法，
# 管不住大小：每个有产品的阶段写一句“大小参照：”，用这一镜里看得见的东西（手的张开、脸、身边的杯子手机……）说清产品多大。
SCALE_HOLE = ('大小参照：【写：画面里有人脸就先写产品和人脸的比例，再写和离产品最近的实际物体的比例——拿在手里写手张开多大、'
              '盒比手掌宽还是窄，放在桌上写和旁边杯子手机的比例；不拿竞品或任何要移除、替换的东西比；'
              '按 rules-now.md“每镜大小参照”的对照表，不写厘米】。')
# 用户 2026-11-25："在当前镜头内找参照物不会是拿竞品做参照物吧，要找人脸，找镜头内离产品最近的实际物体做参照物"。
# 竞品和要移除的东西在生成画面里不存在（或已换成我方产品），拿它比等于让模型照竞品的大小画。
NOT_A_REFERENCE = ('竞品', '原片', '原来', '原瓶', '被替换', '滴剂', '滴管', '滴瓶', '药瓶', '胶囊', '软糖', '片剂', '粉剂', '琥珀色')
SCALE_KEEP = '产品在每一镜里和人脸、手、身边物件的大小关系都一样，不忽大忽小；'
# 用户 2026-11-25："把在当前镜头内找参照物描述产品比例的做法也同样放在首次生成里……带原视频参考生成出来的才最理想"。
# 挂参考视频时，模型会沿用原片竞品容器的大小和手的张开幅度（批 62：“本段全部继承了原滴剂瓶和手部的相对比例”），
# 所以 @视频1 的职责句里明说大小不跟参考视频走。
SCALE_VIDEO = '产品的大小不沿用原片里被替换容器的大小和手的张开幅度，按事件脚本里每一镜写的大小参照来画。'
PRODUCT_WORDS = ('纸盒', '条包')


def act_of(r):
    """The product action of one shot, from its design row (the agent fills only the 【写】 parts)."""
    if r['action'] == 'redesign': return r['redesign']
    noun = '纸盒' if 'carton' in (r.get('form') or '') else '条包'
    hold = '五指包握纸盒左右两侧' if noun == '纸盒' else '拇指和食指捏住条包下半部，正面朝镜头'
    return {'in_hand': f'【写：哪只手（按画面左右）】{hold}，【写：动到哪】',
            'standing': f'{noun}底面着地稳立在原物的位置，正面朝镜头',
            'shipping_box': '快递箱里整齐竖放一排LymphLoop纸盒，【写：手怎么提起一盒】'}.get(r['placement'], '【写】')


def skeleton(design_rows, shot_ids):
    """Five-section skeleton for consecutive shots from their design rows (single-shot regeneration on 100x)."""
    rows = [design_rows[s] for s in shot_ids]
    views = views_of(rows); forms = forms_of(rows)
    removals = sorted({x for r in rows for x in r['removals']})
    lines = [f'# views: {",".join(views)}', f'# design: {",".join(shot_ids)}', '【生成目标】',
             f'复刻@视频1的【写：几个镜头的景别切换、环境、人物神态】与【写：语言】台词，将原片中的{"、".join(removals) or "竞品"}完全替换为{"和".join(forms)}'
             + ('，并按@图片2的手掌比例写抓握。' if 'carton_hand' in views else '。'), '', '【参考素材职责】',   # ratio is a request parameter (user 2026-11-20)
             f'@视频1用于复刻【写：人物长相服装、陈设】、镜头切换机位与时机和说话口型节奏，完全移除原片中的{"、".join(removals) or "竞品"}及字幕。' + SCALE_VIDEO]
    lines += [f'@图片{i}{IMG_ROLE[v]}' for i, v in enumerate(views, 1)]
    count = '全程仅' + '和'.join(forms)
    lines += ['', '【主体与关系】', f'【写：光线、场景、人物】。画面里只有{"和".join(forms)}，原片竞品所在的其他位置是空的。' + physics_of(views)
              + '音色——【写：每个说话人一句：年龄、口音、音色、语气，不是播音腔】', '', '【事件脚本】']
    for i, (sid, r) in enumerate(zip(shot_ids, rows), 1):
        lines.append(f'# 阶段{stage_label(i)} = {sid}（{r["rule"]}）')     # analysis stays in comments, never in the body
        lines.append(f'阶段{stage_label(i)}：{"切至" if i > 1 else ""}【写：景别和机位】。{act_of(r)}。{SCALE_HOLE}【写：说话人】台词（【写：语气】，用【写：语言】）：{{【写：原词】}}。结束时【写：产品和人的终态】。')
    lines += ['', '【保持一致】', f'{count}，' + ('盒身尺寸与手掌比例严格匹配@图片2，硬挺无挤压变形；' if 'carton_hand' in views else '')
              + SCALE_KEEP + '人物面容服装、道具、空间方向与光线同@视频1一致；听者自然闭口聆听；无bgm，只保留环境声、动作声和台词；画面不显示字幕。']
    return '\n'.join(lines) + '\n'


SECTION_RE = r'{}(.*?)(?=【(?:生成目标|参考素材职责|主体与关系|事件脚本|保持一致)】|\Z)'
STAGE_RE = re.compile(r'阶段([一二三四五六七八九十\d]+)\s*[：:]')


def section(body, name):
    m = re.search(SECTION_RE.format(re.escape(name)), body, re.S)
    return m.group(1) if m else ''


def stages(body):
    """[(label, text)] of the 【事件脚本】 stages, in order."""
    script = section(body, '【事件脚本】')
    marks = list(STAGE_RE.finditer(script))
    return [(m.group(1), script[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(script))].strip()) for i, m in enumerate(marks)]


def scale_problems(body, st=None):
    """Every stage that shows our product names an in-shot size reference, and the product keeps one size across shots
    (user 2026-11-25: 忽大忽小 is what makes it look fake; front views and the hand photo do not fix size)."""
    errs = []
    removed = tuple(x.strip() for grp in re.findall(r'完全移除原片中的([^。]*?)(?:及字幕|。)', body)
                    for x in re.split(r'[、，,]|和|与', grp) if len(x.strip()) >= 2 and '【' not in x)
    for label, text in (st if st is not None else stages(body)):
        if not any(w in text for w in PRODUCT_WORDS): continue
        m = re.search(r'大小参照[：:]\s*([^。]*)', text)
        if not m or len(m.group(1).strip()) < 6:
            errs.append(f'阶段{label} 有产品，没写“大小参照：”——找这一镜里看得见的参照物（手的张开、脸、身边杯子手机等）写清产品和它的大小关系（rules-now.md“每镜大小参照”）')
            continue
        bad = [w for w in NOT_A_REFERENCE + removed if w in m.group(1)]
        if bad:
            errs.append(f'阶段{label} 的大小参照拿“{bad[0]}”比：竞品和要移除、替换的东西在生成画面里不存在，不能当参照——先找人脸，再找离产品最近的实际物体（手、杯子、手机等）')
    if any(any(w in t for w in PRODUCT_WORDS) for _, t in (st if st is not None else stages(body))) and '忽大忽小' not in section(body, '【保持一致】'):
        errs.append('【保持一致】写“产品在每一镜里和人脸、手、身边物件的大小关系都一样，不忽大忽小”')
    return errs


def five_section_problems(body, n_shots=None, speech=False, n_images=None, video=True):
    """Prompt lint from the Gemini clone-seedance-video skill the user's one-shot prompts came from (2026-11-20):
    all five sections, one stage per reference shot each ending in an observable end state (结束时…), lines as {原词},
    listeners' mouths closed, no analysis (shot / rule ids) in the body."""
    errs = [f'缺 {s}' for s in SECTIONS if s not in body]
    st = stages(body)
    if not st: errs.append('【事件脚本】按镜头分阶段写：“阶段一：…结束时…。阶段二：切至…”')
    for label, text in st:
        if '结束时' not in text: errs.append(f'阶段{label} 没有写“结束时……”的终态')
    # at least one stage per detected shot; more is fine: dissolves and flares hide cuts from shot detection
    # (2026-11-20 film 2210 S055: four camera set-ups inside one detected shot)
    if n_shots and st and len(st) < n_shots:
        errs.append(f'参考片有 {n_shots} 个镜头，【事件脚本】只写了 {len(st)} 个阶段：每个镜头至少一个阶段，按顺序')
    if re.search(r'[“"][^”"]*[A-Za-z]{3,}[^”"]*[”"]', section(body, '【事件脚本】')):
        errs.append('台词写成 {原词}（花括号），不用引号')
    if speech and '闭口' not in body: errs.append('有台词：写“听者自然闭口”（【保持一致】或说话那一阶段）')
    errs += scale_problems(body, st)
    # every asset accounted for (Gemini skill rule 4): @视频1 and each uploaded image get their own role line, one asset
    # per line, and nothing refers to an image that is not uploaded
    roles = section(body, '【参考素材职责】')
    # card mode (goof redos, user 2026-11-23): no reference video, so nothing may point at one
    if video and '@视频1' not in roles: errs.append('【参考素材职责】缺 @视频1 的职责句')
    if video and any(any(w in t for w in PRODUCT_WORDS) for _, t in st) and '不沿用' not in roles:
        errs.append('挂参考视频又有产品：【参考素材职责】@视频1 那句后面写“' + SCALE_VIDEO + '”（rules-now.md“每镜大小参照”）')
    if not video and re.search(r'@视频\d', body): errs.append('人物卡模式没有参考视频：去掉 @视频1，场景、机位、动作都直接写出来')
    for line in roles.splitlines():
        if len(set(re.findall(r'@图片(\d+)', line))) > 1: errs.append(f'一行只写一张图的职责（不要“@图片1和@图片2用于…”）：{line.strip()}')
    if n_images is not None:
        for i in range(1, n_images + 1):
            if not re.search(rf'^\s*@图片{i}(?!\d)', roles, re.M): errs.append(f'@图片{i} 上传了，【参考素材职责】里没有它单独一行的职责（用于……，不采用……）')
        extra = sorted({int(k) for k in re.findall(r'@图片(\d+)', body)} - set(range(1, n_images + 1)))
        if extra: errs.append(f'提示词提到 @图片{extra}，但只上传了 {n_images} 张图')
    if re.search(r'@视频[2-9]', body): errs.append('只有 @视频1 一段参考视频')
    if re.search(r'(?<![A-Za-z])R\d+b?-|(?<![A-Za-z0-9])S\d{3}(?!\d)', body):
        errs.append('正文里有规则号或镜头号：分析写在 # 注释里，不进提示词')
    return errs


OLD_PHRASES = ['这不是视频编辑', '更换为', '动作照@视频1', '照它', '厘米']


def check_against_design(raw, design_rows):
    """Errors when a prompt does not match its design rows (the agent decides nothing the rule table decided)."""
    head = {l.split(':', 1)[0][2:].strip(): l.split(':', 1)[1].strip() for l in raw.splitlines() if l.startswith('# ') and ':' in l}
    ids = [s for s in head.get('design', '').split(',') if s]
    if not ids: return []
    rows = [design_rows[s] for s in ids if s in design_rows]
    if len(rows) != len(ids): return [f"design 里没有这些镜头：{[s for s in ids if s not in design_rows]}"]
    rows = [r for r in rows if r.get('form')]
    body = '\n'.join(l for l in raw.splitlines() if not l.startswith('#'))
    errors = []
    want = views_of(rows)
    got = [v.strip() for v in head.get('views', '').split(',') if v.strip()]
    close_up_exception = (want == CARTON_VIEWS and got == ['carton_views'] and CLOSE_UP_MARK in raw)
    if got != want and not close_up_exception: errors.append(f'# views 应为 {",".join(want)}（设计表），现在是 {",".join(got)}')
    for sec in ('【生成目标】', '【参考素材职责】', '【主体与关系】', '【事件脚本】', '【保持一致】'):
        if sec not in body: errors.append(f'缺 {sec}')
    forms = {r['form'] for r in rows}
    if any('carton' in f for f in forms) and '纸盒' not in body: errors.append('设计是纸盒，提示词里没有纸盒')
    if forms & {'opened_packet', 'sealed_packet', 'carton+sealed_packet'} and '条包' not in body: errors.append('设计有条包，提示词里没有条包')
    if not any('carton' in f for f in forms) and not forms & {'phone_page', 'desktop_page'} and '纸盒' in body: errors.append('设计里没有纸盒，提示词却写了纸盒')
    if any(r.get('count') == 1 for r in rows) and not ('仅1' in body or '只有1' in body or '全程仅' in body):
        errors.append('数量：写“全程仅1盒/1支”')
    if '已开封' in body and 'opened_packet' not in forms and 'opened_packet_hand' not in got: errors.append('设计没有已开封条包')
    errors += [f'旧写法：“{w}”' for w in OLD_PHRASES if w in body]
    if '【写' in body: errors.append('还有没填的【写…】')
    return errors


def cmd_skeleton(a):
    run = Path(a.run).resolve(); design = read(run/'plan/swap_design.json')['shots']
    ids = a.shots.split(',')
    missing = [s for s in ids if s not in design]
    if missing: raise SystemExit(f'no design for {missing}: run observe + decide first')
    out = run/'shots-regen'/f'{a.name}.txt'
    if out.exists() and not a.force: raise SystemExit(f'{out} exists (--force to overwrite)')
    out.parent.mkdir(exist_ok=True); out.write_text(skeleton(design, ids), encoding='utf-8'); print(out)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    q = sub.add_parser('observe'); q.add_argument('--run', required=True); q.add_argument('--shots'); q.add_argument('--import-obs', help='Antigravity native mode: import agent-written shot observation JSON {S###: {...}}'); q.set_defaults(fn=cmd_observe)
    q = sub.add_parser('decide'); q.add_argument('--run', required=True); q.set_defaults(fn=cmd_decide)
    q = sub.add_parser('skeleton'); q.add_argument('--run', required=True); q.add_argument('--seg'); q.add_argument('--shots', required=True)
    q.add_argument('--name', required=True); q.add_argument('--force', action='store_true'); q.set_defaults(fn=cmd_skeleton)
    a = p.parse_args(); a.fn(a)


if __name__ == '__main__':
    main()
