#!/usr/bin/env bash
# 新一轮 ChatDev Baseline 全量实验（Docker 模式，agent + eval 都走 Docker）
#
# 产物独立新目录，与之前的 output/chatdev_* 隔开：
#   · Patch / run.log / results.json  →  ./output_run_20260822/chatdev_<域>/
#   · swebench report（评估汇总）    →  /tmp/evomas_swebench_reports_20260822/
#   · 各域控制台 stdout/stderr      →  /tmp/evomas_run_20260822/chatdev_<域>.log
#
# 流程：
#   Step 1: 4 域并行跑 ChatDev Baseline（use_docker=true 的 MinisweagentRunner）
#           每个 case agent 跑在 swebench instance docker 镜像内生成 patch
#   Step 2: 4 域并行跑 run_swebench_docker_eval.py（swebench docker harness 评估）
#           写回 docker_* 字段到 results.json
#
# 用户离开前可直接：
#   nohup bash scripts/run_chatdev_baseline_run_20260822.sh > /tmp/evomas_run_20260822/controller.log 2>&1 &
# 再通过 tail -f /tmp/evomas_run_20260822/*.log 看进度

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ============ 变量 ============
TS=20260822
OUT_ROOT="$REPO_ROOT/output_run_${TS}"
REP_DIR="/tmp/evomas_swebench_reports_${TS}"
LOG_DIR="/tmp/evomas_run_${TS}"
DOMAINS=(django sympy matplotlib scikit-learn)

BASELINE="chatdev"
DATASET="swe_bench_verified"
JUDGE="none"
# 生成阶段用 --evaluate-on-save 直接 per-case eval；但即使关了，Step 2 也会做全域 docker eval
EVAL_ON_SAVE="--evaluate-on-save"

mkdir -p "$OUT_ROOT" "$REP_DIR" "$LOG_DIR"
for d in "${DOMAINS[@]}"; do
  mkdir -p "$OUT_ROOT/chatdev_${d}"
done

source ~/miniconda/etc/profile.d/conda.sh
conda activate evomas

set -a; source "$REPO_ROOT/.env"; set +a
export MSWEA_COST_TRACKING=ignore_errors

# ============ Step 1: 生成阶段（4 域并行） ============
echo "[$(date)] ============ Step 1 / 2: 生成阶段（patch） ==========="
echo "         输出目录: $OUT_ROOT"
echo "         控制台日志: $LOG_DIR/chatdev_<域>.log"
echo "         每个域 run.log: $OUT_ROOT/chatdev_<域>/run.log"

STEP1_PIDS=()
for d in "${DOMAINS[@]}"; do
  (
    OUTPUT_D="$OUT_ROOT/chatdev_${d}"
    LOG="$LOG_DIR/chatdev_${d}.log"
    echo "  [$d] 启动，输出: $OUTPUT_D  控制台: $LOG"
    python -u main.py \
      --dataset "$DATASET" \
      --baseline "$BASELINE" \
      --task-ids-file "scripts/selected_${d}.txt" \
      --llm-as-judge "$JUDGE" \
      $EVAL_ON_SAVE \
      --output-dir "$OUTPUT_D" \
      >> "$LOG" 2>&1
    ec=$?
    echo "  [$d] Step1 结束 exit=$ec" >> "$LOG"
    exit $ec
  ) &
  STEP1_PIDS+=($!)
done

# 等待 + 收集退出码
STEP1_OK=0
for i in "${!DOMAINS[@]}"; do
  d="${DOMAINS[$i]}"
  pid="${STEP1_PIDS[$i]}"
  if wait "$pid"; then
    echo "[$(date)] Step1 OK: $d (pid=$pid)"
  else
    echo "[$(date)] Step1 FAIL: $d exit=$? (pid=$pid)" >&2
    STEP1_OK=1
  fi
done

if [ $STEP1_OK -ne 0 ]; then
  echo "[$(date)] Step1 有域失败，仍尝试 Step2（已完成的域依然可评估）"
fi

# 给用户一份 Step1 完成 snapshot
echo "[$(date)] ============ Step1 各域完成 case 数 ===========" >&2
for d in "${DOMAINS[@]}"; do
  n=$(find "$OUT_ROOT/chatdev_${d}" -name "*.txt" -type f 2>/dev/null | wc -l)
  echo "  $d: $n patch files"
done

# ============ Step 2: 评估阶段（swebench docker harness，4 域并行） ============
echo "[$(date)] ============ Step 2 / 2: 评估阶段（docker harness） ==========="
echo "         swebench report dir: $REP_DIR"
echo "         控制台日志: $LOG_DIR/swe_eval_<域>.log"

STEP2_PIDS=()
for d in "${DOMAINS[@]}"; do
  (
    OUTPUT_D="$OUT_ROOT/chatdev_${d}"
    LOG="$LOG_DIR/swe_eval_${d}.log"
    echo "  [$d] swe_eval 启动，控制台: $LOG"
    # 注意 run_swebench_docker_eval.py 默认读 output/chatdev_<域>/...，
    # 这里用 --output-root 让它走 OUTPUT_D 目录（脚本内部拼接 output_selected/...）
    python -u scripts/run_swebench_docker_eval.py \
      --domain "$d" \
      --output-root "$OUTPUT_D" \
      --max-workers 4 \
      --timeout 900 \
      --report-dir "$REP_DIR" \
      --skip-existing \
      >> "$LOG" 2>&1
    ec=$?
    echo "  [$d] Step2 结束 exit=$ec" >> "$LOG"
    exit $ec
  ) &
  STEP2_PIDS+=($!)
done

STEP2_OK=0
for i in "${!DOMAINS[@]}"; do
  d="${DOMAINS[$i]}"
  pid="${STEP2_PIDS[$i]}"
  if wait "$pid"; then
    echo "[$(date)] Step2 OK: $d (pid=$pid)"
  else
    echo "[$(date)] Step2 FAIL: $d exit=$? (pid=$pid)" >&2
    STEP2_OK=1
  fi
done

# ============ 最后打印汇总 ============
echo ""
echo "======================================================================"
echo "  实验全部结束（Step1=$STEP1_OK  Step2=$STEP2_OK）  $(date)"
echo "======================================================================"
echo ""
echo "—— 各域 patch 数 + 评估汇总 ——"
for d in "${DOMAINS[@]}"; do
  PATCH_DIR=$(find "$OUT_ROOT/chatdev_${d}" -name "*.txt" -type f | head -n 1 | xargs dirname 2>/dev/null || true)
  n_patch=$(find "$OUT_ROOT/chatdev_${d}" -name "*.txt" -type f 2>/dev/null | wc -l)
  SUMMARY="$REP_DIR/chatdev_Deepseek-V4-Flash-0731.evomas_docker_${d}.json"
  resolved="?"
  total="?"
  if [ -f "$SUMMARY" ]; then
    resolved=$(python3 -c "import json;d=json.load(open('$SUMMARY'));print(d.get('resolved_instances','?'))" 2>/dev/null || echo "?")
    total=$(python3 -c "import json;d=json.load(open('$SUMMARY'));print(d.get('total_instances','?'))" 2>/dev/null || echo "?")
  fi
  echo "  $d:  $n_patch patches,  resolved=$resolved/$total,  summary=$SUMMARY"
done
echo ""
echo "—— 报告模板 ——"
echo "  生成 Task3 报告时：python 脚本读取上述 4 个 SUMMARY + 每个域 results.json"
echo "  → 按模板写入 output_paper/ChatDev Baseline SWE-bench Verified 全域Docker评估报告.run_20260822.md"
