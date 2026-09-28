#!/usr/bin/env bash
# Re-run SWE-bench Pro with container network isolation (--network none)
# Prevents agent from searching the web for answers
# Usage: nohup bash scripts/run_pro_network_isolated.sh > logs/run_pro_netisol.log 2>&1 &

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

source ~/miniconda/etc/profile.d/conda.sh

# Config
BASELINE="chatdev"
DATASET="swe_bench_pro"
MODEL="openai:Deepseek-V4-Flash-0731"
JUDGE="none"
OUTPUT_ROOT="output_pro_netisol"
LOG_DIR="logs/run_pro_netisol_$(date +%Y%m%d_%H%M%S)"
PYTHON="/home/xhgong/miniconda/envs/evomas/bin/python"
TASK_TIMEOUT="--task-timeout 7200"

mkdir -p "$LOG_DIR"

echo "======================================================================"
echo "# SWE-bench Pro Network-Isolated Re-run (--network none)"
echo "# Dataset: $DATASET (216 cases: ansible 63 + flipt 54 + openlibrary 60 + webclients 39)"
echo "# Model: $MODEL"
echo "# Container: --network none (agent shell commands have no internet)"
echo "# Task timeout: 7200s/case"
echo "# Output: $OUTPUT_ROOT"
echo "# Log: $LOG_DIR"
echo "======================================================================"

run_domain() {
    local domain=$1
    local output_dir="${OUTPUT_ROOT}/chatdev_${domain}"
    local log_file="${LOG_DIR}/${domain}.log"

    mkdir -p "$output_dir"

    local count=$(wc -l < "scripts/pro_test_${domain}.txt")

    echo ""
    echo "----------------------------------------------------------------------"
    echo "# Starting: $domain ($count cases) at $(date)"
    echo "# Output: $output_dir"
    echo "# Log: $log_file"
    echo "----------------------------------------------------------------------"

    $PYTHON -u main.py \
        --dataset "$DATASET" \
        --baseline "$BASELINE" \
        --task-ids-file "scripts/pro_test_${domain}.txt" \
        --llm-as-judge "$JUDGE" \
        $TASK_TIMEOUT \
        --output-dir "$output_dir" \
        2>&1 | tee "$log_file"

    echo "# Completed: $domain at $(date)"
}

export -f run_domain
export BASELINE DATASET MODEL JUDGE OUTPUT_ROOT LOG_DIR PYTHON TASK_TIMEOUT

# Batch 1: ansible + flipt (117 cases)
echo ""
echo "=== Batch 1: ansible + flipt ==="
run_domain "ansible" &
run_domain "flipt" &
wait

# Batch 2: openlibrary + webclients (99 cases)
echo ""
echo "=== Batch 2: openlibrary + webclients ==="
run_domain "openlibrary" &
run_domain "webclients" &
wait

echo ""
echo "======================================================================"
echo "# All domains completed at $(date)"
echo "# Results in: $OUTPUT_ROOT/"
echo "# Logs in: $LOG_DIR/"
echo "# Next: run Docker eval with scripts/run_swebench_pro_eval.py"
echo "======================================================================"
