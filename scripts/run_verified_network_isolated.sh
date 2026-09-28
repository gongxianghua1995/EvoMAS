#!/usr/bin/env bash
# Run SWE-bench Verified with container network isolation, split by domain
# Prevents agent from searching the web for answers
# Usage: bash scripts/run_verified_network_isolated.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

mkdir -p logs
mkdir -p output_verified_netisol/django
mkdir -p output_verified_netisol/matplotlib
mkdir -p output_verified_netisol/sphinx
mkdir -p output_verified_netisol/sympy

PYTHON="/home/xhgong/miniconda/envs/evomas/bin/python"

nohup $PYTHON -u main.py \
    --dataset swe_bench_verified \
    --baseline chatdev \
    --task-ids-file scripts/verified_test_django.txt \
    --llm-as-judge none \
    --task-timeout 7200 \
    --output-dir output_verified_netisol/django \
    > logs/run_verified_netisol_django.log 2>&1 &

nohup $PYTHON -u main.py \
    --dataset swe_bench_verified \
    --baseline chatdev \
    --task-ids-file scripts/verified_test_matplotlib.txt \
    --llm-as-judge none \
    --task-timeout 7200 \
    --output-dir output_verified_netisol/matplotlib \
    > logs/run_verified_netisol_matplotlib.log 2>&1 &

nohup $PYTHON -u main.py \
    --dataset swe_bench_verified \
    --baseline chatdev \
    --task-ids-file scripts/verified_test_sphinx.txt \
    --llm-as-judge none \
    --task-timeout 7200 \
    --output-dir output_verified_netisol/sphinx \
    > logs/run_verified_netisol_sphinx.log 2>&1 &

nohup $PYTHON -u main.py \
    --dataset swe_bench_verified \
    --baseline chatdev \
    --task-ids-file scripts/verified_test_sympy.txt \
    --llm-as-judge none \
    --task-timeout 7200 \
    --output-dir output_verified_netisol/sympy \
    > logs/run_verified_netisol_sympy.log 2>&1 &

echo "Started 4 domains: django matplotlib sphinx sympy"
jobs -p
