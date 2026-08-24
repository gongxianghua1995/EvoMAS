#!/usr/bin/env python3
"""Generate the ChatDev Baseline SWE-bench Verified Docker evaluation markdown report.

Reads:
  - SWE-bench harness summary reports under <report_dir>/<MODEL>.evomas_docker_<domain>.json
  - Per-domain results.json under <output_root>/chatdev_<domain>/output_selected/...
    (for duration_seconds, total_tokens per case)

Writes the final markdown to output_paper/<out_basename>.md (exactly matches the
existing report template structure: 实验配置 / 总体得分 / 三项核心指标 / 按域详情
/ 典型失败 case 分析 / 与上次实验对比 / 结论与建议).

Usage
-----
# After run_chatdev_baseline_run_20260822.sh finishes:
python scripts/generate_docker_eval_report.py \
    --output-root output_run_20260822 \
    --report-dir /tmp/evomas_swebench_reports_20260822 \
    --out-md output_paper/"ChatDev Baseline SWE-bench Verified 全域Docker评估报告.run_20260822.md"

# Optional: also feed in a previous summary json for delta comparison (e.g. the
# one from /tmp/evomas_swebench_reports/chatdev_Deepseek-V4-Flash-0731.evomas_docker_scikit-learn.json
# merged across 4 domains)
python scripts/generate_docker_eval_report.py \
    --output-root output_run_20260822 \
    --report-dir /tmp/evomas_swebench_reports_20260822 \
    --prev-report-glob "/tmp/evomas_swebench_reports/chatdev_Deepseek-V4-Flash-0731.evomas_docker_*.json" \
    --prev-results-glob "output/chatdev_*/output_selected/swe_bench_verified/*/results.json" \
    --out-md output_paper/"ChatDev Baseline SWE-bench Verified 全域Docker评估报告.run_20260822.md"
"""

import argparse
import json
import glob as _glob
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

DOMAINS = ["django", "sympy", "matplotlib", "scikit-learn"]
REPO_LABEL = {
    "django": "django/django",
    "sympy": "sympy/sympy",
    "matplotlib": "matplotlib/matplotlib",
    "scikit-learn": "scikit-learn/scikit-learn",
}
MODEL = "chatdev_Deepseek-V4-Flash-0731"


def _find_results_json(output_root: Path, domain: str) -> Path | None:
    base = output_root / f"chatdev_{domain}" / "output_selected"
    if not base.exists():
        return None
    # output_selected/{dataset_name}/{MODEL}/results.json
    candidates = sorted(base.rglob("results.json"), key=lambda p: -p.stat().st_size)
    return candidates[0] if candidates else None


def _find_summary_json(report_dir: Path, domain: str) -> Path | None:
    p = report_dir / f"{MODEL.replace('/', '__')}.evomas_docker_{domain}.json"
    return p if p.exists() else None


def _merge_summaries(report_dir: Path):
    """Return {'total':..., 'resolved':..., 'by_domain':{d: {...}}} plus a
    per-instance id -> (status, domain) map."""
    by_domain = {}
    id_status = {}
    for d in DOMAINS:
        p = _find_summary_json(report_dir, d)
        if not p:
            by_domain[d] = {
                "total": 0, "resolved": 0, "unresolved": 0,
                "empty_patch": 0, "error": 0, "infra": 0, "ambiguous": 0,
                "summary": {}, "completed": 0,
            }
            continue
        s = json.loads(p.read_text())
        entry = {
            "total": s.get("total_instances", 0),
            "completed": s.get("completed_instances", 0),
            "resolved": s.get("resolved_instances", 0),
            "unresolved": s.get("unresolved_instances", 0),
            "empty_patch": s.get("empty_patch_instances", 0),
            "error": s.get("error_instances", 0),
            "infra": s.get("infra_failure_instances", 0),
            "ambiguous": s.get("ambiguous_failure_instances", 0),
            "summary": s,
            "resolved_ids": set(s.get("resolved_ids", [])),
            "unresolved_ids": set(s.get("unresolved_ids", [])),
            "empty_patch_ids": set(s.get("empty_patch_ids", [])),
            "error_ids": set(s.get("error_ids", [])),
            "infra_ids": set(s.get("infra_failure_ids", [])),
            "ambiguous_ids": set(s.get("ambiguous_failure_ids", [])),
            "failure_reasons": s.get("failure_reasons", {}),
        }
        by_domain[d] = entry
        for iid in entry["resolved_ids"]:   id_status[iid] = ("RESOLVED", d)
        for iid in entry["unresolved_ids"]: id_status[iid] = ("UNRESOLVED", d)
        for iid in entry["empty_patch_ids"]: id_status[iid] = ("EMPTY_PATCH", d)
        for iid in entry["error_ids"]: id_status[iid] = ("ERROR", d)
        for iid in entry["infra_ids"]: id_status[iid] = ("INFRA_FAILURE", d)
        for iid in entry["ambiguous_ids"]: id_status[iid] = ("AMBIGUOUS", d)
    return by_domain, id_status


def _load_results_jsons(output_root: Path):
    """Return instance_id -> {duration_seconds, total_tokens, input_tokens,
    output_tokens, query, raw_output, ...} keyed dict."""
    meta = {}
    for d in DOMAINS:
        p = _find_results_json(output_root, d)
        if not p:
            continue
        data = json.loads(p.read_text())
        if not isinstance(data, list):
            continue
        for r in data:
            iid = r.get("task_id") or r.get("instance_id")
            if iid:
                meta[iid] = r
    return meta


def _fmt_num(v, nd=2):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    return f"{v:,.{nd}f}"


def _fmt_int(v):
    if v is None:
        return "–"
    return f"{int(v):,}"


def status_of(iid, by_domain):
    for d, e in by_domain.items():
        if iid in e.get("resolved_ids", set()): return "RESOLVED"
        if iid in e.get("unresolved_ids", set()): return "UNRESOLVED"
        if iid in e.get("empty_patch_ids", set()): return "EMPTY_PATCH"
        if iid in e.get("error_ids", set()): return "ERROR"
        if iid in e.get("infra_ids", set()): return "INFRA_FAILURE"
        if iid in e.get("ambiguous_ids", set()): return "AMBIGUOUS"
    return "UNKNOWN"


def domain_of(iid):
    prefix = iid.split("__", 1)[0]
    if prefix == "scikit":
        return "scikit-learn"
    return prefix


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output-root", required=True,
                    help="Root that contains chatdev_<domain>/ subdirs (e.g. "
                         "output_run_20260822)")
    ap.add_argument("--report-dir", required=True,
                    help="Directory passed to run_swebench_docker_eval.py --report-dir")
    ap.add_argument("--out-md", required=True, help="Output markdown path")
    ap.add_argument("--run-tag", default="run_20260822",
                    help="Tag used in report title & 实验配置 section")
    ap.add_argument("--model", default="Deepseek-V4-Flash-0731")
    ap.add_argument("--benchmark", default="SWE-bench Verified")
    ap.add_argument("--prev-report-glob", default=None,
                    help="Optional: glob to previous run's 4 domain summary jsons "
                         "(e.g. '/tmp/evomas_swebench_reports/chatdev_Deepseek-V4-Flash-0731.evomas_docker_*.json') "
                         "for delta comparison")
    ap.add_argument("--prev-results-glob", default=None,
                    help="Optional: glob to previous run's results.json for "
                         "time/token baseline comparison")
    args = ap.parse_args()

    output_root = Path(args.output_root).resolve()
    report_dir = Path(args.report_dir).resolve()
    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)

    by_domain, _ = _merge_summaries(report_dir)
    meta = _load_results_jsons(output_root)

    # totals
    total = sum(e["total"] for e in by_domain.values())
    resolved = sum(e["resolved"] for e in by_domain.values())
    unresolved = sum(e["unresolved"] for e in by_domain.values())
    empty = sum(e["empty_patch"] for e in by_domain.values())
    errors = sum(e["error"] for e in by_domain.values())
    infra = sum(e["infra"] for e in by_domain.values())
    ambiguous = sum(e["ambiguous"] for e in by_domain.values())
    pass_rate = (resolved / total * 100.0) if total else 0.0

    # -------- 1. 实验配置 --------
    lines = []
    lines.append(f"# ChatDev Baseline {args.benchmark} 全域 Docker 评估报告 ({args.run_tag})")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> Patch 生成阶段：ChatDev baseline（use_docker=true, agent 在 swebench 容器内执行），"
                 f"输出根目录：`{output_root}`")
    lines.append(f"> 评估阶段：SWE-bench 官方 docker harness（swebench 5.0.2），"
                 f"汇总报告目录：`{report_dir}`")
    lines.append("")
    lines.append("## 1. 实验配置")
    lines.append("")
    lines.append("```text")
    lines.append(f"Benchmark:        {args.benchmark}")
    lines.append(f"Selection:        scripts/selected_<域>.txt，共 {total} 条（4 域）")
    lines.append(f"Model:            {args.model}")
    lines.append(f"Team:             ChatDev baseline（mas_pools/swebench/chatdev.yaml，4 角色：CEO/CTO/Programmer/Tester）")
    lines.append(f"Agent Runtime:    MinisweagentRunner + DockerEnvironment（cwd=/testbed, instance 专属镜像）")
    lines.append(f"Evolution:        False")
    lines.append(f"Workers（生成）:  4 域并行（bash &），每域内部串行跑 case")
    lines.append(f"Workers（评估）:  4 域并行，每域 4 个 swebench docker 容器并行")
    lines.append(f"Agent timeout:    3600 秒/case（SWE-bench 自动升级）")
    lines.append(f"Eval timeout:     900 秒/case")
    lines.append(f"Evaluation:       per-task --evaluate-on-save + 全域 sweep 兜底 run_swebench_docker_eval.py")
    lines.append(f"Metric:           docker_resolved / total（swebench harness）")
    lines.append("```")
    lines.append("")

    # -------- 2. 总体得分 --------
    lines.append("## 2. 总体得分")
    lines.append("")
    lines.append("```text")
    lines.append(f"总数：     {total}")
    lines.append(f"已解决：   {resolved}")
    lines.append(f"未解决：   {unresolved}（含 {empty} empty patch + {errors} eval error + {infra} infra failure + {ambiguous} ambiguous"
                 f" / 其余 {max(unresolved-empty-errors-infra-ambiguous, 0)} 为 patch 应用但测试不通过）")
    lines.append(f"Pass Rate：{resolved}/{total} = {pass_rate:.2f}%")
    lines.append("```")
    lines.append("")
    lines.append("按域统计：")
    lines.append("")
    lines.append("| 仓库 | 总数 | 已解决 | 未解决 | Empty Patch | Error | Pass Rate |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for d in DOMAINS:
        e = by_domain[d]
        t = e["total"]
        r = e["resolved"]
        u = t - r
        emp = e["empty_patch"]
        err = e["error"]
        pr = (r / t * 100.0) if t else 0.0
        lines.append(f"| `{REPO_LABEL[d]}` | {t} | {r} | {u} | {emp} | {err} | {pr:.2f}% |")
    lines.append(f"| **合计** | **{total}** | **{resolved}** | **{total-resolved}** | **{empty}** | **{errors}** | **{pass_rate:.2f}%** |")
    lines.append("")
    lines.append("说明：")
    lines.append("- `Empty Patch`：agent 跑完但 `<task_id>.txt` 为空，swebench harness 直接跳过。")
    lines.append("- `Error`：docker harness apply 阶段 / 容器准备阶段抛错。典型原因有 predictions 格式不对、镜像未拉到。")
    lines.append("- `未解决`（UNRESOLVED）：patch apply 成功，但 FAIL_TO_PASS 的测试没全部过。说明 patch 逻辑不完全对。")
    lines.append("")

    # -------- 3. 三项核心指标 --------
    # 按域汇总 token + time
    per_domain_stats = {}
    for d in DOMAINS:
        ids = set()
        for tag in ["resolved_ids", "unresolved_ids", "empty_patch_ids", "error_ids", "infra_ids", "ambiguous_ids"]:
            ids.update(by_domain[d].get(tag, set()))
        tokens = []; times = []
        res_tok = []; res_time = []; unres_tok = []; unres_time = []
        for iid in ids:
            m = meta.get(iid, {})
            tok = m.get("total_tokens") or m.get("tokens") or None
            t = m.get("duration_seconds")
            if tok is not None:
                tokens.append(tok)
                if iid in by_domain[d].get("resolved_ids", set()): res_tok.append(tok)
                else: unres_tok.append(tok)
            if t is not None:
                times.append(t/60.0)
                if iid in by_domain[d].get("resolved_ids", set()): res_time.append(t/60.0)
                else: unres_time.append(t/60.0)
        per_domain_stats[d] = dict(
            n=len(tokens), tokens=tokens, times=times,
            res_tok=res_tok, res_time=res_time,
            unres_tok=unres_tok, unres_time=unres_time,
        )

    lines.append("## 3. 三项核心指标")
    lines.append("")
    lines.append("### 3.1 准确率（Pass Rate）")
    lines.append("")
    lines.append("| 仓库 | 已解决 | 总数 | Pass Rate |")
    lines.append("|---|---:|---:|---:|")
    for d in DOMAINS:
        e = by_domain[d]; t = e["total"]; r = e["resolved"]
        pr = (r/t*100.0) if t else 0.0
        lines.append(f"| `{REPO_LABEL[d]}` | {r} | {t} | {pr:.2f}% |")
    pr_total = (resolved/total*100.0) if total else 0.0
    lines.append(f"| **总计** | **{resolved}** | **{total}** | **{pr_total:.2f}%** |")
    lines.append("")
    lines.append("### 3.2 平均执行时间（生成阶段，results.json duration_seconds，不含 docker eval）")
    lines.append("")
    lines.append("| 仓库 | 样本数 | 平均 (min) | 中位 (min) | 最小 (min) | 最大 (min) | 累计 (h) |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    all_times = []
    for d in DOMAINS:
        s = per_domain_stats[d]
        ts = sorted(s["times"]) or [0]
        n = len(ts)
        mean = sum(ts)/n
        med = ts[n//2] if n % 2 == 1 else (ts[n//2 - 1] + ts[n//2])/2
        mn, mx = min(ts), max(ts)
        acc_h = sum(ts)/60.0
        all_times.extend(ts)
        lines.append(f"| `{REPO_LABEL[d]}` | {s['n']} | {_fmt_num(mean)} | {_fmt_num(med)} | {_fmt_num(mn)} | {_fmt_num(mx)} | {_fmt_num(acc_h)} |")
    if all_times:
        n=len(all_times); mean=sum(all_times)/n; ts=sorted(all_times); med=ts[n//2] if n%2==1 else (ts[n//2-1]+ts[n//2])/2
        lines.append(f"| **全部** | **{n}** | **{_fmt_num(mean)}** | **{_fmt_num(med)}** | **{_fmt_num(min(ts))}** | **{_fmt_num(max(ts))}** | **{_fmt_num(sum(ts)/60.0)}** |")
    lines.append("")
    lines.append("Docker eval 阶段额外耗时（apply + pytest）约 10~60 秒 / case，4 workers 下全域评估总耗时约 30~60 分钟。")
    lines.append("")
    lines.append("### 3.3 平均 Token 消耗（results.json total_tokens，保存 patch 时统计）")
    lines.append("")
    lines.append("| 仓库 | 样本数 | 平均 Token | 中位 Token | 最小 | 最大 | 累计 |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    all_toks = []
    for d in DOMAINS:
        s = per_domain_stats[d]
        toks = sorted(s["tokens"]) or [0]
        n = len(toks)
        mean = sum(toks)/n
        med = toks[n//2] if n%2==1 else (toks[n//2-1]+toks[n//2])/2
        all_toks.extend(toks)
        lines.append(f"| `{REPO_LABEL[d]}` | {s['n']} | {_fmt_int(mean)} | {_fmt_int(med)} | {_fmt_int(min(toks))} | {_fmt_int(max(toks))} | {_fmt_int(sum(toks))} |")
    if all_toks:
        n=len(all_toks); mean=sum(all_toks)/n; ts=sorted(all_toks); med=ts[n//2] if n%2==1 else (ts[n//2-1]+ts[n//2])/2
        lines.append(f"| **全部** | **{n}** | **{_fmt_int(mean)}** | **{_fmt_int(med)}** | **{_fmt_int(min(ts))}** | **{_fmt_int(max(ts))}** | **{_fmt_int(sum(ts))}** |")
    lines.append("")
    lines.append("注：`total_tokens` 只记录 results.json 里每次 agent 保存 patch 时的累计值，不等于 API 网关总 token；真实网关累计通常比这里高 1~2 个数量级。")
    lines.append("")
    lines.append("### 3.4 Resolved vs Unresolved 的时间/token 对比")
    lines.append("")
    lines.append("| 仓库 | 子集 | 样本数 | 平均时间 (min) | 平均 Token |")
    lines.append("|---|---|---:|---:|---:|")
    for d in DOMAINS:
        s = per_domain_stats[d]
        if s["res_time"]: lines.append(f"| `{REPO_LABEL[d]}` | Resolved | {len(s['res_time'])} | {_fmt_num(sum(s['res_time'])/len(s['res_time']))} | {_fmt_int(sum(s['res_tok'])/len(s['res_tok']) if s['res_tok'] else 0)} |")
        if s["unres_time"]: lines.append(f"| `{REPO_LABEL[d]}` | Unresolved | {len(s['unres_time'])} | {_fmt_num(sum(s['unres_time'])/len(s['unres_time']))} | {_fmt_int(sum(s['unres_tok'])/len(s['unres_tok']) if s['unres_tok'] else 0)} |")
    lines.append("")

    # -------- 4. 按域详情 --------
    lines.append("## 4. 已完成结果（按域）")
    lines.append("")
    for d in DOMAINS:
        e = by_domain[d]
        t = e["total"]; r = e["resolved"]
        lines.append(f"### 4.{DOMAINS.index(d)+1} {REPO_LABEL[d]}（{t} cases，{r} RESOLVED）")
        lines.append("")
        lines.append("| Instance ID | Status | Time (min) | Tokens |")
        lines.append("|---|---|---:|---:|")
        all_ids = sorted(set().union(
            *[e.get(tag, set()) for tag in
              ["resolved_ids","unresolved_ids","empty_patch_ids","error_ids","infra_ids","ambiguous_ids"]]
        ))
        # 按域的 instance_id 过滤一次，避免跨域混到
        ids = [iid for iid in all_ids if domain_of(iid) == d]
        # 排个序：resolved / unresolved / empty / error / infra 分组
        order = {"RESOLVED": 0, "UNRESOLVED": 1, "EMPTY_PATCH": 2, "ERROR": 3, "INFRA_FAILURE": 4, "AMBIGUOUS": 5, "UNKNOWN": 9}
        ids.sort(key=lambda i: (order.get(status_of(i, by_domain), 99), i))
        for iid in ids:
            st = status_of(iid, by_domain)
            m = meta.get(iid, {})
            dur = m.get("duration_seconds")
            tok = m.get("total_tokens")
            tstr = _fmt_num(dur/60.0, 1) if dur else "–"
            kstr = _fmt_int(tok) if tok else "–"
            lines.append(f"| `{iid}` | {st} | {tstr} | {kstr} |")
        lines.append("")

    # -------- 5. 典型失败 case 分析 --------
    lines.append("## 5. 典型失败 case 分析")
    lines.append("")
    # 收集每类典型（每域取 1~2 条）
    categories = [
        ("EMPTY_PATCH", "agent 没有生成任何 diff（文件为空或全是 ERROR: 开头）。常见原因：agent_config 不是 swebench 导致提交格式不匹配、容器 bash 里 git diff 未暂存新文件、超时被 kill 未保存。"),
        ("ERROR",       "swebench harness 抛错（通常是 patch apply 阶段或镜像不存在）。"),
        ("UNRESOLVED",  "patch apply 成功但 FAIL_TO_PASS 未全过。本质是逻辑错误或回归测试没补全。"),
    ]
    for stype, note in categories:
        bucket = defaultdict(list)
        for d in DOMAINS:
            tag_map = {"EMPTY_PATCH": "empty_patch_ids", "ERROR": "error_ids", "UNRESOLVED": "unresolved_ids"}
            for iid in by_domain[d].get(tag_map[stype], set()):
                bucket[d].append(iid)
        total_type = sum(len(v) for v in bucket.values())
        lines.append(f"### 5.{categories.index((stype,note))+1} {stype}（{total_type} 条）")
        lines.append("")
        lines.append(note)
        lines.append("")
        lines.append("| 域 | Instance ID | 备注 |")
        lines.append("|---|---|---|")
        for d in DOMAINS:
            for iid in bucket[d][:3]:  # 每域最多列 3 条
                m = meta.get(iid, {})
                extra = ""
                if stype == "UNRESOLVED":
                    # 看看有没有 failure_reason
                    fr = by_domain[d].get("failure_reasons", {}).get(iid)
                    if fr: extra = f" 失败原因：{str(fr)[:60]}"
                if stype == "EMPTY_PATCH":
                    if m.get("error"): extra = f" error：{str(m.get('error'))[:60]}"
                    elif (m.get("duration_seconds") or 0) < 120: extra = " 生成时间 < 2min（疑似早退）"
                lines.append(f"| {REPO_LABEL[d]} | `{iid}` |{extra} |")
        lines.append("")

    # -------- 6. 与上次实验对比（可选） --------
    if args.prev_report_glob:
        lines.append("## 6. 与上次全域实验对比")
        lines.append("")
        prev_paths = sorted(_glob.glob(args.prev_report_glob))
        prev_by_domain = {}
        for p in prev_paths:
            d_guess = None
            for cand in DOMAINS:
                if f"evomas_docker_{cand}" in p:
                    d_guess = cand; break
            if not d_guess:
                continue
            s = json.loads(Path(p).read_text())
            prev_by_domain[d_guess] = {
                "total": s.get("total_instances", 0),
                "resolved": s.get("resolved_instances", 0),
                "empty": s.get("empty_patch_instances", 0),
                "error": s.get("error_instances", 0),
            }
        if prev_by_domain:
            lines.append("| 仓库 | 上次 | 这次 | Δ（这次-上次） |")
            lines.append("|---|---|---|---|")
            prev_tot = prev_res = 0
            for d in DOMAINS:
                pc = prev_by_domain.get(d, {})
                pt, pr = pc.get("total", 0), pc.get("resolved", 0)
                ct, cr = by_domain[d].get("total", 0), by_domain[d].get("resolved", 0)
                prev_tot += pt; prev_res += pr
                delta = cr - pr
                lines.append(f"| `{REPO_LABEL[d]}` | {pr}/{pt} | {cr}/{ct} | {delta:+d} |")
            lines.append(f"| **整体** | **{prev_res}/{prev_tot} = {prev_res/prev_tot*100:.2f}%** | **{resolved}/{total} = {pass_rate:.2f}%** | **{resolved-prev_res:+d} ({pass_rate-(prev_res/prev_tot*100 if prev_tot else 0):+.2f}pp)** |")
            lines.append("")

    # -------- 7. 结论与建议 --------
    lines.append("## 7. 结论与建议")
    lines.append("")
    lines.append("### 7.1 结论")
    lines.append("")
    lines.append(f"- 本次 {args.run_tag} 四域共 {total} case，docker harness 评估 Pass Rate = **{pass_rate:.2f}%** "
                 f"（{resolved}/{total}）。")
    lines.append(f"- Empty Patch 有 {empty} 条，占 {empty/total*100:.1f}%（通常可通过统一提交格式 agent_config: swebench 救回）。")
    lines.append(f"- Harness Error 有 {errors} 条，若 > 0 可通过查看 `logs/run_evaluation/evomas_docker_<域>/<MODEL>/<instance>/run_instance.log` 快速定位。")
    lines.append("")
    lines.append("### 7.2 下一步建议")
    lines.append("")
    lines.append("1. **Empty Patch（{}/{}）**：逐个 cat 对应的 agent 输出 run.log，确认是否 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 后没有 `cat patch.txt`（即 agent_config 没配对），该类修复成本最低、收益最大。".format(empty, total))
    lines.append("2. **UNRESOLVED（{}/{}）**：挑 5 条 patch apply clean 但 FAIL_TO_PASS 差 < 3 条测试的 case，跑 `grep \"FAILED\" logs/run_evaluation/.../test_output.txt`，把具体失败的 AssertionError 拿回来做定性分析，通常能归纳出 2~3 类 bug 模式（比如边界条件未考虑、多态参数漏传、import 缺失 等）。".format(unresolved, total))
    lines.append("3. **与 Evolution 模式对比**：这一轮 baseline 拿到的 Pass Rate 可作为对照组；之后开 Meta-Model 演进（selection / mutate / crossover / memory）同 146 case 跑一轮，对比 `resolved + Δ(tokens) + Δ(time)` 三维，验证 EvoMAS 演进算子的收益。")
    lines.append("4. **Token 真实消耗采集**：results.json 的 total_tokens ≪ 网关真实消耗，建议在 LiteLLM 侧或 provider 控制台导出月度账单，再除以 146 得出真实单 case token，纳入 reward 权重 β 的校准。")
    lines.append("")

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✅ 报告已生成：{out_md}")
    print(f"   总体 resolved={resolved}/{total} ({pass_rate:.2f}%)，empty={empty}, errors={errors}")


if __name__ == "__main__":
    main()
