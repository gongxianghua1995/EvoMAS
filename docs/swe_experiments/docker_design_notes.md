# 把生成 patch 流程也放进 Docker — 基于原有流程调整方案

## 结论

你说得对。**原始 minisweagent 包本来就支持 docker agent 执行**，我们 ChatDev baseline 现在只是把环境硬编码成 `LocalEnvironment`，切换到 `DockerEnvironment` 就能让生成阶段也跑在容器里。**不需要从头设计任何新东西，只改 1 个 import + 1 处实例化 + 1 处镜像名解析。**

---

## 1. 现状（生成在 host conda，eval 在 docker）

```
┌─── HOST conda: evomas ───────────────────────────────────────────┐
│                                                                  │
│  MasRunner.run()                                                 │
│      └─ MinisweagentRunner.create_agent()                        │
│          ├─ Line 231: from minisweagent.environments.local         │
│          │              import LocalEnvironment                  │
│          ├─ Line 478-482: local_env_config = {cwd: host repo_path}│
│          │              env = LocalEnvironment(**local_env_config)│
│          └─ DefaultAgent(model, env=env, ...)                    │
│                                                                  │
│      agent 在 host 的 dataset/repos/<repo> 里跑 bash 命令         │
│      → 写入 output/<task_id>.txt                                 │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
                          ↓
┌─── DOCKER (swebench 5.0.2) ──────────────────────────────────────┐
│  scripts/run_swebench_docker_eval.py                             │
│      └─ swebench.run_evaluation(...)                             │
│          └─ run_instance()                                        │
│              ├─ container from sweb.eval.x86_64.<repo>_<issue>   │
│              ├─ git apply patch.diff                              │
│              └─ /bin/bash /eval.sh                                │
└──────────────────────────────────────────────────────────────────┘
```

**关键发现**：
- `minisweagent.environments.docker.DockerEnvironment` 类**已经存在**（[/home/xhgong/miniconda/envs/evomas/lib/python3.11/site-packages/minisweagent/environments/docker.py:45](file:///home/xhgong/miniconda/envs/evomas/lib/python3.11/site-packages/minisweagent/environments/docker.py#L45)），接受 `image: str` 参数，自动 `docker run -d --rm` 启动容器，所有 bash 命令通过 `docker exec` 在容器内执行。
- `minisweagent.run.benchmarks.swebench.get_sb_environment()` 函数**已经存在**（[/home/xhgong/miniconda/envs/evomas/lib/python3.11/site-packages/minisweagent/run/benchmarks/swebench.py:79](file:///home/xhgong/miniconda/envs/evomas/lib/python3.11/site-packages/minisweagent/run/benchmarks/swebench.py#L79)），自动按 `instance["instance_id"]` 解析出 `swebench/sweb.eval.x86_64.<repo>_<issue>` 镜像名并启动 DockerEnvironment。
- 我们的 152 个 sweb.eval 镜像已经 pull 下来了，image name 命名规则和 minisweagent 完全一致（`__` → `_1776_`）。

## 2. 改成「生成也在 docker」的最小改动

### 改动点：3 处，都在 `src/agents/runners/minisweagent.py`

#### 改动 1：import 加上 DockerEnvironment（L231）

```python
# 现状
from minisweagent.environments.local import LocalEnvironment

# 改成
from minisweagent.environments.local import LocalEnvironment
from minisweagent.environments.docker import DockerEnvironment, DockerEnvironmentConfig
```

#### 改动 2：实例化环境时按 `use_docker` flag 切换（L476-482）

```python
# 现状
local_env_config = {
    'cwd': str(working_dir),
    'timeout': self.env_config.get('timeout', 60),
    'env': self.env_config.get('env', {})
}
env = LocalEnvironment(**local_env_config)

# 改成
if self.use_docker and instance_id:
    # 复用 SWE-bench 官方镜像，agent 在容器里执行
    # 镜像名规则：swebench/sweb.eval.x86_64.<repo>_<repo>-<issue>
    # 其中双下划线 __ 替换为 _1776_（docker tag 命名限制）
    docker_iid = instance_id.replace("__", "_1776_")
    image_name = f"swebench/sweb.eval.x86_64.{docker_iid}:latest".lower()
    docker_env_config = DockerEnvironmentConfig(
        image=image_name,
        container_timeout="2h",
        env=self.env_config.get('env', {}),
    )
    env = DockerEnvironment(docker_env_config)
    logger.info(f"Using DockerEnvironment, image={image_name}")
else:
    local_env_config = {
        'cwd': str(working_dir),
        'timeout': self.env_config.get('timeout', 60),
        'env': self.env_config.get('env', {})
    }
    env = LocalEnvironment(**local_env_config)
```

#### 改动 3：`MinisweagentRunner.__init__` 加 `use_docker` 参数（L390 附近）

```python
# 现状
def __init__(self, ...):
    self.working_dir = working_dir or Path.cwd()

# 改成
def __init__(self, ..., use_docker: bool = False):
    self.working_dir = working_dir or Path.cwd()
    self.use_docker = use_docker
```

### 配套改动：2 处，让 yaml 配置 + CLI 能传 `use_docker`

#### 改动 4：[mas_pools/swebench/chatdev.yaml](file:///home/xhgong/project/EvoMAS/mas_pools/swebench/chatdev.yaml) 加一行

```yaml
execution:
  parallel_workers: false
  timeout: 900
  max_retries: 0
  use_docker: true      # ← 新增：让 ChatDev runner 走 docker 路径
```

#### 改动 5：[main.py](file:///home/xhgong/project/EvoMAS/main.py) 加 `--use-docker` CLI flag

```python
# 在 baseline 子命令的参数列表里加
@click.option("--use-docker", is_flag=True, default=False,
              help="Run agent inside per-instance SWE-bench docker container")
# 在调 interpret_mas 前传给 MasRunner
```

## 3. 调整后的完整流程（生成 + eval 同一容器）

```
┌─── DOCKER (sweb.eval.x86_64.<repo>_<issue>) ─────────────────────┐
│                                                                   │
│  MinisweagentRunner.create_agent(use_docker=True)                 │
│      ├─ image = swebench/sweb.eval.x86_64.<repo>_<issue>          │
│      ├─ DockerEnvironment(image) ← 启动容器                       │
│      └─ DefaultAgent(model, env=docker_env, ...)                 │
│                                                                   │
│  agent.run()                                                       │
│      ├─ agent 通过 docker exec 在容器里 cd /testbed              │
│      ├─ agent 调 LLM API（容器→外网，需要传 API key 进 env）      │
│      ├─ agent 在容器内 bash 命令探索代码、试运行                  │
│      └─ 完成后 git diff → patch 内容                              │
│                                                                   │
│  同一容器接着用：                                                  │
│      ├─ git apply patch.diff                                      │
│      └─ /bin/bash /eval.sh  ← 跑 SWE-bench 测试                   │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
                          ↓
                  output/<task_id>.txt
                  results.json (resolved)
```

## 4. 关键调用 / 交互点（按代码路径排序）

| # | 调用位置 | 作用 | 改动 |
|---|---------|------|------|
| 1 | [src/agents/runners/minisweagent.py:231](file:///home/xhgong/project/EvoMAS/src/agents/runners/minisweagent.py#L231) | import environment class | 加一行 `DockerEnvironment` import |
| 2 | [src/agents/runners/minisweagent.py:390](file:///home/xhgong/project/EvoMAS/src/agents/runners/minisweagent.py#L390) | `__init__` | 加 `use_docker` 参数 |
| 3 | [src/agents/runners/minisweagent.py:476-482](file:///home/xhgong/project/EvoMAS/src/agents/runners/minisweagent.py#L476) | 构造 env 对象 | 按 `use_docker` 切换 Local/Docker |
| 4 | [mas_pools/swebench/chatdev.yaml](file:///home/xhgong/project/EvoMAS/mas_pools/swebench/chatdev.yaml) execution 段 | 默认开关 | 加 `use_docker: true` |
| 5 | [main.py](file:///home/xhgong/project/EvoMAS/main.py) baseline CLI | 命令行开关 | 加 `--use-docker` flag |
| 6 | [src/utils/mas_runner.py:853](file:///home/xhgong/project/EvoMAS/src/utils/mas_runner.py#L853) `_save_task_output` | 取 patch 写 .txt | **不用改**。DockerEnvironment 的 bash 命令在容器内执行，最后通过 `git diff` 拿到 patch 字符串，写入 host 文件 |
| 7 | [scripts/run_swebench_docker_eval.py](file:///home/xhgong/project/EvoMAS/scripts/run_swebench_docker_eval.py) | 后续 docker eval | **不用改**。如果生成阶段已经在容器里 apply 了 patch，eval 阶段可以跳过 git apply 这一步，或者直接复用 swebench.run_evaluation（它会在新容器里重新 apply patch，幂等） |

## 5. API key / 网络的处理

minisweagent 的 `DockerEnvironmentConfig` 有 `env` 字段（容器内环境变量）和 `forward_env` 字段（把 host 环境变量转发到容器）：

```python
# minisweagent.environments.docker.DockerEnvironmentConfig 的字段
env: dict = {}              # 容器内设置的环境变量
forward_env: list[str] = []  # 从 host 转发的环境变量名
```

我们只需要把 API key 的环境变量名加进 `forward_env`：

```python
docker_env_config = DockerEnvironmentConfig(
    image=image_name,
    container_timeout="2h",
    env={},
    forward_env=["OPENAI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_BASE_URL"],
)
```

## 6. 生成阶段和 eval 阶段是否共用一个容器？

**默认不共用**（minisweagent 的 DockerEnvironment 和 swebench 的 run_instance 是两套独立的容器生命周期），但这不是问题：

- 生成阶段：minisweagent 启动容器 → agent 跑完 → 容器退出（`--rm` 自动清理）
- eval 阶段：swebench.run_evaluation 启动新容器 → apply patch → 跑测试 → 容器退出

两阶段用**同一个 image**（`sweb.eval.x86_64.<repo>_<issue>`），环境完全一致，只是分两次启动。这样设计的好处是：
1. 生成阶段 agent 可能修改了容器内文件状态（试错），如果直接复用做 eval，状态不干净
2. swebench 的 eval harness 有自己的 apply patch + run tests 流程，复用它的逻辑更稳

**如果一定要共用一个容器**（节省一次容器启动开销），可以在 minisweagent 的 DockerEnvironmentConfig 里设 `container_timeout="3h"` 让容器不退出，然后在 agent 跑完后紧接着调 `docker exec <container_id> /bin/bash /eval.sh`。但这需要改 `run_swebench_docker_eval.py` 接受外部传入的 container_id，改动较大。**推荐先不共用，分两阶段跑。**

## 7. 改动量估算

| 改动 | 文件 | 行数 |
|------|------|------|
| 改动 1：import DockerEnvironment | `src/agents/runners/minisweagent.py` | +2 |
| 改动 2：实例化时按 flag 切换 | `src/agents/runners/minisweagent.py` | +15 (替换 4 行 → 19 行) |
| 改动 3：`__init__` 加 `use_docker` | `src/agents/runners/minisweagent.py` | +2 |
| 改动 4：yaml 加配置 | `mas_pools/swebench/chatdev.yaml` | +1 |
| 改动 5：CLI 加 flag | `main.py` | +5 |
| **总计** | 3 个文件 | **+25 行** |

## 8. 验证计划

1. 改完后用 `scikit-learn__scikit-learn-11578` 单 case 验证：
   ```bash
   python main.py --dataset swebench_verified --baseline --model openai:Deepseek-V4-Flash-0731 \
       --task-ids scikit-learn__scikit-learn-11578 --use-docker
   ```
2. 确认：
   - DockerEnvironment 启动 sweb.eval.x86_64.scikit-learn_scikit-learn-11578 容器
   - agent 在容器内 `/testbed` 里跑 bash 命令
   - agent 完成后 `git diff` 输出 patch
   - patch 写入 `output/<task_id>.txt`
3. 然后用 `scripts/run_swebench_docker_eval.py` 做 eval（生成阶段的容器已经退出，eval 用新容器重跑）
4. 单 case OK 后再全域跑

## 9. 风险点

| 风险 | 影响 | 缓解 |
|------|------|------|
| API key 没传进容器 | agent 调 LLM 失败 | `forward_env` 显式列出所有 key 名 |
| 容器 timeout 不够 | agent 跑到一半容器退出 | `container_timeout="2h"` 给足时间 |
| agent 在容器内写文件 | 容器退出后丢失（如果没 `docker cp`） | agent 输出的 patch 是通过 `git diff` 字符串拿的，不依赖容器内文件持久化 |
| DockerEnvironment 和 LocalEnvironment 接口不一致 | `agent.run()` 调用失败 | minisweagent 包统一了 Environment 抽象基类，两者实现相同接口 |
| 152 个镜像里没有某个 instance 的镜像 | 容器启动失败 | 启动前先 `docker images` 查，缺的可以 `swebench.build_instance_image` 自动 build |

## 10. 是否需要保留 local conda evaluator 作为 fallback

**建议保留**，但不再作为主路径。如果 docker daemon 不可用或者某 instance 的镜像缺失，自动 fallback 到 LocalEnvironment（现在的代码逻辑）。`run_swebench_docker_eval.py` 也保留，作为「已有 patch 重跑 eval」的入口。
