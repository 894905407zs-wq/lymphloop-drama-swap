#!/usr/bin/env python3
"""One command from the selected segments to the delivered master and its comparison board:

    retrim -> captions -> recut -> brand -> final-screen -> compare_board

retrim (user 2026-11-23: "回剪的时候把定格帧裁掉") cuts the still frames out of every selected segment first: the still
end of a generated clip and the still frames of redo shots inside a splice (shot_regen.py retrim --all; free, picture and
sound together, never shorter than the source shot, never over speech). It runs whenever captions run, and captions run
again when it changed a segment. recut refuses a cut with still frames left.

(11-19 test film 2412: run five times by hand as a shell chain after each round of fixes; timings were captions 48 s,
recut 96 s, brand 55 s, final-screen 540 s.) Each step is a separate `swap.py` process (each takes the RUN lock
itself), logs to RUN/logs/finish-<step>.log, and the chain stops at the first failure with the log tail. Start it in
the background:  nohup $PY scripts/finish.py --run RUN > RUN/logs/finish.log 2>&1 &

    --from STEP        start at retrim | captions | recut | brand | final-screen | board (earlier steps already done;
                       --from captions runs retrim first too)
    --skip-retrim      do not cut still frames first
    --skip-captions    the selections did not change any generated picture (captions are kept as they are)
    --full             final-screen over the whole master instead of the changed parts
    --previous FILE --previous-timeline FILE --previous-label TEXT   an earlier master for the board's version menu
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEPS = ['retrim', 'captions', 'recut', 'brand', 'final-screen', 'board']


def has_floating_mark(run):
    scan = Path(run)/'watermark/scan.json'
    if not scan.is_file(): return True          # brand itself refuses with the right message
    return any(d.get('classification') == 'floating_overlay' for d in json.loads(scan.read_text()).get('detections', []))


def step_cmd(step, a, run):
    py = sys.executable
    if step == 'retrim':
        return [py, str(HERE/'shot_regen.py'), 'retrim', '--run', str(run), '--all']
    if step == 'board':
        cmd = [py, str(HERE/'compare_board.py'), '--run', str(run)]
        for k in ('previous', 'previous_timeline', 'previous_label'):
            v = getattr(a, k)
            if v: cmd += ['--' + k.replace('_', '-'), v]
        return cmd
    cmd = [py, str(HERE/'swap.py'), step, '--run', str(run)]
    if step == 'final-screen' and a.full: cmd.append('--full')
    if step == 'recut':
        for g in a.user_approved_cut or []: cmd += ['--user-approved-cut', g]   # a segment that ends early because the next one carries the rest of its shot
    return cmd


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--run', required=True); p.add_argument('--from', dest='start', choices=STEPS, default='captions')
    p.add_argument('--skip-captions', action='store_true'); p.add_argument('--full', action='store_true')
    p.add_argument('--skip-retrim', action='store_true', help='do not cut still frames first (recut still refuses still frames left)')
    p.add_argument('--user-approved-cut', action='append', metavar='G##', help='passed to recut (only with the user\'s explicit OK)')
    p.add_argument('--previous'); p.add_argument('--previous-timeline'); p.add_argument('--previous-label')
    a = p.parse_args()
    run = Path(a.run).resolve(); logs = run/'logs'; logs.mkdir(exist_ok=True)
    todo = STEPS[STEPS.index(a.start):]
    if a.start == 'captions' and not a.skip_retrim: todo.insert(0, 'retrim')      # --from captions (after a round splice) still trims
    if a.skip_retrim and 'retrim' in todo: todo.remove('retrim')
    skip_captions = a.skip_captions
    if 'brand' in todo and not has_floating_mark(run):
        todo.remove('brand'); print('brand: skipped (watermark none: no floating watermark in this film)')
    timings = {}
    for step in list(todo):
        if step == 'captions' and skip_captions: continue
        if step == 'board':
            # the agent's own look comes before delivery (user 2026-11-24: "可是我看着还是有产品穿帮镜头"): the flashback sheets
            # and the product crop sheets of this master must be reviewed (swap.py flashback-review / product-review)
            r = subprocess.run([sys.executable, str(HERE/'swap.py'), 'reviews-pending', '--run', str(run)], capture_output=True, text=True)
            if r.returncode == 3:
                pend = json.loads(r.stdout).get('pending', [])
                print('NEEDS_AGENT_REVIEW', json.dumps(pend, ensure_ascii=False))
                raise SystemExit('finish: open every sheet listed above yourself, record it with swap.py flashback-review / product-review '
                                 '(--clean, or --hit and put the goofs in goofs.json), then finish.py --from board. Do not deliver this master before.')
            if r.returncode: raise SystemExit(f'reviews-pending failed: {(r.stderr or r.stdout)[-400:]}')
        log = logs/f'finish-{step}.log'; t0 = time.time()
        print(f'== {step}', flush=True)
        with open(log, 'w') as fh:
            rc = subprocess.run(step_cmd(step, a, run), stdout=fh, stderr=subprocess.STDOUT).returncode
        timings[step] = round(time.time() - t0)
        print(f'{step} {timings[step]}s', flush=True)
        if rc != 0:
            tail = log.read_text().splitlines()[-15:]
            raise SystemExit(f'{step} failed (exit {rc}); {log}:\n' + '\n'.join(tail))
        if step == 'retrim':
            line = next((x for x in reversed(log.read_text().splitlines()) if x.startswith('RETRIM_CHANGED ')), 'RETRIM_CHANGED []')
            changed = json.loads(line.split(' ', 1)[1])
            print(f'retrim: cut still frames in {changed}' if changed else 'retrim: no still frames to cut', flush=True)
            if changed and skip_captions:
                skip_captions = False; print('captions: run again (retrim changed the selected picture)', flush=True)
    (logs/'finish.json').write_text(json.dumps({'steps': todo, 'seconds': timings, 'finished_at': time.strftime('%Y-%m-%dT%H:%M:%S')}, indent=1))
    print('FINISH_DONE', json.dumps(timings))


if __name__ == '__main__':
    main()
