#!/usr/bin/env bash
# Run ChatDev baseline on SWE-bench Pro test cases (4 domains, 216 cases total)
# 2 domains at a time to avoid OOM
# Usage: bash scripts/run_swebench_pro_baseline.sh

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
EVAL_ON_SAVE="--evaluate-on-save"
OUTPUT_ROOT="output_pro"
LOG_DIR="logs/run_pro_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$LOG_DIR"

echo "======================================================================"
echo "# SWE-bench Pro Baseline Experiment (2-domains parallel)"
echo "# Dataset: $DATASET"
echo "# Model: $MODEL"
echo "# Domains: ansible(63) flipt(54) openlibrary(60) webclients(39) = 216"
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

    conda run -n evomas --no-capture-output python -u main.py \
        --dataset "$DATASET" \
        --baseline "$BASELINE" \
        --task-ids-file "scripts/pro_test_${domain}.txt" \
        --llm-as-judge "$JUDGE" \
        $EVAL_ON_SAVE \
        --output-dir "$output_dir" \
        2>&1 | tee "$log_file"

    echo "# Completed: $domain at $(date)"
}

export -f run_domain
export BASELINE DATASET MODEL JUDGE EVAL_ON_SAVE OUTPUT_ROOT LOG_DIR

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
echo "======================================================================"
