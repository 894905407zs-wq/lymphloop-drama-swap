#!/usr/bin/env python3
"""Side-by-side comparison board: the original film on the left, the delivered film on the right, played in sync
through the recut timeline, with every change marked on one time axis.

User, 2026-11-19: "做一个像这样的看板，和原片对比，我要看和原片差多少，然后哪里改了我也要在轴上能看出来"
(modelled on the VO board .../新滴剂/同步对比看板). A folder that works offline: index.html + media/.

    compare_board.py --run RUN [--out DIR] [--previous VIDEO --previous-timeline JSON --previous-label TEXT]
                     [--notes JSON]

--notes is an optional {G##: {"issue": "...", "note": "..."}} written by the agent after reviewing: an issue turns the
segment red on the axis. Videos are copied (not moved); the source film is the one recorded at `init`.
"""
from __future__ import annotations
import oscompat  # noqa: E402,F401  (first import: UTF-8 mode on Windows, see oscompat.py)
import argparse, html, json, re, shutil, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import media_pipeline as media  # noqa: E402

KIND_LABEL = {'reuse': '共用结果', 'regen': '补做', 'replace': '生成换镜', 'issue': '仍有瑕疵', 'splice': '本地拼接',
              'audio': '只换声音', 'fix': '原片局部修补', 'source': '保留原片'}


def read(p): return json.loads(Path(p).read_text())


def paid_dependencies(job, ledgers, seen=None):
    """Paid leaves of a nested local splice, once per job, including native audio windows."""
    seen = set() if seen is None else seen
    if job in seen or job not in ledgers: return {}
    seen.add(job); ledger = ledgers[job]
    if ledger.get('provider') == 'reused_generation': return {}
    if ledger.get('provider') != 'local_splice': return {job: ledger}
    recipe = ledger.get('recipe') or {}
    refs = list(recipe.get('pictures') or []) + list(recipe.get('audio_windows') or []) + [recipe.get('audio') or {}]
    out = {}
    for ref in refs:
        source = str(ref.get('source') or '')
        key = source if source in ledgers else None
        if key is None:
            match = re.match(r'(.+?)-c\d+(?:\..*)?$', Path(source).name)
            key = match.group(1) if match else None
        if key: out.update(paid_dependencies(key, ledgers, seen))
    return out


def is_supplement(job, ledger):
    if (ledger.get('job') or {}).get('shot_regen'): return True
    match = re.search(r'-a(\d+)$', job)
    return bool(match and int(match.group(1)) > 1)


def sync_points(timeline, fps):
    """[[source_s, out_s], ...] breakpoints of the piecewise-linear source->output map."""
    pts = []
    for s in sorted(timeline['segments'], key=lambda x: x['out_in']):
        for a, b in ((s['source_in'], s['out_in']), (s['source_out'], s['out_out'])):
            p = [round(a / fps, 4), round(b / fps, 4)]
            if not pts or p != pts[-1]: pts.append(p)
    return pts


def to_out(pts, t):
    for (a0, b0), (a1, b1) in zip(pts, pts[1:]):
        if a0 <= t <= a1:
            return b0 + (b1 - b0) * ((t - a0) / (a1 - a0) if a1 > a0 else 0)
    return pts[-1][1] + (t - pts[-1][0])


def ranges_of(frames, fps, gap=3):
    out = []
    for f in sorted(set(frames)):
        if out and f - out[-1][1] <= gap: out[-1][1] = f
        else: out.append([f, f])
    return [[round(a / fps, 3), round((b + 1) / fps, 3)] for a, b in out]


def prompt_summary(text):
    m = re.search(r'的视频：(.+?)(?:，镜头顺序|。)', text)
    return m.group(1) if m else ''


def mute_windows(run, job_id):
    """Rewritten-word windows (seconds from the segment start) read from the muted reference file name."""
    job = run/'jobs'/f'{job_id}.json'
    if not job.is_file(): return []
    path = (read(job).get('references') or [{}])[0].get('path', '')
    m = re.search(r'muted-([\d._]+)\.mp4$', path)
    if not m: return []
    nums = [float(x) for x in m.group(1).split('_')]
    return [nums[i:i + 2] for i in range(0, len(nums) - 1, 2)]


def segment_rows(run, plan, sel, timeline, fps, notes, ledgers):
    tl_by_pic = {s['picture_id']: s for s in timeline['segments'] if s.get('picture_id')}
    pts = sync_points(timeline, fps)
    rows = []
    # planned segments and the ones reused from a donor film (swap.py reuse), in film order
    for seg in sorted(list(plan['segments']) + list(plan.get('reused_segments') or []), key=lambda x: x['in_frame']):
        gid = seg['id']; s = sel.get(gid) or {}
        job = s.get('job') or f'{gid}-a1'
        led = ledgers.get(job) or {}
        provider = led.get('provider', 'apimart' if led else None)
        paid = paid_dependencies(job, ledgers)
        extra = {j: l for j, l in paid.items() if is_supplement(j, l)}
        src = [round(seg['in_frame'] / fps, 3), round(seg['out_frame'] / fps, 3)]
        t = tl_by_pic.get(gid)
        out = [round(t['out_in'] / fps, 3), round(t['out_out'] / fps, 3)] if t else [round(to_out(pts, src[0]), 3), round(to_out(pts, src[1]), 3)]
        n = int(job.rsplit('-a', 1)[1]) if '-a' in job else 1
        if s.get('picture') == 'source' and s.get('audio') == 'candidate': kind = 'audio'
        elif s.get('picture') == 'source': kind = 'fix' if (notes.get(gid) or {}).get('fix') else 'source'
        elif provider == 'local_splice':
            kind = 'regen' if extra else 'splice'
        elif provider == 'reused_generation': kind = 'reuse'
        elif n > 1 and provider in ('apimart', 'x100'): kind = 'regen'
        else: kind = 'replace'
        note = notes.get(gid) or {}
        issue = note.get('issue', '')
        model = (led.get('job') or {}).get('model') or ('local_splice' if provider == 'local_splice' else '')
        if provider == 'reused_generation': model = '共用已审定结果（零次 API 提交）'
        if provider == 'local_splice':
            model = 'local_splice ← ' + ' / '.join(sorted({(l.get('job') or {}).get('model', '') for l in paid.values()}))
        prompt = next((f for f in (run/'prompts'/f'{job}.final.txt', run/'prompts'/f'{gid}-a1.final.txt') if f.is_file()), None)
        rw = seg.get('rewrites') or {}
        rows.append({'slot': gid, 'job': job, 'kind': 'issue' if issue else kind, 'baseKind': kind, 'src': src, 'out': out,
                     'delta': round((out[1] - out[0]) - (src[1] - src[0]), 3), 'offsetAfter': round(out[1] - src[1], 3),
                     'what': prompt_summary(prompt.read_text()) if prompt else '',
                     'rewrites': [{'old': r['original'], 'new': r['rewrite']} for r in (rw.get('dialogue') or []) + (rw.get('vo') or [])],
                     'model': model, 'cost': sum(float(c.get('cost_usd') or 0) for l in paid.values() for c in (l.get('candidates') or {}).values()),
                     'regenCost': sum(float(c.get('cost_usd') or 0) for l in extra.values() for c in (l.get('candidates') or {}).values()), 'paidJobs': sorted(paid),
                     'credits': sum(float(c.get('cost_credits') or 0) for l in paid.values() for c in (l.get('candidates') or {}).values()),
                     'regenCredits': sum(float(c.get('cost_credits') or 0) for l in extra.values() for c in (l.get('candidates') or {}).values()),
                     'issue': issue, 'note': note.get('note') or s.get('note') or '',
                     'mutes': [[round(src[0] + a, 3), round(src[0] + b, 3)] for a, b in mute_windows(run, job if kind != 'splice' else f'{gid}-a1')]})
    return rows


def build(a):
    run = Path(a.run).resolve(); state = read(run/'state.json')
    out_dir = Path(state['output_dir'])
    master = out_dir/'master.mp4'; timeline = read(str(master) + '.timeline.json')
    fps = float(timeline.get('source_fps') or 30)
    plan = read(run/'plan/segments.json'); sel = read(run/'plan/selections.json')
    notes = read(a.notes) if a.notes else (read(run/'plan/board_notes.json') if (run/'plan/board_notes.json').is_file() else {})
    ledgers = {p.stem: read(p) for p in (run/'generation/jobs').glob('*.json')}
    board = Path(a.out) if a.out else out_dir/'对比看板'
    (board/'media').mkdir(parents=True, exist_ok=True)

    def put(src, name):
        dst = board/'media'/name
        if not dst.is_file() or dst.stat().st_size != Path(src).stat().st_size or dst.stat().st_mtime < Path(src).stat().st_mtime:
            shutil.copy2(src, dst)
        return 'media/' + name
    rows = segment_rows(run, plan, sel, timeline, fps, notes, ledgers)
    pts = sync_points(timeline, fps)
    stamped = []
    scan = run/'watermark/scan.json'
    if scan.is_file() and (out_dir/'master.brand.json').is_file():      # original floating-watermark frames, source time
        stamped = ranges_of([d['frame'] for d in read(scan).get('detections') or [] if d.get('classification') == 'floating_overlay'], fps, gap=15)
    fix = out_dir/'master.fix.json'
    fixes = []
    if fix.is_file():
        inv = [[b, a] for a, b in pts]
        fixes = [[round(to_out(inv, j['f0'] / fps), 3), round(to_out(inv, (j['f1'] + 1) / fps), 3)] for j in read(fix).get('jobs') or []]
    src_info = media.probe(state['source']); out_info = media.probe(str(master))
    versions = [{'key': 'latest', 'label': a.latest_label, 'src': put(master, 'edited.mp4'), 'duration': out_info['video_duration'], 'sync': pts}]
    if a.previous:
        ptl = read(a.previous_timeline)
        versions.append({'key': 'previous', 'label': a.previous_label, 'src': put(a.previous, 'previous.mp4'),
                         'duration': media.probe(a.previous)['video_duration'], 'sync': sync_points(ptl, fps)})
    regen_cost = sum(r.get('regenCost', 0) for r in rows)
    regen_credits = sum(r.get('regenCredits', 0) for r in rows)
    total_cost = sum(float(c.get('cost_usd') or 0) for l in ledgers.values() for c in (l.get('candidates') or {}).values())
    data = {'title': a.title or Path(state['source']).stem, 'id': Path(state['source']).stem, 'fps': fps,
            'source': put(state['source'], 'original.mp4'), 'originalDuration': src_info['video_duration'],
            'versions': versions, 'rows': rows, 'stamped': stamped, 'fixes': fixes,
            'kindLabel': KIND_LABEL, 'cost': {'total': round(total_cost, 2), 'regen': round(regen_cost, 2), 'regenCredits': regen_credits},
            'notices': [f"{r['slot']}：{r['issue']}" for r in rows if r['issue']]}
    page = TEMPLATE.replace('__DATA__', json.dumps(data, ensure_ascii=False).replace('</', '<\\/')).replace('__TITLE__', html.escape(data['title']))
    (board/'index.html').write_text(page, encoding='utf-8')
    print(json.dumps({'board': str(board/'index.html'), 'segments': len(rows), 'kinds': {k: sum(r['kind'] == k for r in rows) for k in KIND_LABEL}},
                     ensure_ascii=False, indent=1))


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>换品对比 · __TITLE__</title>
<style>
:root{color-scheme:dark;--bg:#101312;--surface:#171b19;--panel:#1c211e;--line:#2b332e;--ink:#eef4f0;--muted:#a2afa6;--dim:#7a8a81;--accent:#a9e8c8;
--k-replace:#77ba95;--k-regen:#a993ef;--k-issue:#df8c82;--k-splice:#e2b168;--k-audio:#95abef;--k-fix:#7fc3c9;--k-source:#5d6a62;--k-mute:#f0d36b;--k-stamp:#d9dee0}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft Yahei",sans-serif;font-size:14px}
button,select{font:inherit;color:inherit}
button{cursor:pointer}
button:focus-visible,select:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
h1,h2,p{margin:0}
.top{display:flex;align-items:center;justify-content:space-between;gap:16px;border-bottom:1px solid var(--line);padding:14px 28px;background:#141816;flex-wrap:wrap}
h1{font-size:18px;font-weight:680;letter-spacing:-.02em}
.meta{margin-top:4px;color:var(--muted);font-size:11px}
.stats{display:flex;gap:8px;flex-wrap:wrap}
.stat{border:1px solid var(--line);border-radius:8px;padding:6px 10px;background:var(--surface);min-width:92px}
.stat b{display:block;font-size:15px;font-variant-numeric:tabular-nums}
.stat span{font-size:10px;color:var(--muted)}
.workspace{padding:18px clamp(16px,3vw,40px) 28px;max-width:1320px;margin:0 auto}
.players{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.player{border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--surface);min-width:0}
.player-title{height:40px;padding:0 11px;display:flex;gap:6px;justify-content:space-between;align-items:center;border-bottom:1px solid var(--line)}
.player-label{display:flex;gap:7px;align-items:center;font-weight:650;font-size:12px;white-space:nowrap}
.player-label::before{content:"";width:6px;height:6px;border-radius:50%;background:#909f95}
.edited .player-label::before{background:var(--accent)}
.video-time{font-variant-numeric:tabular-nums;color:var(--muted);font-size:11px;white-space:nowrap}
.viewport{height:clamp(290px,calc(100dvh - 380px),660px);background:#0b0e0c;display:grid;place-items:center;cursor:pointer;position:relative}
video{position:absolute;inset:0;display:block;width:100%;height:100%;object-fit:contain;background:#0b0e0c}
.badge{position:absolute;left:8px;top:8px;font-size:11px;padding:3px 7px;border-radius:5px;background:rgba(16,19,18,.82);border:1px solid var(--line);pointer-events:none}
.ver{height:28px;border:1px solid #36413a;background:#202722;border-radius:6px;font-size:11px;padding:0 6px;max-width:58%}
.deck{margin-top:12px;border:1px solid var(--line);border-radius:10px;background:var(--surface);padding:10px 12px}
.lane{display:grid;grid-template-columns:64px minmax(0,1fr);align-items:center;gap:8px}
.lane-name{font-size:10px;color:var(--muted);text-align:right;white-space:nowrap}
.timeline{position:relative;height:34px}
.track{position:absolute;left:8px;right:8px;top:12px;height:11px;border-radius:4px;background:#2c3530;overflow:hidden;pointer-events:none}
.mark{position:absolute;top:0;height:100%;opacity:.92;border-left:1px solid rgba(255,255,255,.35)}
.playhead{position:absolute;top:6px;bottom:6px;width:2px;background:#fff;pointer-events:none;margin-left:-1px}
.timeline input{position:absolute;inset:0;width:100%;height:34px;margin:0;appearance:none;background:transparent;cursor:pointer}
.timeline input::-webkit-slider-runnable-track{height:34px;background:transparent}
.timeline input::-webkit-slider-thumb{appearance:none;width:4px;height:34px;background:transparent}
.timeline input::-moz-range-thumb{width:4px;height:34px;background:transparent;border:0}
.thin{position:relative;height:9px;margin:2px 8px}
.thin .bg{position:absolute;inset:0;border-radius:3px;background:#222927}
.thin .mark{border:0}
.delta-lane{position:relative;height:30px;margin:2px 8px}
.delta-lane svg{position:absolute;inset:0;width:100%;height:100%}
.toolbar{display:flex;align-items:center;justify-content:space-between;gap:9px;flex-wrap:wrap;padding-top:8px}
.group{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.control{border:1px solid #36413a;background:#202722;border-radius:7px;height:34px;padding:0 11px;font-size:12px;white-space:nowrap}
.control:hover{background:#2a362e;border-color:#55705d}
.play{background:#b0eacb;border-color:#b0eacb;color:#17271e;min-width:77px;font-weight:700}
.play:hover{background:#caefda;color:#17271e}
.clock{font-variant-numeric:tabular-nums;font-size:12px;color:#dce9e1;white-space:nowrap}
.clock em{font-style:normal;color:var(--k-splice)}
.segmented{display:flex;gap:2px;background:#121713;border:1px solid #36413a;border-radius:7px;padding:2px}
.segmented button{border:0;background:transparent;color:var(--muted);font-size:11px;padding:6px 8px;border-radius:4px}
.segmented button[aria-pressed="true"]{color:#c8efda;background:#2a4737}
.rate{height:32px;padding:0 7px;background:#202722;border:1px solid #36413a;border-radius:7px;font-size:11px}
.chips{display:flex;gap:6px;overflow-x:auto;padding:8px 2px 2px;scrollbar-width:thin}
.chip{flex:none;height:26px;padding:0 8px;border:1px solid #35453b;border-radius:5px;background:#1b251e;font-size:11px;font-variant-numeric:tabular-nums;display:flex;align-items:center;gap:5px}
.chip i{width:7px;height:7px;border-radius:2px;display:inline-block}
.chip.active{background:#355344;border-color:#7fb197}
.legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:11px;margin-top:9px}
.legend span{display:flex;align-items:center;gap:5px}
.legend i{width:9px;height:9px;border-radius:2px;display:inline-block}
.now{padding:10px 0 0;color:#c8d9ce;font-size:12px;line-height:1.7;min-height:30px}
.hint{font-size:11px;color:var(--muted);line-height:1.65;margin-top:4px}
.notice{font-size:12px;line-height:1.7;color:#f2c3b9;background:#2b201e;border:1px solid #57392f;border-radius:8px;padding:10px 12px;margin-top:12px}
.notice ul{margin:0;padding-left:17px}
details{margin-top:12px;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
summary{padding:11px 14px;cursor:pointer;font-weight:650;font-size:13px}
.table-wrap{overflow-x:auto;padding:0 6px 10px}
table{border-collapse:collapse;width:100%;font-size:12px;min-width:860px}
th,td{border-top:1px solid var(--line);padding:8px 7px;text-align:left;vertical-align:top;line-height:1.55}
th{color:var(--muted);font-weight:600;font-size:11px;white-space:nowrap}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
tr.row{cursor:pointer}
tr.row:hover{background:#1f2622}
.tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px;color:#101312;white-space:nowrap}
.rw{color:#e8d48f}
.rw s{color:#9a8f86}
.pos{color:#f0c38a}.neg{color:#9ccbe8}.zero{color:var(--dim)}
@media(max-width:720px){
 .top{padding:12px 14px}.workspace{padding:12px 10px}
 .viewport{height:auto;aspect-ratio:9/16;max-height:55svh}
 .players{gap:6px}.player-title{height:auto;min-height:36px;padding:4px 7px;flex-wrap:wrap}
 .player-label,.video-time{font-size:10px}.lane{grid-template-columns:44px minmax(0,1fr);gap:4px}.lane-name{font-size:9px}
 .stat{min-width:0;flex:1 1 40%}
}
</style>
</head>
<body>
<header class="top">
  <div><h1 id="title"></h1><p class="meta" id="meta"></p></div>
  <div class="stats" id="stats"></div>
</header>
<main class="workspace">
  <div class="players">
    <section class="player">
      <div class="player-title"><span class="player-label">原片</span><span class="video-time" id="t-src"></span></div>
      <div class="viewport" data-play><video id="src" playsinline preload="auto"></video><span class="badge" id="b-src">原片</span></div>
    </section>
    <section class="player edited">
      <div class="player-title"><select class="ver" id="ver" aria-label="选择成片版本"></select><span class="video-time" id="t-out"></span></div>
      <div class="viewport" data-play><video id="out" playsinline preload="auto"></video><span class="badge" id="b-out"></span></div>
    </section>
  </div>
  <div class="deck">
    <div class="lane"><span class="lane-name">换了哪段</span>
      <div class="timeline"><div class="track" id="marks"></div><div class="playhead" id="ph"></div>
        <input id="seek" type="range" min="0" max="100" step="0.001" value="0" aria-label="按原片时间定位"></div></div>
    <div class="lane"><span class="lane-name">改词</span><div class="thin"><div class="bg"></div><div id="mutes"></div></div></div>
    <div class="lane"><span class="lane-name">水印盖章</span><div class="thin"><div class="bg"></div><div id="stamps"></div></div></div>
    <div class="lane"><span class="lane-name">时间差</span><div class="delta-lane" id="delta" title="对应段落在成片时间轴上提前或推后多少秒"></div></div>
    <div class="toolbar">
      <div class="group">
        <button class="control play" id="play">▶ 播放</button>
        <button class="control" id="prev" title="上一处改动">⟨ 上一处</button>
        <button class="control" id="next" title="下一处改动">下一处 ⟩</button>
        <span class="clock" id="clock"></span>
        <span class="clock" id="play-mode"></span>
      </div>
      <div class="group">
        <span style="font-size:11px;color:var(--muted)">声音</span>
        <div class="segmented" id="audio">
          <button data-a="src" aria-pressed="false">原片</button><button data-a="out" aria-pressed="true">成片</button><button data-a="mute" aria-pressed="false">静音</button>
        </div>
        <select class="rate" id="rate"><option>0.5</option><option>0.75</option><option selected>1</option><option>1.25</option><option>1.5</option></select>
      </div>
    </div>
    <div class="chips" id="chips"></div>
    <div class="legend" id="legend"></div>
    <p class="now" id="now" aria-live="polite"></p>
    <p class="hint">默认双方均为1倍速，听哪边就完整播放哪边；另一边按段落起点对齐，本段先播完时暂停等待。不会按新旧段长自动变速。时间轴按原片对应位置定位；两侧时钟显示各自实际播放位置。空格播放，←/→ 跳上一处/下一处改动，Shift+←/→ 逐帧。</p>
  </div>
  <div class="notice" id="notice" hidden></div>
  <details open><summary>逐段明细（点一行跳过去）</summary><div class="table-wrap"><table>
    <thead><tr><th>段</th><th>处理</th><th>原片</th><th>成片</th><th>时长差</th><th>之后累计</th><th>画面内容</th><th>改词</th><th>来源</th><th>说明 / 瑕疵</th></tr></thead>
    <tbody id="rows"></tbody></table></div></details>
</main>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const $ = id => document.getElementById(id);
const src = $('src'), out = $('out');
const COLOR = k => getComputedStyle(document.documentElement).getPropertyValue('--k-' + k).trim();
const fmt = t => { t = Math.max(0, t); const m = Math.floor(t / 60), s = t - m * 60; return `${String(m).padStart(2,'0')}:${s.toFixed(2).padStart(5,'0')}`; };
const sgn = d => (d > 0.0005 ? '+' : d < -0.0005 ? '−' : '±') + Math.abs(d).toFixed(2) + ' 秒';
let V = D.versions[0];
const changed = D.rows.filter(r => r.baseKind !== 'source' || r.kind === 'issue');

function map(pts, t, inv) {
  const a = inv ? 1 : 0, b = inv ? 0 : 1;
  for (let i = 1; i < pts.length; i++) {
    const p = pts[i - 1], q = pts[i];
    if (t <= q[a] || i === pts.length - 1) {
      const span = q[a] - p[a];
      return span > 0 ? p[b] + (q[b] - p[b]) * (t - p[a]) / span : p[b];
    }
  }
  return t;
}
function slope(pts, t) {
  for (let i = 1; i < pts.length; i++) { const p = pts[i - 1], q = pts[i];
    if (t >= p[0] && t < q[0]) return q[0] > p[0] ? (q[1] - p[1]) / (q[0] - p[0]) : 1; }
  return 1;
}
const rowAt = t => D.rows.find(r => t >= r.src[0] && t < r.src[1]);

// header
$('title').textContent = '换品对比 · ' + D.title;
const dur = D.originalDuration, outDur = () => V.duration;
function stats() {
  const replaced = changed.reduce((s, r) => s + (r.src[1] - r.src[0]), 0);
  const items = [
    [fmt(dur), '原片时长'], [fmt(outDur()), '成片时长（' + sgn(outDur() - dur) + '）'],
    [changed.length + ' 段', '改动段数'], [(replaced / dur * 100).toFixed(1) + '%', '改动占全片（' + replaced.toFixed(1) + ' 秒）'],
    [D.rows.filter(r => r.baseKind === 'regen').length + ' 段', '含补做镜头（$' + D.cost.regen.toFixed(2) + (D.cost.regenCredits ? ' + ' + D.cost.regenCredits + ' 积分' : '') + '）'],
    [D.rows.filter(r => r.issue).length + ' 处', '仍有瑕疵']];
  $('stats').innerHTML = items.map(([b, s]) => `<div class="stat"><b>${b}</b><span>${s}</span></div>`).join('');
  $('meta').textContent = `${D.id} · Lymphoria → yesnap LymphLoop · 生成总花费 $${D.cost.total.toFixed(2)}`;
}
// version picker
$('ver').innerHTML = D.versions.map((v, i) => `<option value="${i}">${v.label}</option>`).join('');
$('ver').onchange = () => { const t = cur(), resume = playing; play(false); V = D.versions[+$('ver').value]; out.addEventListener('loadedmetadata', () => { seekTo(t); if (resume) play(true); }, {once: true}); out.src = V.src; stats(); drawDelta(); table(); };
src.src = D.source; out.src = V.src;

// axis
const pct = t => (t / dur * 100) + '%';
function bar(el, a, b, color, title, extra) {
  const d = document.createElement('div'); d.className = 'mark' + (extra || ''); d.style.left = pct(a); d.style.width = `max(2px, ${((b - a) / dur * 100)}%)`;
  d.style.background = color; if (title) d.title = title; el.appendChild(d); return d;
}
D.rows.forEach(r => { if (r.baseKind === 'source' && r.kind !== 'issue') return; bar($('marks'), r.src[0], r.src[1], COLOR(r.kind), `${r.slot} ${D.kindLabel[r.kind]} ${fmt(r.src[0])}–${fmt(r.src[1])}`); });
D.fixes.forEach(f => bar($('marks'), f[0], f[1], COLOR('fix'), '原片局部修补'));
D.rows.forEach(r => r.mutes.forEach(m => bar($('mutes'), m[0], m[1], COLOR('mute'), `${r.slot} 改词：` + r.rewrites.map(x => x.old + ' → ' + x.new).join('；'))));
D.stamped.forEach(s => bar($('stamps'), s[0], s[1], COLOR('stamp'), '原片漂浮水印 LYMPHORIA → 白色 LYMPHLOOP'));
function drawDelta() {
  const pts = V.sync, W = 1000, H = 30, mx = Math.max(0.5, ...pts.map(p => Math.abs(p[1] - p[0])));
  const y = d => H / 2 - d / mx * (H / 2 - 2);
  const path = pts.map((p, i) => `${i ? 'L' : 'M'}${(p[0] / dur * W).toFixed(1)},${y(p[1] - p[0]).toFixed(1)}`).join('');
  $('delta').innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><line x1="0" x2="${W}" y1="${H/2}" y2="${H/2}" stroke="#3a4640" stroke-dasharray="4 4"/>` +
    `<path d="${path}" fill="none" stroke="${COLOR('splice')}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>` +
    `<text x="4" y="9" fill="#7a8a81" font-size="8">±${mx.toFixed(2)}s</text></svg>`;
}
const used = [...new Set(D.rows.map(r => r.kind))].concat(D.fixes.length ? ['fix'] : []);
$('legend').innerHTML = [...new Set(used)].filter(k => k !== 'source').map(k => `<span><i style="background:${COLOR(k)}"></i>${D.kindLabel[k]}</span>`).join('') +
  `<span><i style="background:${COLOR('mute')}"></i>改词（旧词消音、模型重配）</span><span><i style="background:${COLOR('stamp')}"></i>水印换成 LYMPHLOOP</span>`;
// chips
$('chips').innerHTML = changed.map((r, i) => `<button class="chip" data-i="${i}"><i style="background:${COLOR(r.kind)}"></i>${r.slot} ${fmt(r.src[0])}</button>`).join('');
$('chips').onclick = e => { const b = e.target.closest('.chip'); if (b) jump(changed[+b.dataset.i]); };
if (D.notices.length) { $('notice').hidden = false; $('notice').innerHTML = '<b>仍有瑕疵</b><ul>' + D.notices.map(n => `<li>${n}</li>`).join('') + '</ul>'; }

function table() {
  const esc = s => String(s || '').replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
  const cls = d => d > 0.0005 ? 'pos' : d < -0.0005 ? 'neg' : 'zero';
  $('rows').innerHTML = D.rows.map((r, i) => {
    const o = [map(V.sync, r.src[0]), map(V.sync, r.src[1])], d = (o[1] - o[0]) - (r.src[1] - r.src[0]), after = o[1] - r.src[1];
    return `<tr class="row" data-i="${i}"><td><b>${r.slot}</b></td><td><span class="tag" style="background:${COLOR(r.kind)}">${D.kindLabel[r.kind]}</span></td>` +
      `<td class="num">${fmt(r.src[0])}<br>${fmt(r.src[1])}</td><td class="num">${fmt(o[0])}<br>${fmt(o[1])}</td>` +
      `<td class="num ${cls(d)}">${sgn(d)}</td><td class="num ${cls(after)}">${sgn(after)}</td><td>${esc(r.what)}</td>` +
      `<td class="rw">${r.rewrites.map(x => `<s>${esc(x.old)}</s> → ${esc(x.new)}`).join('<br>')}</td>` +
      `<td>${esc(r.job)}<br><span style="color:var(--muted)">${esc(r.model)}${r.cost ? ' · $' + r.cost.toFixed(2) : ''}${r.credits ? ' · ' + r.credits + ' 积分' : ''}</span></td>` +
      `<td>${r.issue ? '<span style="color:#f2b9ac">' + esc(r.issue) + '</span><br>' : ''}${esc(r.note)}</td></tr>`;
  }).join('');
}
$('rows').onclick = e => { const tr = e.target.closest('tr.row'); if (tr) jump(D.rows[+tr.dataset.i]); };

// playback
let playing = false;
let audioSide = 'out', lastPlaybackSpan = null;
const leadVideo = () => audioSide === 'src' ? src : out;
const cur = () => audioSide === 'src' ? src.currentTime : map(V.sync, out.currentTime, true);
function playbackSpan(pts, time, leadOut) {
  const a = leadOut ? 1 : 0, b = 1 - a;
  let span = null;
  for (let i = 1; i < pts.length; i++) {
    const p = pts[i - 1], q = pts[i];
    if (q[a] <= p[a] || q[b] <= p[b]) continue;
    span = { index: i, start: p[a], end: q[a], otherStart: p[b], otherEnd: q[b] };
    if (time < q[a] - 1e-6) break;
  }
  return span;
}
function applyPlaybackRate() {
  const rate = Number($('rate').value) || 1;
  src.playbackRate = rate; out.playbackRate = rate;
  return rate;
}
function startVideo(video) {
  const result = video.play();
  if (result && result.catch) result.catch(() => { if (video === leadVideo()) play(false); });
}
function syncFollower(force = false) {
  const lead = leadVideo(), follower = lead === out ? src : out;
  const span = playbackSpan(V.sync, lead.currentTime, lead === out);
  if (!span) return;
  const elapsed = Math.max(0, lead.currentTime - span.start);
  const otherDuration = span.otherEnd - span.otherStart;
  const hold = elapsed >= otherDuration - 0.005;
  const target = hold ? span.otherEnd - Math.min(1 / D.fps, otherDuration / 2) : span.otherStart + elapsed;
  const key = `${audioSide}:${span.index}`;
  if (force || key !== lastPlaybackSpan || Math.abs(follower.currentTime - target) > 0.18) follower.currentTime = target;
  lastPlaybackSpan = key;
  if (hold) follower.pause();
  else if (playing && follower.paused) startVideo(follower);
  $('play-mode').textContent = hold ? `${lead === out ? '原片' : '成片'}本段已播完，等待${lead === out ? '成片' : '原片'}` : '按所选倍速播放';
}
function seekTo(t) {
  t = Math.min(Math.max(0, t), dur - 0.05);
  src.currentTime = t; out.currentTime = Math.min(map(V.sync, t), outDur() - 0.05);
  lastPlaybackSpan = null; if (playing) syncFollower(true); ui();
}
function jump(r) { seekTo(r.src[0] + 0.02); }
function ui() {
  const t = cur(), o = out.currentTime, r = rowAt(t);
  $('seek').max = dur; $('seek').value = t; $('ph').style.left = `calc(8px + (100% - 16px) * ${t / dur})`;
  $('t-src').textContent = fmt(t) + ' / ' + fmt(dur); $('t-out').textContent = fmt(o) + ' / ' + fmt(outDur());
  $('clock').innerHTML = `原片 ${fmt(src.currentTime)} ↔ 成片 ${fmt(o)}　<em>${sgn(o - src.currentTime)}</em>`;
  $('b-out').textContent = r && (r.baseKind !== 'source' || r.kind === 'issue') ? `${r.slot} · ${D.kindLabel[r.kind]}` : '原片画面';
  $('b-src').textContent = r ? `原片 · ${r.slot}` : '原片';
  document.querySelectorAll('.chip').forEach(c => c.classList.toggle('active', changed[+c.dataset.i] === r));
  $('now').innerHTML = r && (r.baseKind !== 'source' || r.kind === 'issue')
    ? `<b>${r.slot}</b>（${D.kindLabel[r.kind]}，${r.job}）原片 ${fmt(r.src[0])}–${fmt(r.src[1])} → 成片 ${fmt(map(V.sync, r.src[0]))}–${fmt(map(V.sync, r.src[1]))}。${r.what || ''}` +
      (r.rewrites.length ? `　改词：${r.rewrites.map(x => x.old + ' → ' + x.new).join('；')}` : '') + (r.issue ? `　<span style="color:#f2b9ac">瑕疵：${r.issue}</span>` : '')
    : '这里是原片画面和原声，没有改动。';
}
function audio(which) {
  const t = cur();
  audioSide = which;
  src.muted = which !== 'src'; out.muted = which !== 'out';
  document.querySelectorAll('#audio button').forEach(b => b.setAttribute('aria-pressed', b.dataset.a === which));
  if (playing) {
    if (which === 'src') src.currentTime = t; else out.currentTime = map(V.sync, t);
    lastPlaybackSpan = null; syncFollower(true); startVideo(leadVideo());
  }
}
$('audio').onclick = e => { const b = e.target.closest('button'); if (b) audio(b.dataset.a); };
audio('out');
function play(on) {
  if (on && leadVideo().ended) seekTo(0);
  playing = on; $('play').textContent = on ? '❚❚ 暂停' : '▶ 播放';
  applyPlaybackRate();
  if (on) { lastPlaybackSpan = null; startVideo(leadVideo()); syncFollower(true); }
  else { src.pause(); out.pause(); }
}
$('play').onclick = () => play(!playing);
document.querySelectorAll('[data-play]').forEach(v => v.onclick = () => play(!playing));
$('seek').oninput = () => seekTo(+$('seek').value);
$('rate').onchange = applyPlaybackRate;
function step(dir) { const t = cur(); const list = changed.map(r => r.src[0]); const n = dir > 0 ? list.find(x => x > t + 0.05) : [...list].reverse().find(x => x < t - 0.3); if (n !== undefined) seekTo(n + 0.02); }
$('prev').onclick = () => step(-1); $('next').onclick = () => step(1);
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'SELECT' || e.target.tagName === 'INPUT' && e.target.type !== 'range') return;
  if (e.code === 'Space') { e.preventDefault(); play(!playing); }
  else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { e.preventDefault(); const d = e.key === 'ArrowRight' ? 1 : -1;
    if (e.shiftKey) { play(false); seekTo(cur() + d / D.fps); } else step(d); }
});
setInterval(() => {
  if (!playing) return;
  applyPlaybackRate();
  if (leadVideo().ended) play(false); else syncFollower();
  ui();
}, 100);
src.addEventListener('seeked', ui); src.addEventListener('loadedmetadata', ui);
stats(); drawDelta(); table(); ui();
</script>
</body>
</html>
'''


def _make_range_handler(roots, index_html=None):
    import http.server, mimetypes
    from urllib.parse import unquote

    class RangeHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _resolve(self):
            raw = unquote(self.path.split('?', 1)[0])
            if raw in ('', '/') and index_html is not None:
                return None
            for prefix, base in roots.items():
                if prefix != '/' and raw == prefix.rstrip('/'):
                    return ('REDIRECT', prefix + 'index.html')
                if raw.startswith(prefix):
                    rel = raw[len(prefix):] or 'index.html'
                    cand = (Path(base) / rel).resolve()
                    try:
                        cand.relative_to(Path(base).resolve())
                    except ValueError:
                        return ('FORBIDDEN', None)
                    return cand
            return ('NOT_FOUND', None)

        def do_HEAD(self):
            self._serve(head_only=True)

        def do_GET(self):
            self._serve(head_only=False)

        def _serve(self, head_only=False):
            target = self._resolve()
            if target is None:
                data = index_html.encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if not head_only:
                    self.wfile.write(data)
                return
            if isinstance(target, tuple):
                kind, val = target
                if kind == 'REDIRECT':
                    self.send_response(302)
                    self.send_header('Location', val)
                    self.end_headers()
                elif kind == 'FORBIDDEN':
                    self.send_error(403, 'Forbidden')
                else:
                    self.send_error(404, 'Not found')
                return
            if target.is_dir():
                target = target / 'index.html'
            if not target.is_file():
                self.send_error(404, 'Not found')
                return
            size = target.stat().st_size
            ctype = oscompat.mime_type(target)
            ctype = ctype or 'application/octet-stream'
            if ctype == 'text/html':
                ctype = 'text/html; charset=utf-8'
            rng = self.headers.get('Range')
            start, end = 0, size - 1
            status = 200
            if rng:
                m = re.match(r'bytes=(\d*)-(\d*)', rng)
                if m:
                    s, e = m.groups()
                    if s:
                        start = int(s)
                    if e:
                        end = min(int(e), size - 1)
                    if start >= size:
                        self.send_error(416, 'Range Not Satisfiable')
                        return
                    status = 206
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(length))
            if status == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            if head_only:
                return
            with target.open('rb') as f:
                f.seek(start)
                rem = length
                while rem > 0:
                    chunk = f.read(min(65536, rem))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except BrokenPipeError:
                        break
                    rem -= len(chunk)

    return RangeHandler


def serve_boards(board_dirs, port=8765, host='127.0.0.1'):
    """Serve one or more comparison board directories over HTTP with Range support for video seeking."""
    import socketserver

    class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
        allow_reuse_address = True
        daemon_threads = True

    if len(board_dirs) == 1:
        _, bdir = board_dirs[0]
        roots = {'/': Path(bdir).resolve()}
        index_html = None
    else:
        roots = {}
        links = []
        for idx, (label, bdir) in enumerate(board_dirs, 1):
            prefix = f'/b{idx}/'
            roots[prefix] = Path(bdir).resolve()
            links.append(f'<a href="{prefix}index.html">{html.escape(label)}<small>{html.escape(str(bdir))}</small></a>')
        index_html = (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>LymphLoop 对比看板</title>'
            '<style>body{background:#101312;color:#eef4f0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;padding:40px;max-width:740px;margin:auto}'
            'a{display:block;padding:18px 22px;margin:14px 0;background:#171b19;border:1px solid #2b332e;border-radius:10px;color:#a9e8c8;text-decoration:none;font-size:17px;font-weight:600}'
            'a:hover{border-color:#a9e8c8;background:#1c211e}small{display:block;color:#a2afa6;font-weight:400;font-size:12px;margin-top:6px}</style></head>'
            f'<body><h1>LymphLoop 换品对比看板</h1>{"".join(links)}</body></html>'
        )
    handler = _make_range_handler(roots, index_html=index_html)
    with ThreadedTCPServer((host, int(port)), handler) as httpd:
        print(f'Serving comparison board(s) at http://{host}:{port}/', flush=True)
        for prefix, base in roots.items():
            print(f'  http://{host}:{port}{prefix}index.html -> {base}', flush=True)
        httpd.serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run'); ap.add_argument('--out'); ap.add_argument('--title')
    ap.add_argument('--notes', help='{G##: {"issue": "...", "note": "...", "fix": true}}; default RUN/plan/board_notes.json')
    ap.add_argument('--latest-label', default='最新成片')
    ap.add_argument('--previous'); ap.add_argument('--previous-timeline'); ap.add_argument('--previous-label', default='上一版')
    ap.add_argument('--serve', action='store_true', help='start a Range-capable local HTTP server after building (or serve --runs)')
    ap.add_argument('--port', type=int, default=8765, help='HTTP port for --serve (default 8765)')
    ap.add_argument('--runs', nargs='+', help='with --serve: serve comparison boards from multiple RUN directories on one index page')
    a = ap.parse_args()
    if a.previous and not a.previous_timeline: ap.error('--previous needs --previous-timeline')
    if a.runs:
        board_dirs = []
        for r in a.runs:
            rp = Path(r).resolve()
            bdir = rp / 'out' / '对比看板'
            if not (bdir / 'index.html').is_file():
                sub = argparse.Namespace(run=str(rp), out=None, title=None, notes=None, latest_label=a.latest_label,
                                         previous=None, previous_timeline=None, previous_label=a.previous_label)
                build(sub)
            board_dirs.append((rp.name, bdir))
        if a.serve:
            serve_boards(board_dirs, port=a.port)
        return
    if not a.run:
        ap.error('--run is required (or pass --runs RUN1 RUN2 ... --serve)')
    build(a)
    if a.serve:
        bdir = Path(a.out).resolve() if a.out else (Path(a.run).resolve() / 'out' / '对比看板')
        serve_boards([(Path(a.run).resolve().name, bdir)], port=a.port)


if __name__ == '__main__':
    main()
