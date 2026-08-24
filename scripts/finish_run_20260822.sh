#!/usr/bin/env bash
# 用户回来执行这一个脚本就能：
#  1) 如果生成阶段已完成但评估还没结束：继续跑 swebench docker harness（--skip-existing 续跑）
#  2) 生成 markdown 报告（带上次数据的 delta 对比）
#
# 用法（明天直接）：
#   bash scripts/finish_run_20260822.sh
#
# 如果实验还在跑就直接 exit，避免打断。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

TS=20260822
OUT_ROOT="$REPO_ROOT/output_run_${TS}"
REP_DIR="/tmp/evomas_swebench_reports_${TS}"
LOG_DIR="/tmp/evomas_run_${TS}"
DOMAINS=(django sympy matplotlib scikit-learn)
MODEL_FILE_PATH="chatdev_Deepseek-V4-Flash-0731"

source ~/miniconda/etc/profile.d/conda.sh
conda activate evomas
set -a; source "$REPO_ROOT/.env"; set +a
export MSWEA_COST_TRACKING=ignore_errors

echo "[$(date)] ============ finish_run_${TS} =========="

# ---- 1) 判断生成阶段是否仍在跑 ----
ALIVE=0
for p in $(pgrep -f "output_run_${TS}/chatdev" 2>/dev/null || true); do
  if ps -p "$p" -o cmd= 2>/dev/null | grep -q "output_run_${TS}"; then
    ALIVE=1; break
  fi
done
if [ "$ALIVE" = "1" ]; then
  echo "生成阶段仍在运行（检测到 python main.py 进程）。请等待所有域 patch 生成完成后再执行本脚本。"
  for d in "${DOMAINS[@]}"; do
    n=$(find "$OUT_ROOT/chatdev_${d}" -name "*.txt" -type f 2>/dev/null | wc -l)
    echo "  · chatdev_${d}: 已产出 $n / $(wc -l < scripts/selected_${d}.txt) patch"
  done
  exit 2
fi

echo "生成阶段已结束，各域产出："
for d in "${DOMAINS[@]}"; do
  n=$(find "$OUT_ROOT/chatdev_${d}" -name "*.txt" -type f 2>/dev/null | wc -l)
  t=$(wc -l < scripts/selected_${d}.txt)
  echo "  · chatdev_${d}: ${n}/${t}"
done

# ---- 2) 如果还有域未完成 swebench eval，补跑（并行 4 域） ----
PIDS=()
for d in "${DOMAINS[@]}"; do
  SUMMARY_JSON="$REP_DIR/${MODEL_FILE_PATH//\//__}.evomas_docker_${d}.json"
  if [ -f "$SUMMARY_JSON" ] && grep -q "resolved_instances" "$SUMMARY_JSON"; then
    echo "  [eval $d] 已完成 → 跳过（$SUMMARY_JSON）"
    continue
  fi
  (
    LOG="$LOG_DIR/swe_eval_${d}.log"
    echo "  [eval $d] 启动 → $LOG"
    python -u scripts/run_swebench_docker_eval.py \
      --domain "$d" \
      --output-root "$OUT_ROOT/chatdev_${d}" \
      --max-workers 4 --timeout 900 \
      --report-dir "$REP_DIR" --skip-existing \
      >> "$LOG" 2>&1
  ) &
  PIDS+=($!)
done
for pid in "${PIDS[@]}"; do wait "$pid" || true; done

# ---- 3) 生成 markdown 报告（带上次对比） ----
OUT_MD="$REPO_ROOT/output_paper/ChatDev Baseline SWE-bench Verified 全域Docker评估报告.run_${TS}.md"
python scripts/generate_docker_eval_report.py \
  --output-root "$OUT_ROOT" \
  --report-dir "$REP_DIR" \
  --run-tag "run_${TS}" \
  --prev-report-glob "/tmp/evomas_swebench_reports/${MODEL_FILE_PATH//\//__}.evomas_docker_*.json" \
  --prev-results-glob "$REPO_ROOT/output/chatdev_*/output_selected/swe_bench_verified/*/results.json" \
  --out-md "$OUT_MD"

echo ""
echo "🎉 全部完成："
echo "   · 报告：$OUT_MD"
echo "   · swebench 汇总：$REP_DIR/${MODEL_FILE_PATH//\//__}.evomas_docker_<域>.json"
echo "   · 单 case 详情：$REPO_ROOT/logs/run_evaluation/evomas_docker_<域>/${MODEL_FILE_PATH}/<instance_id>/"
