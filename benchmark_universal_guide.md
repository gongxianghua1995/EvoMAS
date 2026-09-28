# Benchmark 测试与验证通用方法

2026-09-18 补充：[SWE agent 跨项目迁移指南](../MetaGPT/docs/SWE_agent_migration_playbook.md)。该文总结 EvoMAS → MetaGPT＋mini 的实际改动、Docker/补丁/协作/预算/评估踩坑、证据边界和迁移验收步骤；当前配置与全量实验入口见文末链接。

## **简介**
本文件包含用于 Benchmark 测试与验证的通用逻辑和命令模板，适配多个项目使用场景。支持常规评估和断网模式。

---

## **通用环境要求**
1. **操作系统**: Linux
2. **容器工具**: Docker
   - 推荐版本：Docker 20.10及以上
3. **数据挂载路径**:
   - 数据路径示例：`-v /path/to/dataset:/app/dataset`
   - 输出路径示例：`-v /path/output:/app/output`

---

## **运行命令**

1. **常规评估模式**
适用于标准模型评估与基准测试。
```bash
docker run \
--rm \
-v /path/to/SWE-bench:/app/dataset \
-v /path/output:/app/output \
my-eval-image:latest \
python3 src/utils/universal_bench.py \
--dataset-path /app/dataset/some_dataset.json \
--output-path /app/output \
--step-limit 250 \
--eval-report-file eval_results.json
```

2. **断网实验模式**
适用于隔离场景验证，完全禁用网络访问：
```bash
docker run \
--network none \
--rm \
-v /path/to/SWE-bench:/app/dataset \
-v /path/output:/app/output \
my-eval-image:latest \
python3 src/utils/universal_bench.py \
--dataset-path /app/dataset/some_dataset.json \
--output-path /app/output \
--step-limit 250 \
--eval-report-file isolated_eval_results.json
```

---

## **通用运行逻辑**
以下为任务初始化、处理与结果收集的核心伪代码：

```python
from pathlib import Path
from multiprocessing import Pool
import json

def process_task(task):
    """处理单个任务的方法"""
    result = {"task": task, "result": f"Processed {task}"}
    return result

def run_tasks(dataset_path, output_path, step_limit=250):
    """加载任务并运行"""
    with open(dataset_path, "r") as f:
        tasks = json.load(f)

    with Pool(processes=4) as pool:
        results = pool.map(process_task, tasks)

    output_file = Path(output_path) / "results.json"
    with open(output_file, "w") as f:
        json.dump(results, f, indent=2)

# main 函数示例
def main():
    import argparse

    parser = argparse.ArgumentParser(description="通用 Benchmark 测试处理器")
    parser.add_argument("--dataset-path", required=True, help="测试数据集路径")
    parser.add_argument("--output-path", required=True, help="结果输出路径")
    parser.add_argument("--step-limit", type=int, default=250, help="任务最大Step限制")
    parser.add_argument("--eval-report-file", default="results.json", help="评估报告文件名")

    args = parser.parse_args()

    run_tasks(args.dataset_path, args.output_path, step_limit=args.step_limit)

if __name__ == "__main__":
    main()
```

---

## **镜像构建示例**
确保 `src/utils/universal_bench.py` 存在，并定义了上述逻辑。

```dockerfile
FROM python:3.10-slim

WORKDIR /app

COPY src/ /app/src/
COPY requirements.txt /app/

RUN pip install --no-cache-dir -r requirements.txt

CMD ["python3", "src/utils/universal_bench.py"]
```

---

## **SWE-bench 适配经验总结**

### **1. 数据集下载与转换**

**关键点：**
- 使用 Hugging Face 源下载 SWE-bench 数据集
- 支持变体：SWE-bench、SWE-bench Lite、SWE-bench Verified

**核心步骤：**
```python
# 配置映射
configs = {
    "swe-bench": {
        "hf_name": "princeton-nlp/SWE-bench",
        "local_name": "swe_bench"
    },
    "swe-bench-lite": {
        "hf_name": "princeton-nlp/SWE-bench_Lite",
        "local_name": "swe_bench_lite"
    },
    "swe-bench-verified": {
        "hf_name": "princeton-nlp/SWE-bench_Verified",
        "local_name": "swe_bench_verified"
    }
}
```

**MAS 格式转换：**
```python
# SWE-bench 原始字段
# - instance_id: 唯一标识符
# - repo: 仓库名
# - base_commit: 基础提交哈希
# - patch: 修复代码
# - problem_statement: 问题描述
# - FAIL_TO_PASS: 失败测试用例

# MAS 格式要求
{
    'id': instance_id,  # 必须使用原始 instance_id
    'query': "Repository: {repo}\n\n{problem_statement}",
    'gt': patch,  # 使用 patch 作为答案
    'metadata': {
        'repo': repo,
        'base_commit': base_commit,
        'FAIL_TO_PASS': [...]
    }
}
```

### **2. 仓库管理（Repo Lock）**

**问题：** 多任务并发访问同一仓库会导致冲突

**解决方案：** 使用 `fcntl` 文件锁

```python
class RepoLock:
    """基于文件的仓库访问锁，防止并发修改"""
    def __init__(self, repo_path: str):
        self.repo_path = repo_path
        safe_name = repo_path.replace('/', '_')
        self.lock_file = f"/tmp/evomas_repo_lock_{safe_name}.lock"

    def acquire(self, timeout: float = 300.0) -> bool:
        """获取锁"""
        self._lock_fd = open(self.lock_file, 'w')
        while True:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except IOError:
                if time.time() - start_time >= timeout:
                    return False
                time.sleep(0.5)
```

**并行任务分配策略：** 按仓库分组，同一仓库的任务分配给同一 Worker

```python
# 按仓库分组
repo_task_groups = {}
for task in tasks:
    repo_name = task.metadata.get('repo', '').split('/')[-1]
    if repo_name not in repo_task_groups:
        repo_task_groups[repo_name] = []
    repo_task_groups[repo_name].append(task)

# 轮询分配给 Worker
worker_repo_assignments = [[] for _ in range(workers)]
for i, repo_name in enumerate(repo_task_groups.keys()):
    worker_repo_assignments[i % workers].append(repo_name)
```

### **3. Git 状态管理**

**关键操作：** 每个任务前重置仓库到 base_commit

```python
def _checkout_base_commit(self, repo_path: str, base_commit: str) -> bool:
    """检出到基础提交，确保仓库在修复前的状态"""
    # 1. 移除可能的锁文件
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path)
    subprocess.run(["git", "clean", "-fdx"], cwd=repo_path)

    # 2. 如果本地没有则 fetch
    if subprocess.run(["git", "cat-file", "-t", base_commit]).returncode != 0:
        subprocess.run(["git", "fetch", "origin", base_commit], cwd=repo_path)

    # 3. 检出
    subprocess.run(["git", "checkout", base_commit], cwd=repo_path, check=True)

    # 4. 验证
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True)
    return result.stdout.strip().startswith(base_commit[:8])
```

**任务后清理：**
```python
def _recover_repository(self, repo_path: str):
    """任务完成后恢复仓库到干净状态"""
    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=repo_path)
    subprocess.run(["git", "clean", "-fd"], cwd=repo_path)
    # 移除锁文件
    for lock in ["index.lock", "HEAD.lock", "config.lock"]:
        lock_file = Path(repo_path) / ".git" / lock
        if lock_file.exists():
            lock_file.unlink()
```

### **4. Docker Harness 评估**

**使用官方 swebench 包进行评估：**

```python
import swebench

# 构造 predictions
predictions = [{
    "instance_id": task_id,
    "model_name_or_path": "openai/Deepseek-V4-Flash-0731",
    "model_patch": patch_content,
}]

# 运行评估
swebench.run_evaluation(
    dataset_name="SWE-bench/SWE-bench_Verified",
    split="test",
    instance_ids=[task_id],
    predictions_path=predictions_file,
    max_workers=4,
    timeout=900,
    report_dir="./logs/swebench_reports",
)
```

**批量评估多域名并行：**
```bash
for d in django sympy matplotlib scikit-learn sphinx; do
    nohup python scripts/run_swebench_docker_eval.py --domain $d \
        --max-workers 4 > logs/swebench_docker_$d.log 2>&1 &
done
```

### **5. 评估结果解析**

**从 summary 报告读取结果：**
```python
summary_path = Path(report_dir) / f"{model_name.replace('/', '__')}.{run_id}.json"
summary = json.loads(summary_path.read_text())

resolved_ids = set(summary.get("resolved_ids", []))
unresolved_ids = set(summary.get("unresolved_ids", []))
empty_patch_ids = set(summary.get("empty_patch_ids", []))
error_ids = set(summary.get("error_ids", []))

if task_id in resolved_ids:
    resolved = "FULL"
elif task_id in empty_patch_ids:
    resolved = "EMPTY"
elif task_id in error_ids:
    resolved = "ERROR"
else:
    resolved = "NO"
```

### **6. SWE-bench 评估脚本示例**

**本地评估脚本 (`src/dataset/swe_evaluator.py`)：**
```bash
# 前台运行
python -m src.dataset.swe_evaluator --dataset swe_bench_verified

# 后台运行
nohup python src/dataset/swe_evaluator.py --dataset swe_bench_verified \
    --workers 4 > eval_swe_bench_verified.log 2>&1 &
```

**Docker 评估脚本 (`scripts/run_swebench_docker_eval.py`)：**
```bash
# 单域名
python scripts/run_swebench_docker_eval.py --domain sympy

# 多域名并行
for d in django sympy matplotlib scikit-learn; do
    python scripts/run_swebench_docker_eval.py --domain $d --max-workers 4 &
done
```

### **7. 常见问题与解决**

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 并发访问冲突 | 同一仓库被多任务同时修改 | 使用 RepoLock，按仓库分组分配 Worker |
| Git 状态不一致 | 任务崩溃未清理 | finally 块中执行 git reset + clean |
| Patch 格式错误 | 模型输出被截断 | 使用 LLM 补全截断的 diff |
| 评估超时 | 仓库不存在或 base_commit 错误 | 提前检查仓库路径，验证 commit |
| 锁文件残留 | 进程异常退出 | 清理脚本删除 `/tmp/evomas_repo_lock_*.lock` |

### **8. 性能优化建议**

1. **任务超时设置：** SWE-bench 任务默认 60 分钟超时
   ```python
   if 'swe' in dataset_name.lower():
       self.task_timeout = 3600.0  # 60 分钟
   ```

2. **缓存机制：** 跳过已完成的输出
   ```python
   if self._is_task_cached(task.id):
       return  # 跳过已缓存任务
   ```

3. **工件清理：** 任务完成后清理 SWE-agent 产生的文件
   ```python
   def _cleanup_sweagent_artifacts(self):
       """清理 home 目录和 /tmp 中的测试文件"""
       patterns = [
           "reproduce_issue.py",
           "test_fix.py",
           "*.patch",
           "/tmp/sweagent_*",
       ]
   ```

---

## **容器交互经验总结**

### **1. Docker 环境配置**

**SWE-agent 支持两种部署模式：**
- `use_docker=True`: 使用 Docker 部署（生产模式）
- `use_docker=False`: 本地部署（无需 Docker）

**Docker 部署配置：**
```python
from swerex.deployment.config import DockerDeploymentConfig, get_deployment

deployment_config = DockerDeploymentConfig(
    image="python:3.11",      # 基础镜像
    python_standalone_dir="/root"  # Python 安装目录
)
deployment = get_deployment(deployment_config)
```

**本地部署配置：**
```python
from swerex.deployment.local import LocalDeploymentConfig

deployment_config = LocalDeploymentConfig()
deployment = get_deployment(deployment_config)
```

### **2. 模型 ID 转换**

**EvoMAS 格式 → litellm 格式转换：**

```python
def _convert_model_id(model_id: str) -> str:
    """将 EvoMAS 模型 ID 转换为 litellm 兼容格式"""
    if ':' not in model_id:
        return model_id

    provider, model_name = model_id.split(':', 1)
    provider = provider.lower()

    if provider == 'openai':
        return model_name  # litellm 自动检测 OpenAI 模型
    elif provider == 'bedrock':
        return f"bedrock/{model_name}"
    elif provider == 'anthropic':
        return f"anthropic/{model_name}"
    elif provider == 'azure':
        return f"azure/{model_name}"
    else:
        return f"{provider}/{model_name}"
```

**Bedrock 模型降级处理：**
```python
def _resolve_model_id(model_id: str) -> str:
    """当 boto3 不可用时，将 bedrock: 模型降级到 EVO_MAS_FALLBACK_MODEL"""
    if not model_id.startswith("bedrock:"):
        return model_id
    try:
        import boto3
        return model_id
    except ImportError:
        fb = os.environ.get("EVO_MAS_FALLBACK_MODEL")
        if fb:
            return fb
        return model_id
```

### **3. 环境初始化与清理**

**容器启动后执行命令：**
```python
post_startup_commands = [
    f"cd {str(repo_path)}",           # 进入仓库目录
    "git reset --hard HEAD",          # 重置工作区
    "git clean -fd",                  # 清理未跟踪文件
    "export ROOT=$(pwd -P)",          # 设置 ROOT 环境变量
    # 写入状态文件（用于 SWE-agent 内部状态管理）
    'echo \'{"working_dir": "\'$(pwd -P)\'"}\' > /root/state.json',
]

env = SWEEnv(
    deployment=deployment,
    repo=repo_config,
    post_startup_commands=post_startup_commands,
)
```

**关键清理操作（防止状态污染）：**
```python
def _cleanup_swe_agent_state(self):
    """清理 SWE-agent 状态文件，防止新旧任务数据混淆"""
    state_files = [
        Path("/root/model.patch"),    # 上次提交的 git diff
        Path("/root/state.json"),    # 工作目录状态
    ]
    for state_file in state_files:
        if state_file.exists():
            state_file.unlink()

def _cleanup_tools_dir(self):
    """清理 /root/tools 目录，避免文件存在错误"""
    tools_dir = Path("/root/tools")
    if tools_dir.exists():
        for item in tools_dir.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
```

### **4. 仓库挂载与路径解析**

**容器内仓库路径解析：**
```python
def _extract_repo_path(self, task: str) -> Optional[Path]:
    """从任务中提取仓库路径"""
    # 方式1: 绝对路径 "Repository: /path/to/repo"
    match = re.search(r'Repository:\s*(/[^\s\n]+)', task)
    if match:
        return Path(match.group(1))

    # 方式2: "Repository: owner/repo" -> dataset/repos/<repo>
    match = re.search(r'Repository:\s*([^\s/]+/([^\s/]+))', task)
    if match:
        repo_name = match.group(2)
        return Path("dataset/repos") / repo_name

    # 方式3: "directory /path/to/repo" 模式
    match = re.search(r'directory\s+(/[^\s\n\.]+)', task)
    if match and path.exists():
        return path

    return None
```

**Docker 挂载配置示例：**
```bash
docker run \
  -v /path/to/repos:/app/repos \    # 仓库目录
  -v /path/to/dataset:/app/dataset \ # 数据集
  -v /path/to/output:/app/output \   # 输出目录
  -v /path/to/.env:/app/.env \      # 环境变量
  --gpus all \                       # GPU 支持（如需要）
  my-eval-image:latest
```

### **5. 工具配置**

**SWE-agent 工具配置：**
```python
from sweagent.tools.tools import ToolConfig, ToolHandler

tool_config = ToolConfig(
    execution_timeout=120,                    # 执行超时（秒）
    max_consecutive_execution_timeouts=5,     # 最大连续超时次数
)
tools = ToolHandler.from_config(tool_config)
```

**本地模式 vs Docker 模式差异：**
```python
if not self.use_docker:
    tools_config['execution_timeout'] = 120  # 本地模式缩短超时
    tools_config['max_consecutive_execution_timeouts'] = 5
```

### **6. Patch 提取与验证**

**从 git diff 提取 patch：**
```python
def _get_git_diff(self, repo_path: Path) -> str:
    """获取仓库的 git diff，排除 SWE-agent 输出文件"""
    exclude_paths = [
        ":(exclude).sweagent_output",
        ":(exclude)*.traj",
        ":(exclude)*_helpers",
    ]
    result = subprocess.run(
        ["git", "diff", "HEAD", "--", "."] + exclude_paths,
        cwd=str(repo_path),
        capture_output=True, text=True, timeout=30
    )
    return result.stdout

def _filter_sweagent_artifacts(self, diff_content: str) -> str:
    """过滤掉 SWE-agent 工件，只保留源代码变更"""
    exclude_patterns = ['.sweagent_output/', '.traj', '_helpers']
    # 过滤逻辑...
    return filtered_diff
```

**截断 Diff 补全：**
```python
def _complete_truncated_diff(self, diff_text: str) -> str:
    """使用 LLM 补全截断的 diff"""
    # 1. 解析 hunk header，检测是否截断
    # 2. 计算缺失行数
    # 3. 调用 LLM 补全
    # 4. 返回完整 diff
```

### **7. 常见容器交互问题**

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 模型 ID 不兼容 | litellm 格式不匹配 | 使用 `_convert_model_id()` 转换 |
| 状态文件污染 | 未清理旧任务状态 | 每次任务前调用 `_cleanup_swe_agent_state()` |
| 工具目录冲突 | 文件已存在 | 任务前清理 `/root/tools` 目录 |
| 工作目录错误 | 容器内路径不一致 | 通过 `post_startup_commands` 写入 `state.json` |
| GPU 不可用 | 未挂载 nvidia 驱动 | 添加 `--gpus all` 参数 |
| 仓库访问失败 | 权限问题 | 检查容器用户权限或使用 root |
| Patch 提取失败 | git 状态不一致 | 执行 `git reset --hard HEAD && git clean -fd` |

### **8. 容器交互命令速查**

```bash
# 查看运行中的容器
docker ps

# 进入容器调试
docker exec -it <container_id> /bin/bash

# 查看容器日志
docker logs -f <container_id>

# 复制文件到容器
docker cp local_file.txt <container_id>:/app/

# 从容器复制文件
docker cp <container_id>:/app/output.txt local_output.txt

# 停止并删除容器
docker stop <container_id> && docker rm <container_id>

# 查看容器资源使用
docker stats <container_id>

# 清理未使用的镜像
docker image prune -f
```

### **9. SWE-bench 镜像检查与拉取**

**镜像命名格式：**
```
swebench/sweb.eval.x86_64.{family}_{version}_{case_suffix}:latest
```
- `family`: 仓库家族名（如 `django`, `sympy`, `sphinx-doc`, `matplotlib`）
  - **注意**：JSON 中的 family 名称可能与镜像不同（如 JSON 用 `sphinx`，镜像用 `sphinx-doc`）
- `version`: 版本号（如 1776）
- `case_suffix`: 实例 ID 后缀（如 django-10999，从 `django__django-10999` 提取）

**镜像检查脚本：**
```python
import json
import subprocess

# JSON family 名称到镜像 family 名称的映射
FAMILY_MAPPING = {
    'sphinx': 'sphinx-doc',  # JSON 用 sphinx，镜像用 sphinx-doc
}

def check_swebench_images(split_file: str, version: str = '1776') -> dict:
    """
    检查 SWE-bench 数据集对应的镜像是否本地存在

    Args:
        split_file: 数据集分割文件路径（如 verified_new_split.json）
        version: 镜像版本号

    Returns:
        包含检查结果的字典
    """
    with open(split_file) as f:
        data = json.load(f)

    # 获取本地已有的镜像列表
    result = subprocess.run(
        ['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}'],
        capture_output=True, text=True
    )
    local_images = set(result.stdout.strip().split('\n'))

    # 收集所有 test cases
    test_cases_by_family = {}
    for family, categories in data['families'].items():
        test_cases = []
        for case in categories.get('test', []):
            test_cases.append(case)
        for case in categories.get('smoke', []):
            test_cases.append(case)
        if test_cases:
            test_cases_by_family[family] = test_cases

    # 检查每个镜像
    results = {'exists': 0, 'missing': 0, 'by_domain': {}}
    missing_cases = []

    for family, cases in test_cases_by_family.items():
        # 使用映射获取镜像中的 family 名称
        img_family = FAMILY_MAPPING.get(family, family)
        domain_exists = 0
        domain_missing = 0
        for case in cases:
            # 提取 case 后缀: django__django-10999 -> django-10999
            case_suffix = case.split('__')[1] if '__' in case else case
            img = f'swebench/sweb.eval.x86_64.{img_family}_{version}_{case_suffix}:latest'

            if img in local_images:
                domain_exists += 1
            else:
                domain_missing += 1
                missing_cases.append((family, case_suffix))

        results['by_domain'][family] = {
            'total': len(cases),
            'exists': domain_exists,
            'missing': domain_missing
        }
        results['exists'] += domain_exists
        results['missing'] += domain_missing

    return results, missing_cases

# 使用示例
results, missing = check_swebench_images('dataset/splits/verified_new_split.json')
print(f"Existing: {results['exists']}, Missing: {results['missing']}")
```

**镜像拉取脚本：**
```python
# JSON family 名称到镜像 family 名称的映射
FAMILY_MAPPING = {
    'sphinx': 'sphinx-doc',
}

def pull_missing_images(missing_cases: list, version: str = '1776', workers: int = 4):
    """
    并行拉取缺失的镜像

    Args:
        missing_cases: 缺失镜像列表 [(family, case_suffix), ...]
        version: 镜像版本号
        workers: 并行拉取数量
    """
    from concurrent.futures import ThreadPoolExecutor

    def pull_image(family, case_suffix):
        img_family = FAMILY_MAPPING.get(family, family)
        img = f'swebench/sweb.eval.x86_64.{img_family}_{version}_{case_suffix}:latest'
        result = subprocess.run(
            ['docker', 'pull', img],
            capture_output=True, text=True
        )
        return (family, case_suffix, result.returncode == 0)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(pull_image, f, c) for f, c in missing_cases]
        for future in futures:
            family, case, success = future.result()
            status = "✓" if success else "✗"
            print(f"{status} {family}/{case}")

# 使用示例
results, missing = check_swebench_images('dataset/splits/verified_new_split.json')
if missing:
    print(f"Pulling {len(missing)} missing images...")
    pull_missing_images(missing)
```

**命令行一键检查与拉取：**
```bash
# 检查并拉取（需要修改 family 名称映射）
python3 -c "
import json, subprocess

# JSON family 到镜像 family 的映射
FAMILY_MAP = {'sphinx': 'sphinx-doc'}

with open('dataset/splits/verified_new_split.json') as f:
    data = json.load(f)

# 获取本地镜像
result = subprocess.run(['docker', 'images', '--format', '{{.Repository}}:{{.Tag}}'],
                      capture_output=True, text=True)
local = set(result.stdout.strip().split('\n'))

version = '1776'
for family, cats in data['families'].items():
    img_family = FAMILY_MAP.get(family, family)
    for cat in ['test', 'smoke']:
        for case in cats.get(cat, []):
            suffix = case.split('__')[1]
            img = f'swebench/sweb.eval.x86_64.{img_family}_{version}_{suffix}:latest'
            if img not in local:
                print(f'Pulling {img}...')
                subprocess.run(['docker', 'pull', img])
"
```

**常见镜像版本对应关系：**
| 版本 | 说明 |
|------|------|
| 1776 | SWE-bench 原始版本 |
| 1780, 1781 | 后续更新版本 |

---

如需更多调整，请进一步提供需求。
