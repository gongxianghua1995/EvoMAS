#!/usr/bin/env bash
# Run ChatDev baseline experiments on 4 domains (146 selected cases total)
# Usage: bash scripts/run_chatdev_baseline.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Setup
git pull
source ~/miniconda/etc/profile.d/conda.sh

# Config
BASELINE="chatdev"
DATASET="swe_bench_verified"
MODEL="openai:Deepseek-V4-Flash-0731"
JUDGE="none"
EVAL_ON_SAVE="--evaluate-on-save"

# Output log
LOG_DIR="./tmp"
LOG_FILE="$LOG_DIR/baseline.log"
mkdir -p "$LOG_DIR"

echo "======================================================================"
echo "# ChatDev Baseline Experiment"
echo "# Dataset: $DATASET"
echo "# Model: $MODEL"
echo "# Judge: $JUDGE"
echo "# Log: $LOG_FILE"
echo "======================================================================"

run_domain() {
    local domain=$1
    local output_dir="output/chatdev_${domain}"

    mkdir -p "$output_dir"

    local count=$(wc -l < "scripts/selected_${domain}.txt")

    echo ""
    echo "----------------------------------------------------------------------"
    echo "# Starting: $domain ($count cases)"
    echo "# Output: $output_dir"
    echo "----------------------------------------------------------------------"

    conda run -n evomas --no-capture-output python -u main.py \
        --dataset "$DATASET" \
        --baseline "$BASELINE" \
        --task-ids-file "scripts/selected_${domain}.txt" \
        --llm-as-judge "$JUDGE" \
        $EVAL_ON_SAVE \
        --output-dir "$output_dir" \
        2>&1 | tee -a "$LOG_FILE"

    echo "# Completed: $domain at $(date)" | tee -a "$LOG_FILE"
}

export -f run_domain

# Clear log and run in background
> "$LOG_FILE"

run_domain "django" &
run_domain "sympy" &
run_domain "matplotlib" &
run_domain "scikit-learn" &

wait

echo ""
echo "======================================================================"
echo "# All domains completed at $(date)"
echo "======================================================================" | tee -a "$LOG_FILE"
