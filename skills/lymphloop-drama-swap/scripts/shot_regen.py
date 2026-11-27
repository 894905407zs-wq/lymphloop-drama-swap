#!/usr/bin/env python3
"""Regenerate single shots of a segment and put them back in place (user 2026-11-19: "这种非常困难的镜头，你需要一个个
镜头生成，不足4秒的镜头把原段落放慢凑足4秒然后单独发起人物生成"; "G16…把每个出现产品的镜头都单独生成…回剪的时候把两个镜头拆开").

Four steps, all on the segment's caption-cleaned reference (RUN/clips/G##.hq.clean.ref.mp4, competitor left in):

  ref      cut the shot by reference frame numbers [A, B) and make the upload clip RUN/shots-regen/NAME.ref.mp4
           - one range shorter than --seconds (4): slowed to exactly 4 s, silent (the segment's own sound is used at splice)
           - one range of 4 s or more: real speed, last frame held to the whole second, its own sound
           - several ranges (one person's product shots that cut back and forth): joined end to end at real speed,
             sound of the first range kept and the others muted (--keep-sound i,j to keep more), last frame held
           also writes NAME.strip.png (look at it: first frame, last real frame, held frames) and a prompt stub NAME.txt
  prepare  checks NAME.txt (no 【写】 slot, whole-second timestamps, positive description, no spoon, 竖版 9:16), writes
           NAME.final.txt and the job RUN/jobs/G##-NAME.json with the user's words (--user-quote, required: a shot
           regeneration is paid on top of the segment's one paid generation)
  (generate: apimart_wan.py run-batch RUN/jobs/G##-NAME.json -- APIMART seedance-2.0 720p 9:16, the default since
   2026-11-20; `prepare --channel x100` + x100_gen.py quote/submit/poll only when the user asks for 100x)
  splice   puts every generated shot back over the segment's selected clip at the same frames and makes a new local
           splice G##-aN through `swap.py splice` (sound: a real-speed shot always its own, a slowed shot the segment's clip; was --shot-sound NAME, kept a shot's new
           dialogue when it was generated at real speed), then `swap.py select --job G##-aN` it

    shot_regen.py ref     --run RUN --seg G05 --name S170 --frames 342-402
    shot_regen.py ref     --run RUN --seg G16 --name p --frames 149-233 --frames 289-343
    shot_regen.py prepare --run RUN --name S170 --user-quote "用户原话"
    shot_regen.py splice  --run RUN --seg G05 --out-attempt a3 S170 S168 [--base G05-a1-c1]

Goof redos go in rounds (user 2026-11-22: "补穿帮一轮一批"): review every segment first, then

    shot_regen.py round open   --run RUN --file goofs.json     # locate + ref + prompt for every goof, reasons recorded
    (edit each shots-regen/NAME.txt)
    shot_regen.py round submit --run RUN                        # all prompts checked, all jobs sent together, one wait
    (look at each shots-regen/NAME.result.png)
    shot_regen.py round splice --run RUN [--drop NAME ...]      # one splice + select per segment; then finish.py --from captions
    shot_regen.py reasons RUN [RUN ...]                         # why shots were redone, per goof kind

Card mode (user 2026-11-23: "第一次生成采用原片参考的模式，如果第一次出现穿帮了，第二次补生就要采用人物卡模式"): every redo
is generated from character cards + product images (+ the speaker's voice clip), no reference video. `round open`
writes a card-mode stub (# cards / # views / # voice) by default; `--mode reference` keeps the old way.

    (write RUN/cards/cast.json)  shot_regen.py cards --run RUN     # build the character cards (scripts/cards.py)
"""
from __future__ import annotations
import argparse, hashlib, json, math, re, subprocess, sys, time
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as media  # noqa: E402

TARGET_S = 4.0            # shortest output both channels generate
GAP_FILL = 2              # base frames left between two regenerated shots that the earlier shot absorbs
CARTON_VIEWS = ('carton_views', 'carton_hand')
# user 2026-11-20: "我们先采用APIMART中的seedance-2.0，不要默认采用100x"; ratio 9:16, duration = the shot job's whole
# seconds and resolution 720p are request parameters, never prompt text. 100x only on request, and only its seedance-2.0
# (100x seedance-2.5 has no 9:16 parameter, only adaptive, which follows the prompt).
X100_DEFAULT_MODEL = '100x-minimax-h3'
X100_SETTINGS = {
    '100x-minimax-h3': {'resolution': '768P', 'aspect_ratio': '9:16', 'max_seconds': 15},
    '100x-seedance-2.0': {'resolution': '720P', 'aspect_ratio': '9:16', 'max_seconds': 15},
}
VIDEO_ARGS = ['-c:v', 'libx264', '-preset', 'slow', '-crf', '12', '-maxrate', '1500k', '-bufsize', '3000k', '-pix_fmt', 'yuv420p',
              '-movflags', '+faststart']


def read(p): return json.loads(Path(p).read_text())
def write(p, d): Path(p).write_text(json.dumps(d, ensure_ascii=False, indent=1))
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def ff(): return media.binary('ffmpeg')
def rate_of(path): return float(Fraction(str(media.probe(str(path))['fps'])))


def parse_range(text):
    m = re.fullmatch(r'\s*(\d+)\s*-\s*(\d+)\s*', text)
    if not m or int(m[2]) <= int(m[1]): raise SystemExit(f'--frames A-B (end exclusive, A < B), got {text!r}')
    return [int(m[1]), int(m[2])]


def plan_ref(pieces, fps, seconds=TARGET_S, real_speed=False):
    """How the upload clip is made from reference frame ranges. Returns {mode, factor, seconds, real_s}.
    real_speed: a shot under 4 s in which someone on screen speaks stays at real speed with its own sound and the last
    frame held to 4 s (user 2026-11-20: a slowed shot's sound is strange; lip-sync must be generated, not borrowed)."""
    real = sum(b - a for a, b in pieces) / fps
    if len(pieces) == 1 and real < seconds - 1e-6 and not real_speed:
        return {'mode': 'slow', 'factor': seconds / real, 'seconds': seconds, 'real_s': real}
    return {'mode': 'hold' if len(pieces) == 1 else 'join', 'factor': 1.0,
            'seconds': float(max(seconds, math.ceil(real - 1e-6))), 'real_s': real}


def ref_graph(pieces, fps, plan, keep_sound, mutes=(), hflip=False):
    """ffmpeg filter graph for plan_ref's clip. Input 0 is the cleaned reference; for 'slow' input 1 is silence.
    `mutes`: [(a, b)] seconds of the clip silenced (the old word of a changed line, so the model says the new one).
    `hflip`: mirror the picture, when the segment's generated clip came out mirrored and the regenerated shot has to
    match it (2026-11-20 film 2210 G10: the truck cab came back left-right flipped; a part of it is redone)."""
    flip = ',hflip' if hflip else ''
    if plan['mode'] == 'slow':
        a, b = pieces[0]
        return (f"[0:v]trim=start_frame={a}:end_frame={b},setpts=(PTS-STARTPTS)*{plan['factor']:.6f},fps={fps:g}{flip},"
                f"tpad=stop_mode=clone:stop_duration=0.2[v]"), '1:a'
    g, cat = [], ''
    for i, (a, b) in enumerate(pieces):
        vol = '' if i in keep_sound else ',volume=0'
        g.append(f'[0:v]trim=start_frame={a}:end_frame={b},setpts=PTS-STARTPTS{flip}[v{i}]')
        g.append(f'[0:a]atrim=start={a / fps:.4f}:end={b / fps:.4f},asetpts=PTS-STARTPTS{vol}[a{i}]')
        cat += f'[v{i}][a{i}]'
    # concat may report an unknown frame rate: duration-based tpad can then add zero video frames.
    # Restore the native grid and pad an explicit frame count; -t limits only the integer API tail.
    hold_frames = max(0, round((plan['seconds'] - plan['real_s']) * fps)) + 2
    g.append(f'{cat}concat=n={len(pieces)}:v=1:a=1[vc][ac]')
    silence = ''.join(f",volume=enable='between(t\\,{a:.3f}\\,{b:.3f})':volume=0" for a, b in mutes)
    g.append(f'[vc]fps={fps:g},tpad=stop_mode=clone:stop={hold_frames}[v]'); g.append(f'[ac]apad{silence}[a]')
    return ';'.join(g), '[a]'


PROMPT_STUB = """# views: 【写：按设计表：carton_views,carton_hand / handheld_photo / opened_packet_hand】
# 有设计表就用 swap_design.py skeleton --run RUN --shots S###[,S###] --name {name} --force 覆盖本文件（形态、图、数量、动作由规则表定）。
# 五段式写法和检查见 references/rules-now.md。画幅、时长、分辨率是接口参数，正文里不写，也不写秒数和时间戳。
{slow}【生成目标】
复刻@视频1的【写：景别、环境、人物神态】{speech_goal}，将原片中的【写：竞品逐个：在哪、几个、什么样】完全替换为【写：设计表的产品】。

【参考素材职责】
@视频1用于复刻【写：人物长相服装、陈设】、机位{mouth}，完全移除原片中的【写：同一份竞品清单】及字幕。{scale_video}
【写：每张图一行：@图片N用于……，不采用……】

【主体与关系】
【写：光线、场景、人物】。画面里只有【写：设计表的产品】，原片竞品所在的其他位置是空的。

【事件脚本】
阶段一：【写：景别和机位】。【写：产品在哪、手怎么动】。{scale}结束时【写：产品和人的终态】。

【保持一致】
全程仅【写：1盒/1支】；{scale_keep}人物面容服装、道具、空间方向与光线同@视频1一致；{sound}画面不显示字幕。
"""


CARD_STUB = """# cards: 【写：这一镜里出现的人物卡 ID，逗号分隔（RUN/cards/ID.png，shot_regen.py cards build 出卡）；现有：{avail}】
# views: {views}
{voice_head}# 人物卡模式（用户 2026-11-23：第二次补生采用人物卡模式）：没有参考视频，模型只看人物卡、产品图和这段文字，场景全靠文字搭。
# 写法照 ai-drama-ad prompting.md（用户 2026-11-24："本质上都是在用人物卡和产品参考在塑造当前场景"）：
# - 看 {name}.strip.png（原片这一镜，左右已按生成片{flip}）和前后相邻的原片镜头：环境、固定物件、机位都照原片写，
#   补出来的镜头要接得上相邻原片镜头，不换场景、不换光；不挂场景图（用户 2026-11-23），环境只用文字。
# - 站位绕固定物件写（坐在靠窗那把椅子上），机位表写死背景；屏幕左右只在机位句里用“某物在镜头的左手边”表达。
# - 原片这一段有几镜就写几个阶段，不新加切刀；每个阶段都写身体在做什么，听者写闭着嘴的具体反应，表演幅度照原片。
# - 台词按情绪节拍拆开写，括号里写听得见的语速、停顿、重音、音量；台词 }} 后面只接动作。
# - 穿帮那一处直接写成正确的样子；人物卡的脸只取长相，不带白底。
【生成目标】
生成一个美式写实剧情镜头：【写：谁在什么地方做什么】。人物严格按人物卡，产品严格按产品图。表演是【写：照原片的幅度，如“日间肥皂剧的幅度：情绪写在脸上和手上”】。

【参考素材职责】
【写：每张人物卡一行，按 # cards 的顺序：@图片1是<谁>，用于他/她的脸、发型、胡子、体型和<服装>。】
【写：每张产品图一行，接着编号：@图片N用于……，不采用……（产品图写法同 carton-swap.md）】
{voice_line}人物卡的白色背景和排版都不采用。

【主体与关系】
环境：【写：时间和光，光从哪来、冷暖】；【写：3–6 个固定物件和它们的相对位置，照原片相邻镜头里看得见的】。【写：轴线锚点，人相对物件的朝向，如“坐在靠台面那把椅子上、背对台面时，大窗在左手边”】。
站位：【写：每个人相对固定物件的位置、坐着还是站着、朝向谁】。
机位 A：【写：架在哪、朝哪拍、拍谁，背景永远是什么，再写一句某个固定物件在镜头的哪只手边——照原片这一镜的机位】。【写：原片这一段有第二个机位才写“机位 B：……”，否则删掉这一句】
产品：画面里仅【写：1盒/1支】yesnap LymphLoop【写：产品】，开始时【写：在谁手里或放在哪、密封还是已开、哪一面朝镜头】；【写：台面或手边还有什么】。{voice_desc}

【事件脚本】
阶段一：机位 A，【写：景别和运镜】。【写：身体在做什么——说话的人这句话时身体怎么动，不说话的人闭着嘴的具体反应；产品在哪、手怎么动】。{scale}{speech}结束时【写：产品和人的终态，写清产品在谁手里、数量】。

【保持一致】
全程仅【写：1盒/1支】yesnap LymphLoop【写：产品】；{scale_keep}人物面容服装同人物卡；环境、光线、站位和机位同上，镜头之间空间方向稳定；产品、桌面和手与人物同一种画面质感；{sound}画面不显示字幕。
"""


def user_again_quote(g):
    """A goof the user asked to redo although its seconds used up MAX_GOOF_REDOS ("again": true): their own words are
    required (2026-11-23 film 1912538379721657 G06: "这段喝条包然后拿钥匙的这段…请用人物卡的方式重做一下")."""
    q = str(g.get('user_quote') or '').strip()
    if not q: raise SystemExit(f'{g.get("seg")}@{g.get("at")}: "again": true 要带 "user_quote"（用户要求再做的原话）')
    return q


def card_stub(run, name, info, views, base):
    """Prompt stub of a card-mode redo (CARD_RULE): the shot is described in full, nothing points at a video."""
    import cards, swap_design
    lines = info.get('lines_in_shot') or []
    talk = info.get('sound') == 'lip_sync'
    speech = ''.join(f"【写：说话前的触发动作】【写：说话人】台词（【写：语速、停顿、重音、音量】，用美式英语）：{{{l.split(': ', 1)[-1]}}}【写：说完手上或身体的动作】。"
                     for l in lines) if talk else ''
    if lines and not talk: speech = '【写：这一镜里没人开口，台词在画外：写“画外传来说话声，画面里的人闭着嘴”】'
    return CARD_STUB.format(avail=', '.join(cards.available(run)) or '还没有，先写 RUN/cards/cast.json 再 cards build', views=views or '【写：按设计表的产品图，纸盒 carton_views,carton_hand】',
                            voice_head=(f'# voice: 【写：说话人自己的一段声音，{base} 里的秒数 A-B（2–15 秒，只有这个人在说话），作音色参考】\n' if talk else ''),
                            name=name, flip='镜像过' if info.get('mirrored') else '，未镜像',
                            voice_line='@音频1只用于【写：说话人】的音色、语速和语气，台词以下面写出的为准。\n' if talk else '',
                            voice_desc=('\n音色——【写：说话人：①人种与口音 ②性别和年龄档 ③音高 ④质感 ⑤语速与节奏 ⑥习惯 ⑦一句“不是播音腔”这类不是什么】，同@音频1。'
                                        if talk else '画面里的人都不说话，自然闭口。'),
                            speech=speech, scale=swap_design.SCALE_HOLE, scale_keep=swap_design.SCALE_KEEP,
                            sound='台词只有上面写出的这些，不说话的人闭着嘴做自己的反应（自然闭口），只有写明台词的人张嘴说话；【写：环境声，原片有背景音乐写“轻微背景音乐”，没有写“无背景音乐”】；' if talk
                                  else '只有轻微的环境声；')


def source_cut(run, a):
    """A reference cut straight from the working master by source seconds, for a goof shot that runs across two planned
    segments (2026-11-20 film 2210: the wide shot 431.6-440.73 was split between G06 and G07 inside the take, and the
    original bottle flashed back at the seam). Only for a stretch without captions (checked with OCR against the
    film's lines); scaled to the reference short edge like the segment references."""
    import swap, caption_clean
    run_, state = swap.load(str(run))
    master = Path(str(swap.working_master(state)))
    t0, t1 = (float(x) for x in a.source_s.split('-'))
    d = run/'shots-regen'; d.mkdir(exist_ok=True); out = d/f'{a.name}.src.mp4'
    info = media.probe(str(master)); w, h = info['video']['width'], info['video']['height']
    scale, _ = swap.reference_scale(w, h)       # an ffmpeg scale filter (None when already at the reference size)
    subprocess.run([ff(), '-v', 'error', '-y', '-ss', f'{t0:.3f}', '-i', str(master), '-t', f'{t1 - t0:.3f}',
                    '-vf', ','.join(x for x in (scale, 'fps=30') if x), *VIDEO_ARGS, '-c:a', 'aac', '-b:a', '256k', str(out)], check=True)
    plan = read(run/'plan/segments.json')
    spoken = [l['text'] for s in plan['segments'] for l in s.get('speech') or []]
    n_cap, n_all, texts = caption_clean.caption_frames(out, run/'qa'/'caption_scan', spoken)
    if n_cap:
        raise SystemExit(f'{t0}-{t1} s has captions in {n_cap} of {n_all} frames ({texts[:3]}): use --frames on a segment reference '
                         '(generate --refs-only cleans captions)')
    return out


def cmd_ref(a):
    run = Path(a.run).resolve()
    if getattr(a, 'source_s', None):
        src = source_cut(run, a)
        fps = rate_of(src); pieces = [[0, media.probe(str(src), include_frames=True)['frame_count']]]
    else:
        src = run/'clips'/f'{a.seg}.hq.clean.ref.mp4'
        if not src.is_file(): raise SystemExit(f'{src} missing: run swap.py generate --refs-only for {a.seg} first')
        meta = src.with_suffix('.json')
        if not meta.is_file():      # the .json is written after the clip is complete (11-19 G12: read half-written, 'moov atom not found')
            raise SystemExit(f'{src.name} is still being built (no {meta.name} yet); wait for generate --refs-only to finish')
        boxes = read(meta).get('erase') or []
        # caption-sized boxes are --caption-box (leftover caption text, 2026-11-22), not an erased object: allowed
        # (2896957363997251 G04 pink "IT", 1585905389415123 G06 WEEK 3 title were refused here)
        import swap
        if any(float(b[3]) > swap.CAPTION_BOX_MAX_H for b in boxes):
            raise SystemExit(f'{src.name} is an erased reference (erasing is off since 11-19); rebuild it with generate --refs-only')
        if not a.frames: raise SystemExit('--frames A-B (or --source-s A-B)')
        fps = rate_of(src); pieces = [parse_range(x) for x in a.frames]
    n = media.probe(str(src), include_frames=True)['frame_count']
    if any(b > n for _, b in pieces): raise SystemExit(f'frames past the end of {src.name} (0-{n - 1})')
    plan = plan_ref(pieces, fps, a.seconds, real_speed=getattr(a, 'real_speed', False))
    keep = {int(x) for x in (a.keep_sound or '0').split(',') if x.strip()}
    mutes = [tuple(float(v) for v in w.split('-', 1)) for spec in (a.mute or []) for w in spec.split(',')]
    if mutes and plan['mode'] == 'slow': raise SystemExit('--mute: a slowed reference is silent already')
    graph, amap = ref_graph(pieces, fps, plan, keep, mutes, hflip=getattr(a, 'hflip', False))
    d = run/'shots-regen'; d.mkdir(exist_ok=True); out = d/f'{a.name}.ref.mp4'
    cmd = [ff(), '-v', 'error', '-y', '-i', str(src)]
    if plan['mode'] == 'slow': cmd += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
    cmd += ['-filter_complex', graph, '-map', '[v]', '-map', amap, '-t', f"{plan['seconds']:g}", '-r', f'{fps:g}', *VIDEO_ARGS,
            '-c:a', 'aac', '-b:a', '256k', str(out)]
    subprocess.run(cmd, check=True)
    real_frames = round(plan['real_s'] * plan['factor'] * fps)
    picks = sorted({0, max(0, real_frames - 1), real_frames + 3, *range(0, real_frames, max(1, real_frames // 12))})
    sel = '+'.join(f'eq(n\\,{k})' for k in picks)
    subprocess.run([ff(), '-v', 'error', '-y', '-i', str(out), '-vf', f"select='{sel}',scale=144:-1,tile={len(picks)}x1",
                    '-frames:v', '1', str(d/f'{a.name}.strip.png')], check=True)
    write(d/f'{a.name}.json', {'segment': a.seg, 'name': a.name, 'pieces': pieces, 'ref_fps': fps, **plan,
                               **({'source_s': a.source_s} if getattr(a, 'source_s', None) else {}),
                               'keep_sound': sorted(keep), 'mutes': mutes, 'source': str(src), 'ref': str(out), 'ref_sha256': sha(out),
                               'created_at': time.strftime('%Y-%m-%dT%H:%M:%S')})
    stub = d/f'{a.name}.txt'
    if not stub.exists():
        import swap_design
        slowed = plan['mode'] == 'slow'
        stub.write_text(PROMPT_STUB.format(
            name=a.name, slow='# @视频1 是这个镜头放慢后的样子（无声），回剪时用段落自己的声音。\n' if slowed else '',
            speech_goal='' if slowed else '与【写：语言】台词', mouth='' if slowed else '和说话口型节奏',
            scale=swap_design.SCALE_HOLE, scale_keep=swap_design.SCALE_KEEP, scale_video=swap_design.SCALE_VIDEO,
            sound='声音只有轻微的室内环境声；' if slowed else '听者自然闭口聆听；台词只有写出的这些；【写：原片有背景音乐写“背景音乐和环境声同@视频1”，没有写“无bgm”】；'))
    print(json.dumps({'ref': str(out), 'strip': str(d/f'{a.name}.strip.png'), 'prompt': str(stub), 'mode': plan['mode'],
                      'real_s': round(plan['real_s'], 3), 'factor': round(plan['factor'], 3), 'seconds': plan['seconds']}, ensure_ascii=False))


def check_prompt(body, model=None):
    """(errors, warnings) for a shot prompt, with the same checks `generate --prepare-only` applies."""
    import swap
    errors, warns = [], []
    if '【写' in body: errors.append('还有没填的【写…】')
    errors += [f'正文里写了画幅/时长/分辨率/时间戳（这些是接口参数，用户 2026-11-20）：{x}' for x in swap.param_sentences(body)]
    if re.search(r'照它|(动作|节奏|手势|握法|拿法)[^。，；]{0,8}照@视频1', body):
        errors.append('不写“动作照@视频1 / 照它”（用户 2026-11-19：会照搬原视频的动作和手里竞品的样子），拿产品的手做什么逐拍写')
    neg = swap.negative_sentences(body)
    if neg: errors.append('只写正向描述，改掉：' + ' / '.join(neg))
    spoons, risks = swap.copy_risks(body)
    if spoons: errors.append('勺子或读音说明：' + ' / '.join(spoons))
    warns += [f'照抄风险（{row}）：{adv}  “{s}”' for row, adv, s in risks]
    return errors, warns


def abs_windows(run, sr):
    """[[start, end]] in source seconds of a shot redo (its meta or a job's shot_regen block): what it re-made of the film,
    comparable across segment references and --source-s cuts."""
    if sr.get('abs_s'): return [list(map(float, w)) for w in sr['abs_s']]
    if sr.get('source_s'): return [[float(x) for x in str(sr['source_s']).split('-')]]
    plan = read(Path(run)/'plan/segments.json'); fps = float(plan.get('fps') or 30)
    seg = next((s for s in plan['segments'] if s['id'] == sr.get('segment')), None)
    if not seg: return []
    t0 = seg['in_frame'] / fps; rf = float(sr.get('ref_fps') or fps)
    return [[t0 + a / rf, t0 + b / rf] for a, b in sr.get('pieces') or []]


def span_s(windows): return sum(b - a for a, b in windows)


def earlier_regen(run, meta):
    """[(job id, source windows)] of shot redos with a downloaded result that re-made part of the same source seconds
    (any segment: a redo may run across two, 2210 G06/G07)."""
    run = Path(run); mine = abs_windows(run, meta); out = []
    for p in sorted((run/'jobs').glob('G*-*.json')):
        sr = read(p).get('shot_regen') or {}
        if not sr or p.stem == f"{meta['segment']}-{meta['name']}": continue
        led = run/'generation'/'jobs'/p.name
        if not led.is_file() or not any(c.get('status') == 'downloaded' for c in (read(led).get('candidates') or {}).values()): continue
        theirs = abs_windows(run, sr)
        if any(a < d and c < b for a, b in theirs for c, d in mine): out.append((p.stem, theirs))
    return out


def redo_problem(run, meta):
    """None when this redo may be paid for under GOOF_RULE, else why not: at most MAX_GOOF_REDOS redos of the same
    source seconds, and a later redo strictly narrower than every redo it overlaps (down to one action)."""
    prior = earlier_regen(run, meta)
    if not prior: return None
    names = ', '.join(j for j, _ in prior)
    if len(prior) >= MAX_GOOF_REDOS:
        return f'这几秒已经补做过 {len(prior)} 次（{names}），同一处最多补做 {MAX_GOOF_REDOS} 次：写进交付说明报告用户，由用户决定'
    mine = span_s(abs_windows(run, meta)); narrow = min(span_s(w) for _, w in prior)
    if len(prior) >= 2:
        # the third redo (user 2026-11-21) may be as long as the narrowest before it: a goof the second redo did not
        # reach (film 1753 G17: a half-hidden bottle on a side table inside the same 1.4 s shot) needs that whole shot
        if mine > narrow + 0.05:
            return f'第三次补做不能比之前最窄的一次（{narrow:.1f} 秒）更长，现在是 {mine:.1f} 秒'
        return None
    if mine > narrow - NARROWER_BY + 0.02:          # a frame of slack: 0.4 -> 0.2 s came out 0.2000001 (2026-11-22)
        return (f'这几秒补做过（{names}，{narrow:.1f} 秒）；再补做要缩小范围（单镜头里的单个动作，用 locate --from-s/--to-s），'
                f'现在是 {mine:.1f} 秒，至少要短到 {narrow - NARROWER_BY:.1f} 秒')
    return None


def card_scene_problems(body, talk, ids, people):
    """Scene-building checks of a card-mode prompt (user 2026-11-24, after ai-drama-ad prompting.md: "本质上都是在用人物卡和
    产品参考在塑造当前场景"): with no reference video the room, the camera and the voice exist only in the text, so the
    parts that keep them still -- a camera table with a fixed background, an axis anchor around fixed objects, the
    stable-direction line, one picture quality for product and people, the voice sentence -- must be there."""
    out = []
    scene = [x for x in ids if x not in people]
    if scene: out.append(f'# cards 只写 cast.json 里的人物（用户 2026-11-23：人物卡模式不用场景图，环境只用文字写），去掉 {scene}')
    if not re.search(r'机位\s*A', body): out.append('【主体与关系】写机位表：机位 A：架在哪、朝哪拍、拍谁、背景永远是什么（ai-drama-ad prompting.md 第 5 节）')
    if '手边' not in body: out.append('写轴线锚点或机位句里“某固定物件在镜头的左/右手边”，锁住空间方向')
    if '空间方向稳定' not in body: out.append('【保持一致】写“镜头之间空间方向稳定”')
    if '画面质感' not in body: out.append('【保持一致】写“产品、桌面和手与人物同一种画面质感”')
    if talk and '音色——' not in body: out.append('说话镜头在【主体与关系】末尾写音色句：音色——说话人：人种与口音、性别年龄、音高、质感、语速节奏、习惯、不是什么')
    return out


def prepare_cards(a, run, meta, raw):
    """Job for a card-mode redo (CARD_RULE): character cards + product images (+ the speaker's voice clip), no video."""
    import swap, swap_design, cards
    import apimart_wan as seedance
    d = run/'shots-regen'
    head = {l[2:].split(':', 1)[0].strip(): l.split(':', 1)[1].strip() for l in raw.splitlines() if l.startswith('# ') and ':' in l}
    ids = [x.strip() for x in head.get('cards', '').split(',') if x.strip()]
    if not ids or '【写' in head.get('cards', ''): raise SystemExit(f'{a.name}: 第一行写 # cards: 人物卡 ID（RUN/cards/ID.png）')
    missing = [x for x in ids if not cards.card_png(run, x).is_file()]
    if missing: raise SystemExit(f'{a.name}: 没有这些人物卡 {missing}：写 RUN/cards/cast.json，shot_regen.py cards build --run RUN')
    views = [v.strip() for v in head.get('views', '').split(',') if v.strip() and v.strip() != 'none']
    if not swap_design.carton_views_ok(views, raw):
        raise SystemExit(f'{a.name}: 纸盒两张图一起给，顺序 carton_views,carton_hand（carton-swap.md）')
    body, _ = swap.ceil_timestamps(swap.clean_prompt(raw))
    errors, warns = check_prompt(body)
    errors += swap_design.five_section_problems(body, speech=bool(swap.spoken_lines(body)), n_images=len(ids) + len(views), video=False)
    errors += card_scene_problems(body, talk=meta.get('sound') == 'lip_sync', ids=ids, people=set(cards.cast(run)))
    voice = head.get('voice')
    if meta.get('sound') == 'lip_sync' and not voice:
        errors.append('这一镜看得到有人说话：写 # voice: A-B（说话人在选用片里的一段声音，作音色参考），并在【参考素材职责】写 @音频1 一行')
    if voice and '@音频1' not in body: errors.append('给了 # voice，【参考素材职责】要写“@音频1只用于<谁>的音色……”')
    if not voice and '@音频' in body: errors.append('提示词提到 @音频1，但没有 # voice: A-B')
    if '# design:' in raw and (run/'plan/swap_design.json').is_file():
        errors += swap_design.check_against_design(raw, read(run/'plan/swap_design.json')['shots'])
    for w in warns: print(f'{a.name}: {w}', file=sys.stderr)
    if errors: raise SystemExit(f'{a.name}: ' + '\n- '.join([''] + errors))
    a.user_quote = (f'用户原话：{a.user_quote}；' if a.user_quote else '') + GOOF_RULE + '；' + CARD_RULE
    if not (meta.get('goof') or {}).get('kind'):
        if getattr(a, 'why', None): meta['goof'] = {'kind': goof_kind(a.why), 'note': (getattr(a, 'note', None) or '').strip()}; write(d/f'{a.name}.json', meta)
        else: raise SystemExit(f'{a.name}: 穿帮补做要登记原因（round open 会自动登记）')
    problem = redo_problem(run, meta)
    if problem and not a.again:
        raise SystemExit(f'{a.name}: {problem}（{GOOF_RULE}）。用户明确要求再做时加 --again，并把用户原话放进 --user-quote')
    job_id = f"{meta['segment']}-{a.name}"
    if (run/'generation'/'jobs'/f'{job_id}.json').is_file():
        raise SystemExit(f'{job_id} was already generated; a new try is a new --name (e.g. {a.name}b)')
    profile = read(swap.PRODUCT)
    refs = [{'kind': 'image', 'path': str(cards.card_png(run, x)), 'sha256': sha(cards.card_png(run, x)), 'card': x} for x in ids]
    for v in views:
        p = swap.view_path(run, profile, v); refs.append({'kind': 'image', 'path': str(p), 'sha256': sha(p), 'view': v})
    if voice:
        v0, v1 = (float(x) for x in voice.split('-', 1))
        if not seedance.AUDIO_MIN_S <= v1 - v0 <= seedance.AUDIO_MAX_S:
            raise SystemExit(f'{a.name}: # voice {voice}: {v1 - v0:.1f} 秒，要 {seedance.AUDIO_MIN_S:g}–{seedance.AUDIO_MAX_S:g} 秒')
        src = swap.splice_source(run, meta['base']); mp3 = d/f'{a.name}.voice.mp3'
        subprocess.run([ff(), '-v', 'error', '-y', '-ss', f'{v0:.3f}', '-i', str(src), '-t', f'{v1 - v0:.3f}', '-vn', '-ac', '1', '-ar', '44100',
                        '-b:a', '192k', str(mp3)], check=True)
        refs.append({'kind': 'audio', 'path': str(mp3), 'sha256': sha(mp3), 'from': f"{meta['base']}:{voice}"})
    final = d/f'{a.name}.final.txt'; final.write_text(body)
    import agent_native
    channel = getattr(a, 'channel', None) or agent_native.gen_channel()
    if channel == 'x100':
        model = getattr(a, 'model', None) or X100_DEFAULT_MODEL
        if model not in X100_SETTINGS: raise SystemExit(f'--model: one of {", ".join(X100_SETTINGS)}')
        xs = X100_SETTINGS[model]
        job = {'job_id': job_id, 'run_dir': str(run), 'model': model, 'output_seconds': int(meta['seconds']),
               'generation_mode': seedance.MODE_CARDS, 'resolution': xs['resolution'], 'aspect_ratio': xs['aspect_ratio'], 'candidates': 1,
               'x100': {'model': model, **xs},
               'prompt_file': str(final), 'prompt_sha256': sha(final), 'references': refs,
               'shot_regen': {'quote': a.user_quote, 'segment': meta['segment'], 'shot': a.name, 'mode': 'cards', 'sound': meta.get('sound'),
                              'pieces': meta['pieces'], 'ref_fps': meta['ref_fps'], 'seconds': meta['seconds'], 'real_s': meta['real_s'],
                              'abs_s': abs_windows(run, meta), 'cards': ids, **({'goof': meta['goof']} if meta.get('goof') else {})}}
        nxt = f"x100_gen.py run-batch {run/'jobs'/f'{job_id}.json'}"
    else:
        job = {'job_id': job_id, 'run_dir': str(run), 'model': seedance.MODEL, 'output_seconds': int(meta['seconds']),
               'generation_mode': seedance.MODE_CARDS, 'resolution': seedance.RESOLUTION, 'aspect_ratio': seedance.ASPECT_RATIO, 'candidates': 1,
               'prompt_file': str(final), 'prompt_sha256': sha(final), 'references': refs,
               'shot_regen': {'quote': a.user_quote, 'segment': meta['segment'], 'shot': a.name, 'mode': 'cards', 'sound': meta.get('sound'),
                              'pieces': meta['pieces'], 'ref_fps': meta['ref_fps'], 'seconds': meta['seconds'], 'real_s': meta['real_s'],
                              'abs_s': abs_windows(run, meta), 'cards': ids, **({'goof': meta['goof']} if meta.get('goof') else {})}}
        seedance.validate(job)
        nxt = f"apimart_wan.py run-batch {run/'jobs'/f'{job_id}.json'}"
    (run/'jobs').mkdir(exist_ok=True); write(run/'jobs'/f'{job_id}.json', job)
    print(json.dumps({'job': job_id, 'mode': 'cards', 'channel': channel, 'model': job['model'], 'references': [Path(r['path']).name for r in refs],
                      'next': nxt}, ensure_ascii=False))


def cmd_prepare(a):
    import swap
    run = Path(a.run).resolve(); d = run/'shots-regen'
    meta = read(d/f'{a.name}.json'); raw = (d/f'{a.name}.txt').read_text()
    if meta.get('mode') == 'cards': return prepare_cards(a, run, meta, raw)
    first = raw.splitlines()[0] if raw else ''
    if not first.startswith('# views:'): raise SystemExit(f'{a.name}.txt: first line is "# views: …"')
    views = [v.strip() for v in first.split(':', 1)[1].split(',') if v.strip() and v.strip() != 'none']
    body, _ = swap.ceil_timestamps(swap.clean_prompt(raw))
    import apimart_wan as seedance
    import swap_design
    if not swap_design.carton_views_ok(views, raw):
        raise SystemExit(f'{a.name}: 纸盒两张图一起给，顺序 carton_views,carton_hand（用户 2026-11-20，carton-swap.md）；'
                         '构图和图2一样的纸盒手持特写才可只给 carton_views，并在提示词里写 # carton-close-up')
    if '【参考素材职责】' not in body:
        raise SystemExit(f'{a.name}: 按五段式写（生成目标/参考素材职责/主体与关系/事件脚本/保持一致；references/rules-now.md）')
    import agent_native
    channel = getattr(a, 'channel', None) or agent_native.gen_channel()
    if channel == 'x100':
        model = a.model or X100_DEFAULT_MODEL
        if model not in X100_SETTINGS: raise SystemExit(f'--model: one of {", ".join(X100_SETTINGS)}')
        limit = X100_SETTINGS[model]['max_seconds']
    else:
        model = seedance.MODEL; limit = seedance.OUTPUT_MAX
    if meta['seconds'] > limit: raise SystemExit(f'{a.name}: {meta["seconds"]} 秒超过 {model} 的上限 {limit} 秒')
    errors, warns = check_prompt(body, model)
    if '【参考素材职责】' in body:      # five-section (user 2026-11-20): same lint as the main flow, stage count left free here
        import swap_design
        errors += swap_design.five_section_problems(body, speech=bool(swap.spoken_lines(body)), n_images=len(views))
    design_file = run/'plan/swap_design.json'
    if '# design:' in raw:
        if not design_file.is_file(): raise SystemExit(f'{a.name}: 提示词引用了设计表，但 {design_file} 不存在（先 swap_design.py observe + decide）')
        import swap_design
        errors += swap_design.check_against_design(raw, read(design_file)['shots'])
    for w in warns: print(f'{a.name}: {w}', file=sys.stderr)
    if errors: raise SystemExit(f'{a.name}: ' + '\n- '.join([''] + errors))
    if sha(meta['ref']) != meta['ref_sha256']: raise SystemExit(f'{a.name}: reference changed since `ref`; run ref again')
    job_id = f"{meta['segment']}-{a.name}"
    if a.goof:
        # a goof redo is one shot (user 2026-11-20; Codex 30542865505302802 G14R1314 joined a close-up and a medium shot
        # to keep a line whole and the medium shot kept the competitor bottle)
        if len(meta['pieces']) != 1: raise SystemExit(f'{a.name}: 穿帮补做只做一个镜头，不要用多个 --frames 接镜头（声音按 SOUND_RULE 处理）')
        inner = [c for c in cut_times(meta['ref']) if 0.15 < c < float(meta['real_s']) * float(meta.get('factor') or 1) - 0.15]
        # the agent looked at the frames: one continuous shot whose screen or light changes fast enough to look like a cut
        # to the scene score (2026-11-22 film 1409783017240926 G03_webhero: a push-in on a laptop page, 0.24 s)
        if inner and (meta.get('goof') or {}).get('no_cut_reviewed'): inner = []
        if inner: raise SystemExit(f'{a.name}: 参考片里有硬切（{", ".join(f"{c:.2f}" for c in inner)} 秒）：穿帮补做只做一个镜头，按 locate 给的帧号重做 ref')
        a.user_quote = a.user_quote or GOOF_RULE
        # why this shot is redone, in the checklist's words (2026-11-22: 145 redos in three batches and no record of
        # which kind of goof each one fixed, so no rule could be corrected from them)
        if getattr(a, 'why', None):
            prev = meta.get('goof') or {}
            meta['goof'] = {'kind': goof_kind(a.why), 'note': (getattr(a, 'note', None) or '').strip(), **{k: prev[k] for k in ('round', 'no_cut_reviewed') if k in prev}}
            write(d/f'{a.name}.json', meta)
        if not (meta.get('goof') or {}).get('kind'):
            raise SystemExit(f'{a.name}: 穿帮补做要登记原因：--why ' + '|'.join(GOOF_KINDS) + ' --note "看到了什么"（round open 会自动登记）')
    if not a.user_quote: raise SystemExit('--user-quote "用户原话"（穿帮补做用 --goof，自动记录规则原文）')
    problem = redo_problem(run, meta)
    if problem and not a.again:
        raise SystemExit(f'{a.name}: {problem}（{GOOF_RULE}）。用户明确要求再做时加 --again，并把用户原话放进 --user-quote')
    if (run/'generation'/'jobs'/f'{job_id}.json').is_file():
        raise SystemExit(f'{job_id} was already generated; a new try is a new --name (e.g. {a.name}b)')
    profile = read(swap.PRODUCT)
    final = d/f'{a.name}.final.txt'; final.write_text(body)
    refs = [{'kind': 'video', 'path': meta['ref'], 'sha256': meta['ref_sha256'], 'duration_seconds': meta['seconds']}]
    for v in views:
        p = swap.view_path(run, profile, v); refs.append({'kind': 'image', 'path': str(p), 'sha256': sha(p), 'view': v})
    if len(refs) > 4: raise SystemExit(f'{a.name}: at most 3 images with the video (11-19 G07 was refused with 4)')
    job = {'job_id': job_id, 'run_dir': str(run), 'model': model, 'output_seconds': int(meta['seconds']),
           **({'x100': {'model': model, **X100_SETTINGS[model]}} if channel == 'x100' else
              {'generation_mode': seedance.MODE, 'resolution': seedance.RESOLUTION, 'aspect_ratio': seedance.ASPECT_RATIO, 'candidates': 1}),
           'prompt_file': str(final), 'prompt_sha256': sha(final), 'references': refs,
           'shot_regen': {'quote': a.user_quote, 'segment': meta['segment'], 'shot': a.name, 'mode': meta['mode'],
                          'pieces': meta['pieces'], 'ref_fps': meta['ref_fps'], 'seconds': meta['seconds'], 'real_s': meta['real_s'],
                          'abs_s': abs_windows(run, meta), **({'goof': meta['goof']} if meta.get('goof') else {})}}
    if channel != 'x100': seedance.validate(job)
    (run/'jobs').mkdir(exist_ok=True); write(run/'jobs'/f'{job_id}.json', job)
    nxt = (f'x100_gen.py quote --run {run} {job_id}' if channel == 'x100' else f"apimart_wan.py run-batch {run/'jobs'/f'{job_id}.json'}")
    print(json.dumps({'job': job_id, 'model': model, 'references': [Path(r['path']).name for r in refs], 'next': nxt},
                     ensure_ascii=False))


def splice_pieces(base_name, base_frames, base_fps, shots):
    """--picture specs for swap.py splice. `shots`: [{name, mode, pieces, ref_fps, seconds, real_s, gen_fps, gen_frames}]
    (pieces in reference frames). Each shot range lands on the base frames of the same moment and is fitted to them."""
    spans = []
    for s in shots:
        k = base_fps / s['ref_fps']; t = 0.0
        for a, b in s['pieces']:
            b0, b1 = round(a * k), round(b * k)
            if s['mode'] == 'slow': g0, g1 = 0, min(s['gen_frames'], round(s['seconds'] * s['gen_fps'])) - 1
            else:
                g0 = round(t * s['gen_fps']); t += (b - a) / s['ref_fps']; g1 = min(s['gen_frames'], round(t * s['gen_fps'])) - 1
            spans.append([b0, b1, s, (a, b), g0, g1])
    spans.sort(key=lambda x: x[0])
    for x, y in zip(spans, spans[1:]):
        if y[0] < x[1]: raise SystemExit('two shots cover the same frames')
        if y[0] - x[1] <= GAP_FILL: x[1] = y[0]      # 1-2 old frames between two new shots would flash (11-19 G05 S167/S168)
    out, cur, done = [], 0, []
    for b0, b1, s, ref, g0, g1 in spans:
        if b0 > cur: out.append(f'{base_name}:{cur}-{b0 - 1}')
        spec = f"{s['name']}:{g0}-{g1}@{b1 - b0}"; out.append(spec); cur = b1
        done.append((b0, b1, spec, s, ref))
    if cur < base_frames: out.append(f'{base_name}:{cur}-{base_frames - 1}')
    return out, done


def voice_end(path, max_s, noise_db=-35, gap_s=0.25):
    """Seconds where the last sound above `noise_db` ends in the first `max_s` of a clip (its own speech), else 0."""
    r = subprocess.run([ff(), '-hide_banner', '-t', f'{max_s:.3f}', '-i', str(path), '-vn', '-af', f'silencedetect=noise={noise_db}dB:d={gap_s}',
                        '-f', 'null', '-'], capture_output=True, text=True)
    starts = [float(x) for x in re.findall(r'silence_start: ([0-9.]+)', r.stderr)]
    ends = [float(x) for x in re.findall(r'silence_end: ([0-9.]+)', r.stderr)]
    if not starts and not ends: return float(max_s)                      # sound all the way
    if len(starts) > len(ends): return starts[-1]                        # trailing silence to the end of the window
    return float(max_s)


def card_quiet(s):
    """A card-mode redo with no speaking mouth on screen: it keeps the segment's sound (SOUND_RULE)."""
    return s.get('mode') == 'cards' and s.get('sound') != 'lip_sync'


def into_pieces(base, base_frames, shots, intos):
    """--picture specs when each regenerated shot replaces exact base frames (from `locate`): `intos` is one "A-B" for a
    single shot, or "NAME=A-B" per shot (a round puts every redone shot of a segment back in one splice). Frames are
    inclusive. Returns (pictures, spans) like splice_pieces."""
    by = {}
    for spec in intos:
        name, eq, rng = spec.rpartition('=')
        if not eq:
            if len(shots) != 1: raise SystemExit('--into A-B takes one shot; with several shots give --into NAME=A-B for each')
            name = shots[0]['short']
        b0, b1 = (int(x) for x in rng.split('-'))
        if not 0 <= b0 <= b1 < base_frames: raise SystemExit(f'--into {spec}: base has frames 0-{base_frames - 1}')
        by[name] = (b0, b1)
    missing = [s['short'] for s in shots if s['short'] not in by]
    if missing or len(by) != len(shots): raise SystemExit(f'--into: one range per shot (missing {missing}, given {sorted(by)})')
    order = sorted(shots, key=lambda s: by[s['short']][0])
    pics, spans, cur = [], [], 0
    for s in order:
        b0, b1 = by[s['short']]
        if b0 < cur: raise SystemExit(f"--into: {s['short']} overlaps the shot before it")
        if b0 > cur: pics.append(f'{base}:{cur}-{b0 - 1}')
        if s['mode'] == 'slow':
            # a slowed, silent shot fills exactly the frames it replaces (it was made from that window, slowed)
            g1 = min(s['gen_frames'], round(s['seconds'] * s['gen_fps'])) - 1
            spec = f"{s['name']}:0-{g1}@{b1 - b0 + 1}"
        elif card_quiet(s):
            # a card-mode shot in which no mouth speaks: its first real_s seconds at real speed over the same frames, with
            # the segment's sound under it (declared --no-mouth), like a slowed shot
            g1 = min(s['gen_frames'], round(float(s['real_s']) * s['gen_fps'])) - 1
            spec = f"{s['name']}:0-{g1}@{b1 - b0 + 1}"
        else:
            # a real-speed shot goes in at its own length, picture and voice together, and what follows moves (user:
            # 原速顺剪). It runs to the end of what is said in it (`use_s`), never shorter than the source shot: 2026-11-22
            # film 1603578481269031 G01 cut the brand line's tail, G05 squeezed 1.7 s of new picture into 1.29 s.
            use = max(float(s['real_s']), float(s.get('use_s') or 0))
            g1 = min(s['gen_frames'], round(use * s['gen_fps'])) - 1
            spec = f"{s['name']}:0-{g1}"
        pics.append(spec); spans.append((b0, b1 + 1, spec, s, None)); cur = b1 + 1
    if cur < base_frames: pics.append(f'{base}:{cur}-{base_frames - 1}')
    return pics, spans


# why a shot is redone: the eight goofs of references/qa-checklist.md (review looks for these and nothing else)
GOOF_KINDS = {
    'competitor': '竞品还在（容器、剂型、标签、品牌字，含背景里的和接口闪回）',
    'count': '数量不对，或中途凭空出现、消失、变成别的东西',
    'shape': '产品形态明显错（纸盒像瓶子/名片/火柴盒，条包变硬棍、弯折）',
    'side': '展示镜头拿侧面或背面当正面',
    'physics': '物理上做不到（条包自己立着、画面里撕条包、服用时手里没有产品）',
    'identity': '人、脸、衣服、场景变了',
    'speech': '台词没说出来、说了竞品名或没有的成分、嘴和声音对不上',
    'text': '画面里有字幕、水印、竞品文字',
}


def goof_kind(value):
    v = str(value).strip().lower()
    keys = list(GOOF_KINDS)
    if v.isdigit() and 1 <= int(v) <= len(keys): return keys[int(v) - 1]
    if v in GOOF_KINDS: return v
    raise SystemExit(f'穿帮原因 {value!r}：用 ' + '、'.join(f'{i + 1}={k}' for i, k in enumerate(keys)) + '（qa-checklist.md 的八类）')


GOOF_RULE = ('规则（用户 2026-11-20）：首次生成的整段里出现穿帮，对照原片找出穿帮的那一镜，单独针对这一镜重写提示词生成，'
             '不整段重生成；补做后还有穿帮，继续缩小范围（缩到单镜头里的单个动作）再生成第三次；检查出来的还可以补第三次，同一处最多补做三次（用户 2026-11-21）')
CARD_RULE = ('人物卡模式（用户 2026-11-23）：第一次生成采用原片参考的模式，如果第一次出现穿帮了，第二次补生就要采用人物卡模式'
             '——补做不挂参考视频，只挂人物卡 + 产品图（说话镜头再挂说话人的一段声音作音色参考），场景、机位、站位、动作、台词全写进提示词')
MAX_GOOF_REDOS = 3        # user 2026-11-20: a shot redo, then a narrower redo (a single action); 2026-11-21: "后续只要skill进程中检查出来了，都可以执行第三次补做" -- four generations in all
NARROWER_BY = 0.2         # seconds: a second redo must be at least this much shorter than the redo it follows
CUT_SCENE = 0.12          # ffmpeg scene score for a hard cut (2210 G01: 0.53 / 0.61 at the real cuts, < 0.06 elsewhere)


def cut_times(video, threshold=CUT_SCENE):
    """Hard-cut times (seconds) of a clip, by ffmpeg's scene score."""
    r = subprocess.run([ff(), '-hide_banner', '-i', str(video), '-vf', f"select='gt(scene,{threshold})',showinfo", '-an', '-f', 'null', '-'],
                       capture_output=True, text=True)
    return [float(m) for m in re.findall(r'pts_time:([0-9.]+)', r.stderr)]


def gray_frame(video, t, size=(48, 86)):
    import numpy as np
    r = subprocess.run([ff(), '-v', 'error', '-ss', f'{max(0.0, t):.3f}', '-i', str(video), '-frames:v', '1', '-vf',
                        f'scale={size[0]}:{size[1]},format=gray', '-f', 'rawvideo', '-'], capture_output=True, check=True)
    a = np.frombuffer(r.stdout, np.uint8).astype(float)
    return a.reshape(size[1], size[0]) if a.size == size[0] * size[1] else None


def mirrored(gen, src, times):
    """True when the generated clip matches the horizontally flipped source better (2210 G09/G10 came back mirrored)."""
    import numpy as np
    votes = []
    for t in times:
        g, s = gray_frame(gen, t), gray_frame(src, t)
        if g is None or s is None: continue
        def corr(x, y):
            x = x - x.mean(); y = y - y.mean(); d = (np.sqrt((x * x).sum() * (y * y).sum()) or 1.0)
            return float((x * y).sum() / d)
        votes.append(corr(g, s[:, ::-1]) - corr(g, s))
    return bool(votes) and sum(votes) / len(votes) > 0.15


def shot_span(cuts, t, end):
    """[start, end) of the shot containing t, from cut times."""
    b = [0.0] + sorted(c for c in cuts if 0 < c < end) + [end]
    for x, y in zip(b, b[1:]):
        if x <= t < y: return x, y
    return b[-2], b[-1]


SOUND_RULE = ('声音（用户 2026-11-20，2026-11-21 统一）：画面里有人说话（看得到嘴）-> 这一镜连口型带声音重新生成、原速'
              '（不足 4 秒末帧定格补到 4 秒），拼回时用它自己的声音，不论台词改没改，不再用别处的声音对口型；'
              '画面里没人说话 -> 可以放慢无声生成，台词没改用原片这一段的声音，改过用首次生成已改好的声音')


def sound_plan(lines, rewrites, speaking_on_screen):
    """What a redone shot sounds like (SOUND_RULE). lines: the lines heard in the shot; rewrites: changed words in them;
    speaking_on_screen: someone in the picture talks. Returns 'none' | 'source' | 'first_generation' | 'lip_sync'."""
    if not lines: return 'none'
    if speaking_on_screen: return 'lip_sync'
    return 'first_generation' if rewrites else 'source'


def shot_speaking(run, seg, ss, se):
    """True when a shot of the segment overlapping [ss, se] has someone speaking on screen (design observation), or a
    line in it that is not narration."""
    run = Path(run); hit = [sid for sid, a, b in seg.get('shot_times') or [] if a < se and b > ss]
    for sid in hit:
        f = run/'design/obs'/f'{sid}.json'
        if f.is_file() and read(f).get('speaking'): return True
    return any(str(l.get('kind', '')).lower() not in ('voice_over', 'vo', 'narration') and l.get('kind')
               for l in seg.get('speech') or [] if float(l['end']) > ss and float(l['start']) < se)


def line_has_rewrite(text, rw):
    """True when a spoken line holds a word this rewrite replaces. The plan's original can span two lines ("It's called
    Lymphoria. Half a dropper," against the lines "It's called Lymfuria." / "Half a dropper, morning and night,") and
    the transcript spells the name its own way: the whole-sentence match missed both, the redo was given the source
    sound and the old brand line came back under a rewritten voice-over (2026-11-22 film 1429516565935964 G07)."""
    import difflib
    tok = lambda t: [w for w in re.findall(r"[a-z0-9']+", str(t or '').lower())]
    changed = {w for w in tok(rw.get('original')) if len(w) >= 3} - set(tok(rw.get('rewrite')))
    if not changed: return (rw.get('original') or '').lower() in str(text).lower()
    words = tok(text)
    return any(w == c or (len(c) >= 5 and difflib.SequenceMatcher(None, w, c).ratio() >= 0.7) for w in words for c in changed)   # a miss gives back the old line; a wrong hit only keeps the generated voice


def cmd_locate(a):
    print(json.dumps(locate(a), ensure_ascii=False, indent=1))


def locate(a, cuts=None):
    """Where a goof sits: the generated shot around --at, the same moments in the source reference, mirror check, the
    lines spoken there, and the ref / splice commands to redo only that shot (the rule in GOOF_RULE). `cuts`: a dict
    shared between calls so each clip's hard cuts are measured once (a round locates many goofs of one segment)."""
    run = Path(a.run).resolve()
    import swap
    cuts = {} if cuts is None else cuts
    def cut_times_of(path):
        k = str(path)
        if k not in cuts: cuts[k] = cut_times(path)
        return cuts[k]
    plan_ = read(run/'plan/segments.json')
    src_run = next((x['reused_from'] for x in plan_.get('reused_segments') or [] if x['id'] == a.seg), None)
    if src_run:
        raise SystemExit(f"{a.seg} 是共用段（来自 {src_run['film']} {src_run['segment']}）：穿帮回那条代表片的 RUN 修（round），修完在本 RUN 重跑 swap.py reuse")
    selection_file = run/'plan/selections.json'
    sel = (read(selection_file).get(a.seg) or {}) if selection_file.is_file() else {}
    base = a.base or (f"{sel['job']}-{sel.get('candidate') or 'c1'}" if sel.get('job') else None)
    if not base: raise SystemExit(f'{a.seg}: nothing selected; pass --base G##-aN-c1')
    gen = swap.splice_source(run, base); src = run/'clips'/f'{a.seg}.hq.clean.ref.mp4'
    gi = media.probe(str(gen), include_frames=True); si = media.probe(str(src), include_frames=True)
    gfps = float(Fraction(str(gi['fps']))); sfps = float(Fraction(str(si['fps'])))
    seg = next(s for s in plan_['segments'] if s['id'] == a.seg)
    seg_end = float(sel.get('clip_out') or seg['duration_s'])
    t0, t1 = (float(x) for x in (a.at.split('-') if '-' in a.at else (a.at, a.at)))
    g_cuts = cut_times_of(gen); s_cuts = cut_times_of(src)
    gs, ge = shot_span(g_cuts, t0, min(seg_end, gi['video_duration']))
    if t1 > t0: ge = max(ge, shot_span(g_cuts, t1, min(seg_end, gi['video_duration']))[1])
    if a.from_s is not None:     # a long single take: start at a sentence pause before the goof (2210 G10: 3.3 s)
        if not gs <= a.from_s < ge: raise SystemExit(f'--from-s {a.from_s}: outside the goof shot {gs:.2f}-{ge:.2f}')
        gs = a.from_s
    if a.to_s is not None:       # a second redo narrowed to one action: end right after it
        if not gs < a.to_s <= ge: raise SystemExit(f'--to-s {a.to_s}: outside {gs:.2f}-{ge:.2f}')
        ge = a.to_s
    # the generation follows the source's timing (2210: G01 / G10 within a few frames); snap to a source cut nearby
    snap = lambda t, cuts: min([c for c in cuts if abs(c - t) <= a.snap] or [t], key=lambda c: abs(c - t))
    ss = gs if a.from_s is not None else snap(gs, s_cuts)
    se = ge if a.to_s is not None else min(snap(ge, s_cuts), float(seg['duration_s']))
    # the generation dropped or added a cut, so its shots no longer sit at the source's times (2026-11-22 film
    # 1659373095741717 G01: one source cut missing, the generated detective close-up at 5.79-8.04 s is the source's
    # S025 at 3.97-5.83 s, not S026 at the same seconds). The agent compares the strips and gives the source window
    inside = lambda cuts, end: [c for c in cuts if 0.05 < c < end - 0.05]
    g_in, s_in = inside(g_cuts, min(seg_end, float(seg['duration_s']))), inside(s_cuts, min(seg_end, float(seg['duration_s'])))
    lone_g = [c for c in g_in if not any(abs(c - x) <= a.snap for x in s_in)]
    lone_s = [c for c in s_in if not any(abs(c - x) <= a.snap for x in g_in)]
    source_given = getattr(a, 'source_window', None)
    if source_given:
        ss, se = (float(x) for x in str(source_given).split('-'))
        if not 0 <= ss < se <= float(seg['duration_s']) + 0.05:
            raise SystemExit(f'source_s {source_given}: outside the source reference 0-{seg["duration_s"]:.2f}')
        se = min(se, float(seg['duration_s']))
    timing_warning = None
    if not source_given and (lone_g or lone_s):
        timing_warning = (f'生成片和原片的切点对不上（只在生成片：{[round(c, 2) for c in lone_g]}；只在原片：{[round(c, 2) for c in lone_s]}）：'
                          f'镜头可能已不在原片同一秒，source_s 可能对错镜头。'
                          f'看 strip 首末帧和原片帧板，对错就在 goofs 里给 "source_s": "起-止"（原片参考里这一镜的秒数）再 round open')
    mid = [gs + (ge - gs) * q for q in (0.25, 0.5, 0.75)]
    flip = mirrored(gen, src, mid)
    lines = [l for l in seg.get('speech') or [] if float(l['end']) > ss and float(l['start']) < se]
    rewrites = [r for r in (seg['rewrites']['dialogue'] + seg['rewrites']['vo'])
                if any(line_has_rewrite(l.get('text') or '', r) for l in lines)]
    real = se - ss
    rf0, rf1 = round(ss * sfps), round(se * sfps); bf0, bf1 = round(gs * gfps), round(ge * gfps) - 1
    sound = sound_plan(lines, rewrites, shot_speaking(run, seg, ss, se))
    short = real < TARGET_S
    out = {'segment': a.seg, 'base': base, 'generated_shot_s': [round(gs, 3), round(ge, 3)], 'base_frames': [bf0, bf1],
           'source_s': [round(ss, 3), round(se, 3)], 'ref_frames': [rf0, rf1], 'mirrored': flip,
           'mode': ('real speed, own sound, last frame held to 4 s' if short and sound == 'lip_sync' else
                    'slow (under 4 s: silent)' if short else 'real speed with its own sound'),
           'sound': sound, 'sound_rule': SOUND_RULE,
           'lines_in_shot': [f"{l['start']:.2f}-{l['end']:.2f} {l.get('speaker', '')}: {l['text']}" for l in lines],
           'rewrites_in_shot': [(r['original'], r['rewrite']) for r in rewrites],
           'generated_cuts': [round(c, 3) for c in g_cuts], 'source_cuts': [round(c, 3) for c in s_cuts],
           'next': [f"shot_regen.py ref --run {run} --seg {a.seg} --name NAME --frames {rf0}-{rf1}" + (' --hflip' if flip else '')
                    + (' --real-speed' if short and sound == 'lip_sync' else '')
                    + (' --mute A-B（改词的旧词，按片段内秒数）' if rewrites and sound == 'lip_sync' else ''),
                    '写 shots-regen/NAME.txt（五段式；只写这一镜，针对穿帮原因改写），shot_regen.py prepare --user-quote "<GOOF_RULE 或用户原话>"',
                    f"shot_regen.py splice --run {run} --seg {a.seg} --out-attempt aN NAME --base {base} --into {bf0}-{bf1}"
                    + {'lip_sync': '', 'source': ' --source-sound NAME', 'first_generation': '', 'none': ''}[sound],
                    f"swap.py select --job {a.seg}-aN --candidate c1 --clip-out {seg_end:g}"],
           'rule': GOOF_RULE, 'seg_end': seg_end, 'real_s': real, 'short': short,
           **({'source_given': True} if source_given else {}), **({'timing_warning': timing_warning} if timing_warning else {}),
           'shots': [sid for sid, x, y in seg.get('shot_times') or [] if min(y, se) - max(x, ss) > 0.1]}
    return out


def sound_root(run, base, b0, b1):
    """The first generation of the segment when `base` is a local splice that keeps its frame positions up to and over
    frames b0-b1 and those frames were a fitted redo (their sound borrowed): a shot put back again takes the first
    generation's sound there, not the base's borrowed one (2026-11-22 film 1429516565935964 G07: G07-a2 carried the
    source's "It's called Lymphoria" under two redone shots; the rewritten voice-over is G07-a1's). None otherwise."""
    led = Path(run)/'generation'/'jobs'/f"{base.rsplit('-c', 1)[0]}.json"
    if not led.is_file() or read(led).get('provider') != 'local_splice': return None
    pos, root = 0, None
    for p in (read(led).get('recipe') or {}).get('pictures') or []:
        a, b = (int(x) for x in p['frames_inclusive']); src = str(p.get('source') or '')
        n_out = int(p.get('fit') or p.get('output_frames') or p.get('fit_frames') or (b - a + 1))
        if REAL_RESULT.match(src):
            if a != pos: return None                         # this piece moved: timings no longer match the first generation
            if root and src != root: return None
            root = src
        pos += n_out
    return root if root and pos > b1 else None


def ripple_splice(a, run, base, base_fps, bi, pics, spans):
    """swap.py splice for --into: every piece brings its own sound (base pieces theirs, real-speed shots their own speech),
    a slowed shot takes the base window's sound (or the source's with --source-sound) at the place it lands now, and
    is declared --no-mouth there. Returns {'delta_s': output length minus base length, 'output_frames': ...}."""
    import swap, math
    cmd = [sys.executable, str(HERE/'swap.py'), 'splice', '--run', str(run), '--segment', a.seg, '--out-attempt', a.out_attempt,
           '--note', a.note or ('单镜头重生成拼回：' + ', '.join(a.shots))]
    if a.force: cmd.append('--force')
    for p in pics: cmd += ['--picture', p]
    by_spec = {spec: (b0, b1, s) for b0, b1, spec, s, _ in spans}
    pos = 0; extra = []
    master = None
    last_shot_b1 = None; last_shot_end_frame = None
    for p in pics:
        name, _, rng = p.rpartition(':'); rng, _, fit = rng.partition('@')
        f0, f1 = (int(x) for x in rng.split('-'))
        if p in by_spec:
            b0, b1, s = by_spec[p]
            if fit:
                n_out = int(fit); t0, t1 = pos / base_fps, (pos + n_out) / base_fps
                if getattr(a, 'source_sound', None) and s['short'] in a.source_sound:
                    if master is None: master = Path(str(swap.working_master(swap.load(str(run))[1])))
                    plan = read(run/'plan/segments.json'); fps = float(plan.get('fps') or 30)
                    seg = next(x for x in plan['segments'] if x['id'] == a.seg); t_seg = seg['in_frame'] / fps
                    clip_in = float((read(run/'plan/selections.json').get(a.seg) or {}).get('clip_in') or 0)
                    extra += ['--audio-window', f'{master}:{t_seg + clip_in + b0 / base_fps:.3f}-{t_seg + clip_in + b1 / base_fps:.3f}@{t0:.3f}']
                else:
                    src = sound_root(run, base, b0, b1) or base
                    extra += ['--audio-window', f'{src}:{b0 / base_fps:.3f}-{b1 / base_fps:.3f}@{t0:.3f}']
                extra += ['--no-mouth', f'{t0:.3f}-{t1:.3f}']
            else:
                n_out = math.ceil((f1 - f0 + 1) / s['gen_fps'] * base_fps)
            last_shot_b1 = b1
            last_shot_end_frame = pos + n_out
        else:
            n_out = f1 - f0 + 1
        pos += n_out
    cmd += extra
    print(' '.join(cmd[2:]))
    if not a.dry_run: subprocess.run(cmd, check=True)
    return {'delta_s': (pos - bi['frame_count']) / base_fps, 'output_frames': pos,
            'base_fps': base_fps, 'last_shot_b1': last_shot_b1, 'last_shot_end_frame': last_shot_end_frame}


def cmd_splice(a):
    run = Path(a.run).resolve()
    selection_file = run/'plan/selections.json'
    sel = (read(selection_file).get(a.seg) or {}) if selection_file.is_file() else {}
    base = a.base or (f"{sel['job']}-{sel.get('candidate') or 'c1'}" if sel.get('job') and sel.get('candidate') else None)
    if not base: raise SystemExit(f'{a.seg}: no selected generated clip to splice into; pass --base G##-aN-c1')
    import swap
    base_path = swap.splice_source(run, base); bi = media.probe(str(base_path), include_frames=True)
    base_fps = float(Fraction(str(bi['fps'])))
    shots = []
    for name in a.shots:
        job = read(run/'jobs'/f'{a.seg}-{name}.json'); sr = job['shot_regen']
        gen = run/'generation'/'results'/f'{a.seg}-{name}-c1.mp4'
        if not gen.is_file(): raise SystemExit(f'{gen} missing: generation not downloaded yet (apimart_wan.py run-batch / x100_gen.py poll)')
        gi = media.probe(str(gen), include_frames=True)
        shots.append({**sr, 'name': f'{a.seg}-{name}-c1', 'short': name, 'gen_fps': float(Fraction(str(gi['fps']))),
                      'gen_frames': gi['frame_count']})
    if a.into:
        # exact base frames (the goof shot as cut in the generated clip, from `locate`): the generation's own timing can
        # differ from the source's by a few frames, so the time mapping of splice_pieces would leave goof frames behind
        for sh in shots:
            if sh['mode'] != 'slow' and not card_quiet(sh):
                gen = run/'generation'/'results'/f"{sh['name']}.mp4"
                sh['use_s'] = min(float(sh['seconds']), voice_end(gen, float(sh['seconds'])) + 0.15)
                # the redo's reference was held on its last frame up to 4 s and the generation copies that still picture;
                # a loudness end (music under the voice) ran into it: 2026-11-22 film 2114 G03 had 1.3 s of still picture
                # in the middle of the segment. Stop where the picture goes still unless the voice is still talking there.
                cut, rec = swap.trim_frozen_tail(gen, 0.0, sh['use_s'], float(sh['real_s']))
                if rec:
                    sh['use_s'] = cut; sh['frozen_tail'] = rec
                    if rec.get('talking'): print(f"{sh['short']}: 补做镜头在 {rec['still_from']:.2f} 秒后画面定住、声音还在说（{rec['talking']}）："
                                                 f"下一轮按 speech 再补这一镜", file=sys.stderr)
        pics, spans = into_pieces(base, bi['frame_count'], shots, a.into if isinstance(a.into, list) else [a.into])
        return ripple_splice(a, run, base, base_fps, bi, pics, spans)
    else:
        if any(sh['mode'] == 'cards' for sh in shots): raise SystemExit('card-mode shots go back with --into (round splice does this)')
        pics, spans = splice_pieces(base, bi['frame_count'], base_fps, shots)
    cmd = [sys.executable, str(HERE/'swap.py'), 'splice', '--run', str(run), '--segment', a.seg, '--out-attempt', a.out_attempt,
           '--audio', base, '--note', a.note or ('单镜头重生成拼回：' + ', '.join(a.shots))]
    if a.force: cmd.append('--force')
    for p in pics: cmd += ['--picture', p]
    if getattr(a, 'source_sound', None):
        # SOUND_RULE case 1: the lines here are unchanged -> the original film's sound of the same moments
        master = Path(str(swap.working_master(swap.load(str(run))[1])))
        plan = read(run/'plan/segments.json'); fps = float(plan.get('fps') or 30)
        seg = next(x for x in plan['segments'] if x['id'] == a.seg); t_seg = seg['in_frame'] / fps
        clip_in = float((read(run/'plan/selections.json').get(a.seg) or {}).get('clip_in') or 0)
        for b0, b1, spec, s, _ in spans:
            if s['short'] not in a.source_sound: continue
            if s['mode'] != 'slow': raise SystemExit(f"{s['short']}: a real-speed shot keeps its own generated sound (user 2026-11-21); --source-sound is only for a slowed shot with no speaking mouth")
            t0 = t_seg + clip_in + b0 / base_fps; t1 = t_seg + clip_in + b1 / base_fps
            cmd += ['--audio-window', f'{master}:{t0:.3f}-{t1:.3f}@{b0 / base_fps:.3f}']
    for b0, b1, spec, s, _ref in spans:
        if s['mode'] == 'slow':
            # a slowed, silent shot: locate only slows a shot in which no mouth speaks, so the segment's (or the source's)
            # sound may run under it
            cmd += ['--no-mouth', f'{b0 / base_fps:.3f}-{b1 / base_fps:.3f}']
        else:
            # a real-speed shot always keeps its own generated sound, lips and voice together (user 2026-11-21: "带有口型同步的
            # 替换段，不论有没有改口播，都应该要用生成段自带的音频"); --shot-sound is no longer needed
            g0, g1 = (int(x) for x in spec.rsplit(':', 1)[1].split('@')[0].split('-'))
            cmd += ['--audio-window', f"{s['name']}:{g0 / s['gen_fps']:.3f}-{(g1 + 1) / s['gen_fps']:.3f}@{b0 / base_fps:.3f}"]
    print(' '.join(cmd[2:]))
    if not a.dry_run: subprocess.run(cmd, check=True)


# ---------------------------------------------------------------- rounds: every goof of a film redone in one batch
# User 2026-11-22 ("补穿帮一轮一批"). Three batches of 26 films: 145 single-shot redos for 146 segments; the films that
# prepared their redos together finished them in 12-24 minutes, the ones that did them one after another (find one,
# redo, wait 7 minutes, look, find the next) took 3-6 hours for the same number. A round is: list every goof of the
# film from the review -> `round open` locates each one and cuts its reference -> write the prompts -> `round submit`
# checks all of them and sends them together (one wait) -> look at every result -> `round splice` puts each segment's
# redone shots back in one splice and selects it. At most MAX_GOOF_REDOS rounds can touch the same seconds.
STAGE_LABELS = '一二三四五六七八九十'


def rounds_dir(run): return Path(run)/'shots-regen'/'rounds'
def round_file(run, n): return rounds_dir(run)/f'round-{n}.json'


def round_numbers(run):
    return sorted(int(m.group(1)) for f in rounds_dir(run).glob('round-*.json') if (m := re.fullmatch(r'round-(\d+)', f.stem)))


def load_round(run, n=None):
    nums = round_numbers(run)
    if not nums: raise SystemExit('no round yet: shot_regen.py round open --run RUN --file goofs.json')
    n = int(n) if n else nums[-1]
    if n not in nums: raise SystemExit(f'round {n} does not exist (have {nums})')
    return read(round_file(run, n))


def stage_blocks(raw, shot_ids_in_order=None):
    """[(shot id or None, stage text)] of a prompt file's 【事件脚本】. The shot of a stage comes from the file's
    "# 阶段二 = 镜头2 S175 …" / "# 阶段二 = S175（…）" comments (anywhere in the file); without them, from
    `shot_ids_in_order` when there is exactly one stage per shot."""
    by_label = {}
    for line in raw.splitlines():
        m = re.match(r'#\s*阶段([一二三四五六七八九十\d]+)\s*=\s*(?:镜头\d+\s+)?(S\d+\w*)', line.strip())
        if m: by_label[m.group(1)] = m.group(2)
    out, cur, inside = [], None, False
    for line in raw.splitlines():
        t = line.strip()
        if re.match(r'【(生成目标|参考素材职责|主体与关系|事件脚本|保持一致)】', t):
            inside = t.startswith('【事件脚本】')
            if cur: out.append(tuple(cur)); cur = None
            continue
        if not inside or t.startswith('#'): continue
        m = re.match(r'阶段([一二三四五六七八九十\d]+)\s*[：:]', t)
        if m:
            if cur: out.append(tuple(cur))
            cur = [by_label.get(m.group(1)), t]
        elif cur and t: cur[1] += '\n' + t
    if cur: out.append(tuple(cur))
    if shot_ids_in_order and len(out) == len(shot_ids_in_order) and not all(sid for sid, _ in out):
        out = [(sid, text) for sid, (_, text) in zip(shot_ids_in_order, out)]
    return out


def stub_from_segment_prompt(raw, shot_ids, views=None, seg_shots=None):
    """The segment's own written prompt cut down to the stage(s) of `shot_ids`: a redo is the same scene, people, voices
    and product, one shot of it, so the agent only rewrites what went wrong. None when the stages cannot be matched to
    shots, or when this shot needs other reference images than the segment had (`views` from the design table)."""
    lines = raw.splitlines()
    head_views = next((l for l in lines if l.startswith('# views:')), None)
    if not head_views: return None
    seg_views = [v.strip() for v in head_views.split(':', 1)[1].split(',') if v.strip()]
    if views is not None and list(views) != seg_views: return None
    blocks = stage_blocks(raw, seg_shots)
    keep = [text for sid, text in blocks if sid in shot_ids]
    if not keep or len(keep) != len(shot_ids): return None
    out, skipping = [head_views], False
    design = next((l for l in lines if l.startswith('# design:')), None)
    if design:
        kept = [x for x in shot_ids if x in [y.strip() for y in design.split(':', 1)[1].split(',')]]
        if kept: out.append('# design: ' + ','.join(kept))
    for line in lines:
        t = line.strip()
        if line.startswith('#'): continue
        if t.startswith('【事件脚本】'):
            out.append(line)
            for i, text in enumerate(keep):
                text = re.sub(r'^阶段[一二三四五六七八九十\d]+\s*[：:]\s*', '', text)
                if i == 0: text = re.sub(r'^切至', '', text)
                out.append(f'阶段{STAGE_LABELS[i]}：{text}')
            out.append(''); skipping = True; continue
        if skipping:
            if re.match(r'【(生成目标|参考素材职责|主体与关系|保持一致)】', t): skipping = False
            else: continue
        out.append(line)
    return '\n'.join(out).rstrip() + '\n'


OUTPUT_MAX_S = 15


def design_views(run, ids):
    """Product images the design table gives these shots ('carton_views,carton_hand', ...), or None."""
    f = Path(run)/'plan/swap_design.json'
    if not f.is_file() or not ids: return None
    import swap_design
    design = read(f)['shots']; rows = [design[x] for x in ids if x in design and design[x].get('form')]
    return ','.join(swap_design.views_of(rows)) if rows else None


def round_stub(run, seg_id, name, info, goof):
    """Write shots-regen/NAME.txt for a round item unless it is there already. Returns which stub was used."""
    run = Path(run); d = run/'shots-regen'; stub = d/f'{name}.txt'
    if stub.exists(): return 'kept'
    import swap_design
    design_file = run/'plan/swap_design.json'
    design = read(design_file)['shots'] if design_file.is_file() else {}
    ids = [x for x in info.get('shots') or []]
    views = None
    rows = [design[x] for x in ids if x in design and design[x].get('form')]
    if rows: views = swap_design.views_of(rows)
    plan = read(run/'plan/segments.json')['segments'] if (run/'plan/segments.json').is_file() else []
    seg_shots = [x[0] for x in (next((g for g in plan if g['id'] == seg_id), {}).get('shot_times') or [])]
    sel = (read(run/'plan/selections.json').get(seg_id) or {}) if (run/'plan/selections.json').is_file() else {}
    cands = [run/'prompts'/f'{seg_id}-a1.txt'] + sorted((run/'prompts').glob(f'{seg_id}-a*.txt'))
    text, how = None, None
    for pf in cands:
        if pf.is_file() and not pf.name.endswith('.final.txt') and ids:
            text = stub_from_segment_prompt(pf.read_text(), ids, views, seg_shots)
            if text: how = f'segment prompt {pf.name}'; break
    if not text and ids and all(x in design for x in ids):
        text = swap_design.skeleton(design, ids); how = 'design skeleton'
    if not text: return None                       # cmd_ref writes the blank five-section stub
    note = [f"# 穿帮（{goof['kind']}）：{GOOF_KINDS[goof['kind']]}。看到的：{goof['note']}",
            '# 只写这一镜，针对上面这处穿帮改写；【生成目标】【主体与关系】若是整段的原文，改成只说这一镜。']
    note += [f'# 这一镜的台词：{l}' for l in info.get('lines_in_shot') or []]
    note += [f'# 改词：{a} -> {b}' for a, b in info.get('rewrites_in_shot') or []]
    if info.get('mode', '').startswith('slow'): note.append('# @视频1 是这个镜头放慢后的样子（无声），回剪时用段落自己的声音；这一镜不写台词。')
    head, _, rest = text.partition('\n')
    stub.write_text(head + '\n' + '\n'.join(note) + '\n' + rest, encoding='utf-8')
    return how


def parse_goof(spec):
    """G05@3.2[-4.0]:kind:note -> a goof entry."""
    m = re.fullmatch(r'(G\d+)@([\d.]+(?:-[\d.]+)?):([^:]+):(.+)', spec.strip(), re.S)
    if not m: raise SystemExit(f'--goof G##@秒[-秒]:原因:看到了什么，got {spec!r}')
    return {'seg': m[1], 'at': m[2], 'why': m[3], 'note': m[4]}


def cmd_round_open(a):
    import contextlib, io
    run = Path(a.run).resolve(); rounds_dir(run).mkdir(parents=True, exist_ok=True)
    goofs = list(read(a.file)) if a.file else []
    goofs += [parse_goof(x) for x in a.goof or []]
    if not goofs: raise SystemExit('round open: give the goofs (--file goofs.json, or --goof G##@秒:原因:说明 for each)')
    nums = round_numbers(run)
    n = int(a.round) if a.round else (nums[-1] if nums and read(round_file(run, nums[-1])).get('status') == 'open' else (nums[-1] + 1 if nums else 1))
    old = {it['name']: it for it in (read(round_file(run, n)).get('items') or [])} if round_file(run, n).is_file() else {}
    cuts, items, used = {}, [], set()
    for g in goofs:
        seg = g.get('seg') or g.get('segment')
        if not seg or g.get('at') in (None, ''): raise SystemExit(f'goof needs "seg" and "at" (seconds in the generated clip): {g}')
        kind = goof_kind(g.get('why') or g.get('kind') or ''); note = str(g.get('note') or '').strip()
        if not note: raise SystemExit(f'{seg}@{g["at"]}: 写 note（看到了什么，一句话）')
        la = argparse.Namespace(run=str(run), seg=seg, base=g.get('base'), at=str(g['at']), snap=float(g.get('snap') or 0.6),
                                from_s=g.get('from_s'), to_s=g.get('to_s'), source_window=g.get('source_s'))
        info = locate(la, cuts)
        if g.get('no_mouth'):
            # the agent looked at the strip: nobody's mouth is in this shot (a phone or laptop screen, a product insert,
            # the back of a head) although its line is dialogue (2026-11-22 film 1409783017240926 G03 web pages). It is
            # slowed and silent and keeps the segment's sound, as any shot without a speaking mouth
            lines = [x for x in info['lines_in_shot']]; rew = info['rewrites_in_shot']
            info['sound'] = sound_plan(lines, rew, False)
        if info.get('timing_warning') and info['sound'] == 'source':
            # Shifted cut in generated base: slicing master-clean.mp4 at base timestamps would drop or misalign speech.
            # Keep base's own aligned soundtrack (first_generation) instead.
            info['sound'] = 'first_generation'
            print(f"{seg}@{g['at']}: 切点与原片存在偏移，无嘴补做镜头改用 first_generation 音轨（保留底片同期声，防止丢句）", file=sys.stderr)
        name = g.get('name')
        if not name:
            stem = f"{(info.get('shots') or [seg])[0]}_{kind}_r{n}"; name = stem; k = 0
            while name in used or ((run/'generation'/'jobs'/f'{seg}-{name}.json').is_file() and name not in old):
                k += 1; name = stem + 'abcdefghij'[k - 1]
        if name in used: raise SystemExit(f'two goofs named {name}')
        used.add(name)
        item = {'name': name, 'seg': seg, 'kind': kind, 'note': note, 'at': str(g['at']), 'from_s': g.get('from_s'), 'to_s': g.get('to_s'),
                'mute': g.get('mute'), 'base': info['base'], 'base_frames': info['base_frames'], 'ref_frames': info['ref_frames'],
                **({'source_s': g['source_s']} if g.get('source_s') else {}), **({'timing_warning': info['timing_warning']} if info.get('timing_warning') else {}),
                'mirrored': info['mirrored'], 'sound': info['sound'], 'real_s': round(info['real_s'], 3), 'seg_end': info['seg_end'],
                'shots': info.get('shots') or [], 'lines': info['lines_in_shot'], 'rewrites': info['rewrites_in_shot'], 'state': 'open',
                **({'no_mouth': True} if g.get('no_mouth') else {})}
        if (run/'generation'/'jobs'/f'{seg}-{name}.json').is_file():           # already sent in an earlier call of this round
            item.update({k: v for k, v in old.get(name, {}).items() if k in ('state', 'strip', 'prompt', 'stub', 'mode', 'result', 'error', 'attempt')})
            if item.get('state') == 'spliced' and old.get(name, {}).get('sound') not in (None, item['sound']):
                # the sound plan changed after the splice (2026-11-22 film 1429516565935964 G07: a rewritten voice-over
                # was first given the source sound): spliced again with the right sound; untouched items stay spliced
                item['state'] = 'generated'; item['resplice'] = f"sound {old[name]['sound']} -> {item['sound']}"
                print(f"{name}: 声音方式变了（{item['resplice']}），round splice 会重拼这一处", file=sys.stderr)
            items.append(item); continue
        goof = {'kind': kind, 'note': note, 'round': n, **({'no_cut_reviewed': True} if g.get('no_cut') else {})}
        mode = str(g.get('mode') or getattr(a, 'mode', None) or 'cards')
        if mode not in ('cards', 'reference'): raise SystemExit(f'{seg}@{g["at"]}: mode 是 cards（默认，CARD_RULE）或 reference')
        if mode == 'cards':
            # CARD_RULE (user 2026-11-23): the redo is generated from character cards + product images, no reference video.
            # The source shot is still cut (real speed, mirrored like the generated clip) for the strip the agent writes
            # the prompt from and for the result sheet; it is never uploaded.
            stub = run/'shots-regen'/f'{name}.txt'; stub.parent.mkdir(exist_ok=True)
            # an unfilled stub is rewritten: a reopened item may have a new sound plan (no_mouth) or window
            if not stub.exists() or '【写' in stub.read_text():
                stub.write_text(card_stub(run, name, info, design_views(run, info.get('shots') or []), info['base']), encoding='utf-8')
            ra = argparse.Namespace(run=str(run), seg=seg, name=name, frames=[f"{info['ref_frames'][0]}-{info['ref_frames'][1]}"], source_s=None,
                                    seconds=TARGET_S, keep_sound=None, mute=None, hflip=info['mirrored'], real_speed=True)
            with contextlib.redirect_stdout(io.StringIO()): cmd_ref(ra)
            mp = run/'shots-regen'/f'{name}.json'; meta = read(mp)
            talk = info['sound'] == 'lip_sync'
            meta.update(goof=goof, into=info['base_frames'], base=info['base'], mode='cards', sound=info['sound'],
                        seconds=float(min(OUTPUT_MAX_S, max(TARGET_S, math.ceil(info['real_s'] - 1e-6) + (1 if talk else 0)))))
            if g.get('again'): meta['again_quote'] = user_again_quote(g)
            write(mp, meta)
            item.update(mode='cards', strip=str(run/'shots-regen'/f'{name}.strip.png'), prompt=str(stub), stub='card-mode stub')
            problem = redo_problem(run, meta)
            if problem and not g.get('again'): item['blocked'] = problem
            elif problem: item['again'] = True; item['over_limit'] = problem
            items.append(item); continue
        real_speed = bool(info['short'] and info['sound'] == 'lip_sync')
        item['stub'] = round_stub(run, seg, name, {**info, 'mode': 'real' if real_speed or not info['short'] else 'slow'}, goof)
        ra = argparse.Namespace(run=str(run), seg=seg, name=name, frames=[f"{info['ref_frames'][0]}-{info['ref_frames'][1]}"], source_s=None,
                                seconds=TARGET_S, keep_sound=None, mute=[g['mute']] if g.get('mute') else None, hflip=info['mirrored'],
                                real_speed=real_speed)
        with contextlib.redirect_stdout(io.StringIO()): cmd_ref(ra)
        mp = run/'shots-regen'/f'{name}.json'; meta = read(mp)
        meta.update(goof=goof, into=info['base_frames'], base=info['base']); write(mp, meta)
        item.update(mode=meta['mode'], strip=str(run/'shots-regen'/f'{name}.strip.png'), prompt=str(run/'shots-regen'/f'{name}.txt'),
                    stub=item['stub'] or 'blank five-section stub')
        if info['rewrites_in_shot'] and info['sound'] == 'lip_sync' and not g.get('mute'):
            item['todo'] = '这一镜有改词且看得到嘴：在 goofs 里给 "mute": "起-止"（参考片内旧词的秒数），再跑一次 round open'
        problem = redo_problem(run, meta)
        if problem: item['blocked'] = problem
        items.append(item)
    rec = {'type': 'lymphloop_goof_round', 'round': n, 'run': str(run), 'status': 'open', 'opened_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
           'rule': GOOF_RULE, 'items': items}
    write(round_file(run, n), rec)
    print(json.dumps({'round': n, 'file': str(round_file(run, n)),
                      'items': [{k: it.get(k) for k in ('name', 'seg', 'kind', 'note', 'mode', 'sound', 'real_s', 'mirrored', 'lines', 'rewrites', 'source_s', 'timing_warning', 'stub', 'strip', 'prompt', 'todo', 'blocked') if it.get(k) not in (None, [], '')} for it in items],
                      'next': ['人物卡模式（默认，CARD_RULE）：RUN/cards/cast.json 写好人物（全片最清楚的正脸），shot_regen.py cards build --run RUN，亲自看每张卡',
                               '看每张 strip（原片这一镜），把场景、机位、站位、动作、台词写进 prompt；# cards / # views / # voice 三行填好',
                               '写完每个 prompt（针对登记的穿帮原因改写；blocked 的不用写，写进交付说明）',
                               f'shot_regen.py round submit --run {run}   # 全部检查通过后一次提交，后台跑']}, ensure_ascii=False, indent=1))


def result_sheet(ref, gen, seconds, out, n=8):
    """Reference (top) and generated shot (bottom) at the same moments, for the look before the splice."""
    d = max(0.3, float(seconds))
    one = lambda i, lab: f'[{i}:v]trim=end={d:.3f},fps={n / d:.4f},scale=180:320,tile={n}x1[{lab}]'
    subprocess.run([ff(), '-v', 'error', '-y', '-i', str(ref), '-i', str(gen), '-filter_complex', f"{one(0, 'a')};{one(1, 'b')};[a][b]vstack[o]",
                    '-map', '[o]', '-frames:v', '1', str(out)], check=True)


def item_result(run, it):
    """(state, detail) of a round item from its ledger."""
    run = Path(run); led = run/'generation'/'jobs'/f"{it['seg']}-{it['name']}.json"
    gen = run/'generation'/'results'/f"{it['seg']}-{it['name']}-c1.mp4"
    if not led.is_file(): return None, None
    c = (read(led).get('candidates') or {}).get('c1') or {}
    if c.get('status') == 'downloaded' and gen.is_file(): return 'generated', str(gen)
    return ('failed', str(c.get('error') or c.get('status'))[:200]) if c.get('status') in ('failed', 'known_failed', 'outcome_unknown') else ('running', c.get('status'))


def cmd_round_submit(a):
    import contextlib, io
    run = Path(a.run).resolve(); rec = load_round(run, a.round); n = rec['round']
    only = set((a.only or '').split(',')) - {''}
    errors, jobs = {}, []
    for it in rec['items']:
        if only and it['name'] not in only: continue
        if it.get('blocked') or it.get('state') in ('spliced', 'dropped'): continue
        state, _ = item_result(run, it)
        if state == 'generated': it['state'] = 'generated'; continue
        job = run/'jobs'/f"{it['seg']}-{it['name']}.json"
        if state is None:                              # not sent yet: check the prompt and write the job
            import agent_native
            mq = (read(run/'shots-regen'/f"{it['name']}.json").get('again_quote') if (run/'shots-regen'/f"{it['name']}.json").is_file() else None)
            pa = argparse.Namespace(run=str(run), name=it['name'], user_quote=mq, goof=True, channel=agent_native.gen_channel(), model=None, again=bool(it.get('again') and mq),
                                    why=None, note=None)
            try:
                with contextlib.redirect_stdout(io.StringIO()): cmd_prepare(pa)
            except (SystemExit, Exception) as e:           # a prompt check, or the adapter refusing the job (size, length)
                errors[it['name']] = str(e); continue
            it['state'] = 'prepared'
        jobs.append(job)
    write(round_file(run, n), rec)
    if errors and not a.partial:
        raise SystemExit('round submit: 这些提示词没过检查，改完再提交（一轮只等一次；确实要先交其余的加 --partial）：\n'
                         + '\n'.join(f'== {k}\n{v}' for k, v in errors.items()))
    if not jobs:
        print(json.dumps({'round': n, 'submitted': 0, 'note': 'nothing to submit', 'errors': errors}, ensure_ascii=False, indent=1)); return
    if a.dry_run:
        print(json.dumps({'round': n, 'would_submit': [j.stem for j in jobs], 'errors': errors}, ensure_ascii=False, indent=1)); return
    rec['status'] = 'submitted'; rec['submitted_at'] = time.strftime('%Y-%m-%dT%H:%M:%S'); write(round_file(run, n), rec)
    import agent_native
    batch_script = 'x100_gen.py' if agent_native.gen_channel() == 'x100' else 'apimart_wan.py'
    rc = subprocess.run([sys.executable, str(HERE/batch_script), 'run-batch', *map(str, jobs)]).returncode
    out = []
    for it in rec['items']:
        state, detail = item_result(run, it)
        if state == 'generated' and it.get('state') not in ('spliced', 'dropped'):
            it['state'] = 'generated'; sheet = run/'shots-regen'/f"{it['name']}.result.png"
            try:
                meta = read(run/'shots-regen'/f"{it['name']}.json")
                result_sheet(meta['ref'], detail, float(meta['real_s']) * float(meta.get('factor') or 1), sheet); it['result'] = str(sheet)
            except Exception as e: it['result'] = f'sheet failed: {e}'[:120]
        elif state in ('failed', 'running'): it['state'] = state; it['error'] = detail
        if state: out.append({k: it.get(k) for k in ('name', 'seg', 'kind', 'state', 'result', 'error') if it.get(k)})
    write(round_file(run, n), rec)
    print(json.dumps({'round': n, 'batch_exit': rc, 'items': out, **({'not_submitted': errors} if errors else {}),
                      'next': ['逐张看 result.png（上排参考、下排生成）：登记的穿帮没了、没有新穿帮、接口两帧干净',
                               f'shot_regen.py round splice --run {run} [--drop 仍有穿帮的NAME ...]   # 每段一次拼回并选用',
                               '仍有穿帮的进下一轮：范围缩到单个动作（goofs 里给 from_s / to_s），同一处最多三次']}, ensure_ascii=False, indent=1))


def next_attempt(run, seg):
    run = Path(run); nums = [1]
    for d, pat in ((run/'generation'/'jobs', f'{seg}-a*.json'), (run/'jobs', f'{seg}-a*.json'), (run/'prompts', f'{seg}-a*.txt'),
                   (run/'generation'/'results', f'{seg}-a*-c1.mp4')):
        for f in d.glob(pat):
            m = re.match(rf'{seg}-a(\d+)', f.name)
            if m: nums.append(int(m.group(1)))
    return max(nums) + 1


def cmd_round_splice(a):
    run = Path(a.run).resolve(); rec = load_round(run, a.round); n = rec['round']
    flat_drop = []
    for item in (a.drop or []):
        if isinstance(item, (list, tuple)):
            for sub in item: flat_drop.extend(x.strip() for x in str(sub).split(',') if x.strip())
        else:
            flat_drop.extend(x.strip() for x in str(item).split(',') if x.strip())
    drop = set(flat_drop)
    unknown = drop - {it['name'] for it in rec['items']}
    if unknown: raise SystemExit(f'--drop: not in round {n}: {sorted(unknown)}')
    sel = read(run/'plan/selections.json'); by_seg = {}
    again = {x for v in (getattr(a, 'resplice', None) or []) for x in v.split(',') if x}
    unknown = again - {it['name'] for it in rec['items']}
    if unknown: raise SystemExit(f'--resplice: not in round {n}: {sorted(unknown)}')
    for it in rec['items']:
        if it['name'] in again and it.get('state') == 'spliced':
            # put back again on the segment's current selection: only the sound plan changed (2026-11-22 film
            # 1429516565935964 G07, a rewritten voice-over had been given the source sound); no new generation
            it['state'] = 'generated'; it['resplice'] = 'requested'
        if it['name'] in drop:
            if it.get('state') != 'spliced': it['state'] = 'dropped'
            continue
        if it.get('state') == 'generated': by_seg.setdefault(it['seg'], []).append(it)
    waiting = [it['name'] for it in rec['items'] if it.get('state') in ('open', 'prepared', 'running') and not it.get('blocked')]
    if waiting and not a.partial:
        raise SystemExit(f'round splice: 还没生成完：{waiting}（等 round submit 跑完；先拼已完成的加 --partial）')
    done, problems = [], {}
    for seg, its in sorted(by_seg.items()):
        s = sel.get(seg) or {}
        base_now = f"{s.get('job')}-{s.get('candidate') or 'c1'}"
        stale = [it['name'] for it in its if it['base'] != base_now]
        if stale:
            problems[seg] = f'选用片已从 {its[0]["base"]} 变成 {base_now}，这些镜头的拼回帧号作废：{stale}（对新选用片重新 round open）'; continue
        attempt = f'a{next_attempt(run, seg)}'
        sa = argparse.Namespace(run=str(run), seg=seg, out_attempt=attempt, shots=[it['name'] for it in its], base=base_now,
                                into=[f"{it['name']}={it['base_frames'][0]}-{it['base_frames'][1]}" for it in its],
                                source_sound=[it['name'] for it in its if it.get('mode') in ('slow', 'cards') and it.get('sound') == 'source'] or None,
                                shot_sound=None, note=f'第 {n} 轮穿帮补做拼回：' + '；'.join(f"{it['name']}（{it['kind']}：{it['note']}）" for it in its),
                                dry_run=a.dry_run, force=False)
        try: res = cmd_splice(sa) or {}
        except (SystemExit, subprocess.CalledProcessError) as e:
            problems[seg] = f'splice: {e}'[:400]; continue
        if a.dry_run: continue
        job = f'{seg}-{attempt}'
        if not a.no_select:
            seg_end = float(its[0]['seg_end'])
            clip_out = seg_end + float(res.get('delta_s') or 0)     # the redone shots' own length moves the rest (ripple)
            # Prevent 1-frame tail flash into the appended old base tail: when the last redone shot already covers
            # through seg_end (within 2 frames), cap clip_out strictly at the end of that redone shot.
            if res.get('base_fps') and res.get('last_shot_end_frame') is not None and res.get('last_shot_b1') is not None:
                bfps = float(res['base_fps'])
                if (int(res['last_shot_b1']) + 1) >= seg_end * bfps - 2.0:
                    tail_cap = int(res['last_shot_end_frame']) / bfps
                    if clip_out > tail_cap:
                        clip_out = tail_cap
            # rounded down: 14.41666 s formatted as 14.4167 lands past the candidate's last frame and select refuses it
            # (2026-11-23 film 1912538379721657 G06-a7, a redo that kept the segment length exactly)
            clip_out = math.floor(clip_out * 10000) / 10000
            cmd = [sys.executable, str(HERE/'swap.py'), 'select', '--run', str(run), '--job', job, '--candidate', 'c1', '--clip-out', f"{clip_out:.4f}",
                   '--note', f'第 {n} 轮穿帮补做：' + ', '.join(it['name'] for it in its)]
            if float(s.get('clip_in') or 0) > 0: cmd += ['--clip-in', f"{float(s['clip_in']):g}"]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                problems[seg] = f'select {job}: {(r.stderr or r.stdout).strip()[-300:]}'; continue
        for it in its: it['state'] = 'spliced'; it['attempt'] = job
        done.append({'segment': seg, 'job': job, 'shots': [it['name'] for it in its]})
    if not a.dry_run:
        if all(it.get('state') in ('spliced', 'dropped') or it.get('blocked') for it in rec['items']): rec['status'] = 'closed'
        write(round_file(run, n), rec)
    left = [{'name': it['name'], 'seg': it['seg'], 'state': it.get('state'), **({'blocked': it['blocked']} if it.get('blocked') else {})}
            for it in rec['items'] if it.get('state') != 'spliced']
    print(json.dumps({'round': n, 'spliced': done, **({'problems': problems} if problems else {}), **({'left': left} if left else {}),
                      'next': [f'finish.py --run {run} --from captions   # 只重做变了的段：补字幕、回剪、盖水印、终检',
                               'dropped 的进下一轮（范围缩到单个动作）；blocked 的写进交付说明']}, ensure_ascii=False, indent=1))
    if a.finish and done and not problems and not a.dry_run:
        subprocess.run([sys.executable, str(HERE/'finish.py'), '--run', str(run), '--from', 'captions'], check=True)


def cmd_round_status(a):
    run = Path(a.run).resolve(); out = []
    for n in round_numbers(run):
        rec = read(round_file(run, n))
        out.append({'round': n, 'status': rec.get('status'),
                    'items': [{k: it.get(k) for k in ('name', 'seg', 'kind', 'note', 'state', 'attempt', 'blocked', 'error') if it.get(k)} for it in rec['items']]})
    print(json.dumps(out, ensure_ascii=False, indent=1))


REAL_RESULT = re.compile(r'^G\d+-a\d+-c\d+$')


def still_ranges(run, job, talking):
    """[(first, last)] output frames of the local splice `job` that are a redo shot's still tail with nothing said: the
    redo reference was held on its last frame up to 4 s and the generation copied it; the round splice took the redo
    to its loudness end (music under it) and kept the still picture inside the segment (2026-11-22: 12 redo shots in 6
    delivered films, 0.25-1.4 s each; user: "生成段的最后都会被定格一下"). Splices made from splices are followed."""
    import swap
    run = Path(run); led = run/'generation'/'jobs'/f'{job}.json'
    if not led.is_file(): return []
    L = read(led)
    if L.get('provider') != 'local_splice': return []
    out, pos = [], 0
    for p in (L.get('recipe') or {}).get('pictures') or []:
        a, b = (int(x) for x in p['frames_inclusive']); n = b - a + 1
        n_out = int(p.get('fit') or p.get('output_frames') or p.get('fit_frames') or n)
        src = str(p.get('source') or ''); path = p.get('path')
        if n_out != n or p.get('fit') is not None:
            pos += n_out; continue                                   # a slowed shot fitted into its window: left alone
        inner_job = src.rsplit('-c', 1)[0]
        if REAL_RESULT.match(src) and (run/'generation'/'jobs'/f'{inner_job}.json').is_file() \
                and read(run/'generation'/'jobs'/f'{inner_job}.json').get('provider') == 'local_splice':
            for x, y in still_ranges(run, inner_job, talking):
                lo, hi = max(x, a), min(y, b)
                if lo <= hi: out.append((pos + lo - a, pos + hi - a))
        elif not REAL_RESULT.match(src) and path and Path(path).is_file():
            f = float(Fraction(str(media.probe(path)['fps'])))
            still = media.frozen_tail(path); end = (b + 1) / f
            meta = run/'shots-regen'/f"{src.split('-', 1)[-1].rsplit('-c', 1)[0]}.json"
            real_s = float(read(meta).get('real_s') or 0) if meta.is_file() else 0.0     # never shorter than the source shot
            if still is not None and end > max(still, real_s) + 0.1:
                cut, rec = swap.trim_frozen_tail(Path(path), a / f, end, real_s)
                first = max(a, math.ceil(cut * f))
                if rec and rec.get('talking'): talking.append(f"{src} {rec['still_from']:.2f}-{rec['voice_to']:.2f}s（{rec['talking']}）")
                if first <= b: out.append((pos + first - a, pos + n - 1))
        pos += n_out
    return out


def lockw(a):
    return ['--lock-wait', str(a.lock_wait)] if getattr(a, 'lock_wait', 0) else []


STILL_LEFT_MIN_S = 0.25         # a still stretch shorter than this is not cut (frozen_tail's own minimum)


def pending_still(run, seg_id, s, seg_s, talking):
    """What retrim would still cut from segment `seg_id`'s selection `s`, or None: {'ranges': [(first, last)]} for the
    still frames of redo shots inside a selected splice, {'tail': [cut_s, record]} for a still end past the segment's
    own length. Still frames over which someone is still talking are never counted (they stay, recorded as a defect).
    Used by `retrim` and by `swap.py recut`, which refuses a cut with still frames left (user 2026-11-23: "回剪的时候把定格
    帧裁掉")."""
    import swap
    if not s or s.get('picture') == 'source' or s.get('keep_frozen_tail') or not s.get('job'): return None
    run = Path(run); led = run/'generation'/'jobs'/f"{s['job']}.json"
    L = read(led) if led.is_file() else {}
    if L.get('provider') == 'local_splice':
        fps = float(Fraction(str((L.get('recipe') or {}).get('fps') or 24)))
        # under STILL_LEFT_MIN_S is not a freeze: the cut after the last word moves by a few frames between two
        # transcriptions of the same audio (2026-11-23 film 1409 G03 "Doc Root?" 2.46 s / "Jock route." 2.38 s left 2
        # frames for recut to refuse after retrim had found nothing; 998 G05 the same with 0.06 s at the end)
        ranges = [r for r in sorted(still_ranges(run, s['job'], talking)) if (r[1] - r[0] + 1) / fps >= STILL_LEFT_MIN_S]
        if ranges: return {'ranges': ranges}
    path = (s.get('media') or {}).get('path')
    if not path or not Path(path).is_file() or s.get('clip_out') is None: return None
    clip_out = float(s['clip_out'])
    cut, rec = swap.trim_frozen_tail(Path(path), float(s.get('clip_in') or 0), clip_out, float(seg_s or 0))
    if rec and rec.get('talking'): talking.append(f"{seg_id} 段尾 {rec['still_from']:.2f}-{rec['voice_to']:.2f}s（{rec['talking']}）")
    if rec and cut < clip_out - STILL_LEFT_MIN_S: return {'tail': [round(cut, 3), rec]}
    return None


def cmd_retrim(a):
    """Cut the still frames out of selected segments: the still frames of redo shots inside a selected splice (a native
    swap.py splice of that clip's own frames, picture and sound together, then select it) and a still end past the
    segment's own length (select it again with the same window; select cuts the still end). A segment with nothing to
    cut is left as it is. --all does every segment of the film; finish.py runs it first (user 2026-11-23: "回剪的时候把定格
    帧裁掉"; the 2114 G03 joins kept 1.4 s and 1.7 s of still picture). Segments reused from a donor film follow the
    donor: retrim the donor, then run swap.py reuse again."""
    import swap
    run = Path(a.run).resolve(); sels = read(run/'plan/selections.json')
    plan = {s['id']: s for s in swap.all_segments(read(run/'plan/segments.json'))}
    if not getattr(a, 'all', False) and not a.seg: raise SystemExit('retrim: --seg G## or --all')
    segs = sorted(g for g in sels if g in plan) if getattr(a, 'all', False) else [a.seg]
    changed = []
    for g in segs:
        if retrim_one(a, run, g, read(run/'plan/selections.json').get(g) or {}, float(plan.get(g, {}).get('duration_s') or 0)):
            changed.append(g)
    if getattr(a, 'all', False): print('RETRIM_CHANGED ' + json.dumps(changed))


def retrim_one(a, run, seg_id, sel, seg_s):
    """True when the segment's selection was (or, with --dry-run, would be) changed."""
    job, cand = sel.get('job'), sel.get('candidate') or 'c1'
    if not job and sel.get('picture') != 'source': raise SystemExit(f'{seg_id}: nothing selected')
    if sel.get('picture') == 'source': print(json.dumps({'segment': seg_id, 'skipped': 'original picture'})); return False
    led = run/'generation'/'jobs'/f'{job}.json'
    if led.is_file() and read(led).get('provider') == 'reused_generation':
        print(json.dumps({'segment': seg_id, 'skipped': 'reused from the donor film: retrim the donor, then swap.py reuse again'}, ensure_ascii=False))
        return False
    talking = []; left = pending_still(run, seg_id, sel, seg_s, talking)
    for t in talking: print(f'{seg_id}: 画面定住时还在说话：{t}；这一处按 speech 下一轮补', file=sys.stderr)
    if not left:
        print(json.dumps({'segment': seg_id, 'job': job, 'still': 'none to cut', 'talking': talking}, ensure_ascii=False))
        return False
    if 'tail' in left:
        # a still end past the segment's length: select it again with the same window; select cuts the still end
        cmd = [sys.executable, str(HERE/'swap.py'), 'select', '--run', str(run), *lockw(a), '--job', job, '--candidate', cand,
               '--clip-in', str(sel.get('clip_in') or 0), '--clip-out', str(sel['clip_out']), '--note', (sel.get('note') or '') + ' | retrim']
        print(json.dumps({'segment': seg_id, 'reselect': job, 'clip_out_was': sel['clip_out'], 'cut_at': left['tail'][0]}, ensure_ascii=False))
        if not a.dry_run: subprocess.run(cmd, check=True)
        return True
    ranges = left['ranges']
    name = f'{job}-{cand}'; total = media.probe(str(run/'generation'/'results'/f'{name}.mp4'), include_frames=True)['frame_count']
    keep, cur = [], 0
    for x, y in ranges:
        if x > cur: keep.append((cur, x - 1))
        cur = max(cur, y + 1)
    if cur < total: keep.append((cur, total - 1))
    keep = [(x, y) for x, y in keep if y - x + 1 >= 3]          # a stub of one or two frames would flash
    out = f'a{next_attempt(run, seg_id)}'
    cmd = [sys.executable, str(HERE/'swap.py'), 'splice', '--run', str(run), *lockw(a), '--segment', seg_id, '--out-attempt', out,
           '--note', f'去掉补做镜头末尾定住的画面（{sum(y - x + 1 for x, y in ranges)} 帧，无台词）：{ranges}']
    for x, y in keep: cmd += ['--picture', f'{name}:{x}-{y}']
    print(json.dumps({'segment': seg_id, 'from': name, 'still_frames': ranges, 'keep': keep, 'talking': talking, 'new': f'{seg_id}-{out}'}, ensure_ascii=False))
    if a.dry_run: return True
    subprocess.run(cmd, check=True)
    subprocess.run([sys.executable, str(HERE/'swap.py'), 'select', '--run', str(run), *lockw(a), '--job', f'{seg_id}-{out}', '--candidate', 'c1',
                    '--note', f'retrim: {name} 去掉补做镜头的定住画面'], check=True)
    return True


def cmd_reasons(a):
    """Why shots were redone, over one or more RUNs: the count per goof kind and what was seen. Read it after a batch
    to see which rule to fix so the first generation stops making that goof."""
    kinds, unknown, rows = {}, 0, []
    for r in a.runs:
        r = Path(r).resolve()
        for p in sorted((r/'jobs').glob('G*-*.json')):
            sr = read(p).get('shot_regen')
            if not sr: continue
            g = sr.get('goof') or {}
            if not g.get('kind'): unknown += 1; continue
            kinds[g['kind']] = kinds.get(g['kind'], 0) + 1
            rows.append({'run': r.name, 'job': p.stem, 'kind': g['kind'], 'note': g.get('note'), 'round': g.get('round'), 'seconds': sr.get('real_s')})
    print(json.dumps({'redos': len(rows) + unknown, 'by_kind': dict(sorted(kinds.items(), key=lambda x: -x[1])), 'without_reason': unknown,
                      'kinds': GOOF_KINDS, 'items': rows}, ensure_ascii=False, indent=1))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='cmd', required=True)
    q = sub.add_parser('ref'); q.add_argument('--run', required=True); q.add_argument('--seg', required=True)
    q.add_argument('--name', required=True, help='shot name, e.g. S170 (the job becomes G##-S170)')
    q.add_argument('--frames', action='append', help='A-B reference frames, end exclusive; repeat to join shots')
    q.add_argument('--source-s', help='A-B source seconds instead of --frames: a caption-free shot across two segments, cut from the working master')
    q.add_argument('--seconds', type=float, default=TARGET_S); q.add_argument('--keep-sound', help='joined pieces whose sound stays (0-based, default 0)')
    q.add_argument('--mute', action='append', help='A-B[,A-B] seconds of the clip to silence: the old word of a changed line (real-speed clips only)')
    q.add_argument('--hflip', action='store_true', help="mirror the reference: the segment's generated clip came out mirrored and this shot is spliced into it")
    q.add_argument('--real-speed', action='store_true', help='a shot under 4 s with on-screen speech: real speed, own sound, last frame held (no slow-down)')
    q.set_defaults(fn=cmd_ref)
    q = sub.add_parser('prepare'); q.add_argument('--run', required=True); q.add_argument('--name', required=True)
    q.add_argument('--user-quote', help="the user's own words asking for this regeneration (not needed with --goof)")
    q.add_argument('--goof', action='store_true', help='a goof redo under GOOF_RULE: exactly one shot, no hard cut inside the reference')
    q.add_argument('--channel', choices=['x100', 'apimart'], default=None, help='100x 100x-minimax-h3 standard 768P 9:16 (default, user 2026-11-26); apimart only on request')
    q.add_argument('--model', help='with --channel x100: 100x-minimax-h3 (default, 768P) or 100x-seedance-2.0 (720P)')
    q.add_argument('--again', action='store_true', help='the user asked to redo a shot that was already redone once')
    q.add_argument('--why', help='with --goof: which goof this redo fixes: ' + ' | '.join(GOOF_KINDS) + ' (or 1-8, the checklist number)'); q.add_argument('--note', help='with --goof: what was seen, in a few words')
    q.set_defaults(fn=cmd_prepare)
    q = sub.add_parser('splice'); q.add_argument('--run', required=True); q.add_argument('--seg', required=True)
    q.add_argument('--out-attempt', required=True); q.add_argument('shots', nargs='+'); q.add_argument('--base')
    q.add_argument('--shot-sound', action='append', help='no longer needed: a real-speed shot always keeps its own sound'); q.add_argument('--note'); q.add_argument('--dry-run', action='store_true')
    q.add_argument('--force', action='store_true', help='replace an earlier local splice with the same id')
    q.add_argument('--into', action='append', help='A-B base frames (inclusive) the one shot replaces exactly, from `locate`; with several shots NAME=A-B for each')
    q.add_argument('--source-sound', action='append', help="shot whose window takes the original film's sound (its lines are unchanged; SOUND_RULE case 1)")
    q.set_defaults(fn=cmd_splice)
    q = sub.add_parser('locate', help='find the goof shot: generated cut around --at, the same source moments, mirror check, next commands')
    q.add_argument('--run', required=True); q.add_argument('--seg', required=True); q.add_argument('--base')
    q.add_argument('--at', required=True, help='seconds in the generated clip where the goof shows (T or A-B)')
    q.add_argument('--snap', type=float, default=0.6, help='snap to a source cut within this many seconds')
    q.add_argument('--from-s', type=float, help='start the redo here inside a long single take (a pause before the goof), not at its cut')
    q.add_argument('--to-s', type=float, help='end the redo here: a second redo narrowed to the one action that still goes wrong')
    q.add_argument('--source-window', help="A-B seconds in the segment's source reference for this shot, checked by eye on the strips: when the generation dropped or added a cut and the shot is not at the source's seconds (goofs.json: \"source_s\")")
    q.set_defaults(fn=cmd_locate)
    q = sub.add_parser('round', help='every goof of the film redone in one batch: open -> write prompts -> submit -> look -> splice')
    rs = q.add_subparsers(dest='action', required=True)
    r = rs.add_parser('open', help='locate every goof, cut its reference, write its prompt stub and the round file')
    r.add_argument('--run', required=True); r.add_argument('--file', help='JSON list: [{"seg": "G05", "at": "3.2", "why": "competitor", "note": "…", "from_s"/"to_s"/"mute"/"name" optional, "no_mouth": true when you looked and no mouth is in the shot}]')
    r.add_argument('--goof', action='append', help='G##@秒[-秒]:原因:看到了什么 (repeat); 原因 = ' + ' | '.join(GOOF_KINDS)); r.add_argument('--round', type=int)
    r.add_argument('--mode', choices=['cards', 'reference'], default='cards',
                   help='cards (default, user 2026-11-23: a redo is generated from character cards + product images, no reference video); '
                        'reference only when the user asks for the old way (an item can also give "mode")')
    r.set_defaults(fn=cmd_round_open)
    r = rs.add_parser('submit', help='check every prompt of the round, then send all jobs together (blocks until downloaded: run it in the background)')
    r.add_argument('--run', required=True); r.add_argument('--round', type=int); r.add_argument('--only', help='NAME,NAME'); r.add_argument('--partial', action='store_true', help='send the prompts that pass although others fail')
    r.add_argument('--dry-run', action='store_true'); r.set_defaults(fn=cmd_round_submit)
    r = rs.add_parser('splice', help='one splice per segment with all its redone shots, then select it')
    r.add_argument('--run', required=True); r.add_argument('--round', type=int); r.add_argument('--drop', action='extend', nargs='+', help='shots whose result still has a goof (repeatable or comma-separated; they go to the next round)')
    r.add_argument('--partial', action='store_true'); r.add_argument('--no-select', action='store_true'); r.add_argument('--dry-run', action='store_true')
    r.add_argument('--resplice', action='append', help='NAME[,NAME]: spliced items to put back again on the current selection after round open re-located them (a changed sound plan); no new generation')
    r.add_argument('--finish', action='store_true', help='then run finish.py --from captions'); r.set_defaults(fn=cmd_round_splice)
    r = rs.add_parser('status'); r.add_argument('--run', required=True); r.set_defaults(fn=cmd_round_status)
    q = sub.add_parser('cards', help='build the character cards of RUN/cards/cast.json for card-mode redos (scripts/cards.py build)')
    q.add_argument('--run', required=True); q.add_argument('--only', nargs='*'); q.add_argument('--force', action='store_true')
    q.set_defaults(fn=lambda a: __import__('cards').cmd_build(a))
    q = sub.add_parser('reasons', help='why shots were redone, counted per goof kind over one or more RUNs'); q.add_argument('runs', nargs='+'); q.set_defaults(fn=cmd_reasons)
    q = sub.add_parser('retrim', help='cut still frames out of selected segments (redo shots inside a splice, a still end past the segment), free; finish.py runs it with --all')
    q.add_argument('--run', required=True); q.add_argument('--seg'); q.add_argument('--all', action='store_true', help='every segment of the film')
    q.add_argument('--dry-run', action='store_true')
    q.add_argument('--lock-wait', type=int, default=0, help='wait this many seconds for a RUN another step holds (passed to select / splice)'); q.set_defaults(fn=cmd_retrim)
    a = p.parse_args(); a.fn(a)


if __name__ == '__main__':
    main()
