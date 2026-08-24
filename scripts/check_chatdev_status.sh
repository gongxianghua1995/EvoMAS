#!/usr/bin/env bash
# 检查 4 域 ChatDev baseline: agent 完成情况 + 评估进度/resolve 分布
# 用法: bash scripts/check_chatdev_status.sh

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 - <<'PY'
import json, os
from pathlib import Path

domains = ['django', 'sympy', 'matplotlib', 'scikit-learn']
expect  = {'django':40, 'sympy':40, 'matplotlib':34, 'scikit-learn':32}
cfg_sub = 'swe_bench_verified/chatdev_Deepseek-V4-Flash-0731'

hdr = (f"{'domain':<14}{'patch':>6}{'eval':>6}{'FULL':>6}{'PART':>6}"
       f"{'NO':>5}{'?':>5}{'agent':>7}{'agent_fin':>9}{'eval':>6}{'eval_fin':>9}")
print(hdr)
print('-' * len(hdr))
tot_p = tot_e = tot_f = tot_pa = tot_no = tot_un = 0
agent_all = eval_all = True
for d in domains:
    base   = Path('output') / f'chatdev_{d}' / 'output_selected'
    patch_dir = base / cfg_sub
    rj     = patch_dir / 'results.json'
    ev     = patch_dir / 'evaluation_results.json'

    patches = len(list(patch_dir.glob('*.txt'))) if patch_dir.exists() else 0

    evaled = full = part = no = unk = 0
    if rj.exists():
        try:
            for x in json.load(open(rj)):
                r = x.get('resolved')
                if r == 'FULL': full += 1
                elif r == 'PARTIAL': part += 1
                elif r == 'NO': no += 1
                else: unk += 1
        except Exception as e:
            print(f"  {d}: results.json err: {e}")
    evaled = full + part + no

    agent_pid_f = base / 'run.pid'
    agent_done = base / 'run.done'
    eval_pid_f  = base / 'eval.pid'
    eval_done   = base / 'eval.done'

    def check_alive(pid_path):
        if not pid_path.exists(): return '?'
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return 'yes'
        except ProcessLookupError:
            return 'no'
        except Exception:
            return '?'

    a_alive = check_alive(agent_pid_f)
    e_alive = check_alive(eval_pid_f)
    a_fin = 'yes' if agent_done.exists() else 'no'
    e_fin = 'yes' if eval_done.exists() else 'no'
    if a_fin != 'yes': agent_all = False
    if e_fin != 'yes': eval_all = False

    print(f"{d:<14}{patches:>6}{evaled:>6}{full:>6}{part:>6}{no:>5}{unk:>5}"
          f"{a_alive:>7}{a_fin:>9}{e_alive:>6}{e_fin:>9}")
    tot_p += patches; tot_e += evaled
    tot_f += full; tot_pa += part; tot_no += no; tot_un += unk
print('-' * len(hdr))
print(f"{'TOTAL':<14}{tot_p:>6}{tot_e:>6}{tot_f:>6}{tot_pa:>6}{tot_no:>5}{tot_un:>5}")
print()
line = []
line.append(f"agent: {'all done' if agent_all else 'running'}")
line.append(f"eval:  {'all done' if eval_all else 'running'}")
if tot_e:
    acc = tot_f / tot_e
    print(f"accuracy (FULL/eval'd) = {tot_f}/{tot_e} = {acc:.2%}")
print(' | '.join(line))
PY
