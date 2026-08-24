"""
Runner for mini-swe-agent framework.

This runner supports multiple configuration modes:
1. config_default: Full SWE-bench configuration (recommended for best performance)
2. config_simple: Minimal configuration (backward compatible)
3. Custom config: Load from specified path

Configuration is loaded from:
- config/agent_configs/ (relative to project root, or set EVOMAS_AGENT_CONFIG_DIR)
- Or original mini-swe-agent config directory (set MINISWEAGENT_CONFIG_DIR)
"""

import logging
import os
import shutil
import subprocess
import tempfile
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

from ..spec import AgentSpec, AgentResult
from .base import BaseAgentRunner

logger = logging.getLogger(__name__)

# Token counter using tiktoken (cl100k_base encoding for ChatGPT compatibility)
_tiktoken_encoder = None


def _get_tiktoken_encoder():
    """Lazy-load tiktoken encoder for token counting."""
    global _tiktoken_encoder
    if _tiktoken_encoder is None:
        try:
            import tiktoken
            _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            logger.warning("tiktoken not installed, token counting will be skipped")
            _tiktoken_encoder = None
    return _tiktoken_encoder


def _count_tokens(text: str) -> int:
    """Count tokens in text using tiktoken."""
    enc = _get_tiktoken_encoder()
    if enc is None:
        return 0
    try:
        return len(enc.encode(text, allowed_special="all"))
    except Exception:
        return 0


# Prepended to every SWE-bench problem statement to counter the "over-analysis"
# death spiral: agent keeps running `git show`/`git log -S`/`cat` on historical
# commits instead of editing the actual source files. Observed burning the full
# step_limit (50) without a single file edit on psf/requests-1724.
ACTION_FIRST_PREAMBLE = """IMPORTANT WORKFLOW GUIDANCE:
- Your job is to WRITE A PATCH that fixes the bug, not to investigate how it was fixed historically.
- Do NOT spend steps running `git log -S`, `git show <old-commit>`, or archaeology on past fixes. Those commits are from FUTURE versions and will mislead you.
- Workflow: (1) `grep`/`sed -n` to locate the relevant code, (2) READ 10-20 lines of context, (3) EDIT the file directly, (4) run the repro/test ONCE, (5) SUBMIT.
- If you have not edited any file by step 10, you are off-track — edit immediately.
- Prefer one targeted edit over multiple exploratory commands.

"""


def _resolve_model_id(model_id: str) -> str:
    """Fallback a ``bedrock:`` model_id to ``EVO_MAS_FALLBACK_MODEL`` when boto3
    is unavailable, so MAS configs whose agents still carry Bedrock model_ids
    can still execute on non-AWS / OpenAI-compatible environments.

    In AWS environments with boto3 installed this is a no-op.
    """
    if not (model_id or "").startswith("bedrock:"):
        return model_id
    try:
        import boto3  # noqa: F401
        return model_id
    except ImportError:
        fb = os.environ.get("EVO_MAS_FALLBACK_MODEL")
        if fb:
            logger.warning(
                "boto3 unavailable: falling back agent model '%s' -> '%s'",
                model_id, fb,
            )
            return fb
        return model_id


def _wrap_query_with_step_logger(model: Any, agent_id: str, work_dir: Optional[Path] = None) -> None:
    """Wrap model.query to log each step's IN/OUT to the standard logger.

    Also detects the "task is done but framework keeps demanding tool calls"
    death spiral and forcibly injects mini-swe-agent's submit command so the
    agent exits cleanly instead of burning `step_limit` LLM calls on no-ops.

    `work_dir` is used to anchor the submit command with an absolute `cd` so
    that `git diff` runs inside the repo even if the agent has cd'd elsewhere.
    """
    import re
    import time as _time

    original_query = model.query
    step_counter = [0]
    # Token accumulators attached to the model so the runner can read them
    # after agent.run() completes.
    if not hasattr(model, 'total_input_tokens'):
        model.total_input_tokens = 0
        model.total_output_tokens = 0

    def _fmt_response(response) -> str:
        if not isinstance(response, dict):
            return str(response)
        parts = []
        content = str(response.get("content") or "").strip()
        if content:
            parts.append(f"[content]\n{content}")
        reasoning = str(response.get("reasoning_content") or "").strip()
        if reasoning:
            parts.append(f"[reasoning]\n{reasoning}")
        actions = (response.get("extra") or {}).get("actions") or []
        for i, a in enumerate(actions):
            cmd = a.get("command") if isinstance(a, dict) else a
            parts.append(f"[action {i}]\n{cmd}")
        if not actions:
            for i, tc in enumerate(response.get("tool_calls") or []):
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                parts.append(f"[tool_call {i}] {fn.get('name', '?')}({fn.get('arguments', '')})")
        return "\n\n".join(parts) if parts else "<empty>"

    def logged_query(messages, **kwargs):
        step_counter[0] += 1
        step = step_counter[0]
        started = _time.time()

        response = original_query(messages, **kwargs)
        duration = _time.time() - started

        # Strip empty tool_calls (DashScope rejects them in history)
        if isinstance(response, dict) and not response.get("tool_calls"):
            response.pop("tool_calls", None)

        # Accumulate token usage from the response. litellm responses expose
        # usage as either a dict ("usage") or via the model's own counters.
        usage = None
        if isinstance(response, dict):
            usage = response.get("usage") or response.get("token_usage")
        else:
            usage = getattr(response, "usage", None) or getattr(response, "token_usage", None)
        if isinstance(usage, dict):
            model.total_input_tokens += int(usage.get("prompt_tokens", usage.get("input_tokens", 0)))
            model.total_output_tokens += int(usage.get("completion_tokens", usage.get("output_tokens", 0)))
        elif usage is not None:
            # litellm Usage object with attributes
            pt = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", 0)
            ct = getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", 0)
            model.total_input_tokens += int(pt)
            model.total_output_tokens += int(ct)

        # Fallback: if API didn't return usage (e.g. Deepseek), use tiktoken to count
        if model.total_input_tokens == 0 and model.total_output_tokens == 0:
            input_text = ""
            for msg in (messages or []):
                input_text += msg.get("content", "")
            model.total_input_tokens += _count_tokens(input_text)
            model.total_output_tokens += _count_tokens(_fmt_response(response))

        out_text = _fmt_response(response)
        if len(out_text) > 2000:
            out_text = out_text[:2000] + f"\n... [truncated, total {len(out_text)} chars]"

        last_in = ""
        for m in reversed(messages or []):
            role = m.get("role")
            if role in ("tool", "observation", "user", "system"):
                raw = str(m.get("content", ""))
                if len(raw) > 1000:
                    raw = raw[:1000] + f"\n... [truncated, total {len(raw)} chars]"
                last_in = f"[{role}]\n{raw}"
                break

        logger.info(
            f"\n{'='*20} [LLM {agent_id} step {step}] ({duration:.1f}s) {'='*20}\n"
            f"--- IN ---\n{last_in}\n"
            f"--- OUT ---\n{out_text}\n"
            f"{'='*60}"
        )
        return response

    model.query = logged_query


# Configuration paths — resolve relative to project root, overridable via env vars
EVOMAS_CONFIG_DIR = Path(os.environ.get(
    "EVOMAS_AGENT_CONFIG_DIR",
    str(Path("config/agent_configs").resolve())
))

# Resolve the mini-swe-agent config dir: prefer the project-local override,
# then fall back to the config directory shipped inside the minisweagent
# package so baseline runs work out-of-the-box without copying configs.
def _resolve_minisweagent_config_dir() -> Path:
    override = os.environ.get("MINISWEAGENT_CONFIG_DIR")
    if override:
        return Path(override)
    project_local = Path("config/minisweagent").resolve()
    if project_local.exists():
        return project_local
    try:
        import minisweagent
        return Path(minisweagent.__file__).parent / "config"
    except ImportError:
        return project_local

MINISWEAGENT_CONFIG_DIR = _resolve_minisweagent_config_dir()

# Available configurations
CONFIG_ALIASES = {
    "default": MINISWEAGENT_CONFIG_DIR / "default.yaml",
    "simple": MINISWEAGENT_CONFIG_DIR / "mini.yaml",
    "swebench": MINISWEAGENT_CONFIG_DIR / "benchmarks" / "swebench.yaml",
    "mini_default": MINISWEAGENT_CONFIG_DIR / "default.yaml",
}

# Try to import mini-swe-agent
try:
    from minisweagent.agents.default import DefaultAgent, AgentConfig
    from minisweagent.environments.local import LocalEnvironment
    from minisweagent.environments.docker import DockerEnvironment
    from minisweagent import Model
    MINISWEAGENT_AVAILABLE = True
except ImportError:
    logger.warning("mini-swe-agent not available")
    MINISWEAGENT_AVAILABLE = False
    DefaultAgent = None
    AgentConfig = None
    LocalEnvironment = None
    DockerEnvironment = None


def get_config_path(config_spec: str) -> Path:
    """
    Resolve configuration specification to actual path.

    Args:
        config_spec: Config name ("default", "simple", "swebench") or path

    Returns:
        Path to configuration file
    """
    # Check if it's an alias
    if config_spec.lower() in CONFIG_ALIASES:
        return CONFIG_ALIASES[config_spec.lower()]

    # Check if it's a direct path
    config_path = Path(config_spec)
    if config_path.exists():
        return config_path

    # Add .yaml extension if missing
    if not config_path.suffix:
        config_path = config_path.with_suffix(".yaml")

    # Check in EvoMAS config dir
    evomas_path = EVOMAS_CONFIG_DIR / config_path.name
    if evomas_path.exists():
        return evomas_path

    # Check in mini-swe-agent config dir
    mini_path = MINISWEAGENT_CONFIG_DIR / config_path.name
    if mini_path.exists():
        return mini_path

    mini_extra_path = MINISWEAGENT_CONFIG_DIR / "extra" / config_path.name
    if mini_extra_path.exists():
        return mini_extra_path

    raise FileNotFoundError(
        f"Configuration not found: {config_spec}. "
        f"Available: {list(CONFIG_ALIASES.keys())} or provide a valid path"
    )


def load_config(config_spec: str = "default") -> Dict[str, Any]:
    """
    Load agent configuration from file.

    Args:
        config_spec: Config name or path

    Returns:
        Configuration dictionary
    """
    config_path = get_config_path(config_spec)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    logger.info(f"Loaded config from {config_path}")
    return config


class EvoMASModelAdapter:
    """
    Adapter to make EvoMAS models compatible with mini-swe-agent's Model protocol.
    """

    def __init__(self, model_wrapper, config: Optional[Dict[str, Any]] = None):
        self.model_wrapper = model_wrapper
        self.config = config or {}
        self.cost = 0.0
        self.n_calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

        # Get the underlying smolagents model if available
        if hasattr(model_wrapper, 'smolagents_model'):
            self.smolagents_model = model_wrapper.smolagents_model
        else:
            self.smolagents_model = None

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        """Query the model following mini-swe-agent's protocol."""
        self.n_calls += 1

        try:
            if self.smolagents_model:
                from smolagents import ChatMessage, MessageRole

                chat_messages = []
                for msg in messages:
                    role_str = msg['role'].upper()
                    if hasattr(MessageRole, role_str):
                        role = getattr(MessageRole, role_str)
                    else:
                        role = MessageRole.USER if msg['role'] == 'user' else MessageRole.ASSISTANT

                    chat_messages.append(ChatMessage(role=role, content=msg['content']))

                response = self.smolagents_model.generate(chat_messages, **kwargs)

                if hasattr(response, 'token_usage') and response.token_usage is not None:
                    token_usage = response.token_usage
                    self.total_input_tokens += token_usage.input_tokens
                    self.total_output_tokens += token_usage.output_tokens
                    total_tokens = token_usage.input_tokens + token_usage.output_tokens
                    self.cost += total_tokens * 0.00001

                return {'content': response.content}
            else:
                prompt = "\n".join([f"{msg['role']}: {msg['content']}" for msg in messages])
                return {'content': f"[Model response to: {prompt[:100]}...]"}

        except Exception as e:
            logger.error(f"Error in model query: {e}")
            return {'content': f"Error: {str(e)}"}

    def get_template_vars(self) -> dict[str, Any]:
        return {
            'model_name': getattr(self.model_wrapper, 'model_name', 'unknown'),
            'model_id': getattr(self.model_wrapper, 'model_id', 'unknown'),
        }


class MinisweagentRunner(BaseAgentRunner):
    """
    Runner for mini-swe-agent framework with configurable settings.

    Supports:
    - config_default: Full SWE-bench configuration (250 steps, detailed prompts)
    - config_simple: Minimal configuration (20 steps, basic prompts)
    - Custom configs from path
    """

    def __init__(
        self,
        working_dir: Optional[Path] = None,
        config: str = "default",
        use_docker: bool = False,
        **kwargs
    ):
        """
        Initialize mini-swe-agent runner.

        Args:
            working_dir: Working directory for code execution
            config: Configuration name ("default", "simple", "swebench") or path
            use_docker: If True, run agent inside per-instance SWE-bench docker
                container (image: swebench/sweb.eval.x86_64.<repo>_<issue>:latest).
                Requires instance_id in context at run() time.
        """
        if not MINISWEAGENT_AVAILABLE:
            logger.warning("mini-swe-agent not available, runner will fail")

        self.working_dir = working_dir or Path.cwd()
        self.config_name = config
        self.use_docker = use_docker

        # Load configuration
        try:
            self.full_config = load_config(config)
            self.agent_config = self.full_config.get('agent', {})
            self.env_config = self.full_config.get('environment', {})
        except FileNotFoundError as e:
            logger.warning(f"Config not found, using defaults: {e}")
            self.full_config = {}
            self.agent_config = {}
            self.env_config = {}

        # Default step_limit for SWE-bench tasks (prevents infinite loops).
        # Kept low because the post-PASSED grace + framework-nudge safety net
        # should submit within ~step_at_passed + 15; anything past that is spin.
        if 'step_limit' not in self.agent_config or self.agent_config.get('step_limit') == 0:
            self.agent_config['step_limit'] = 300
        if 'cost_limit' not in self.agent_config or self.agent_config.get('cost_limit') == 0:
            self.agent_config['cost_limit'] = 10.0  # Default $10 budget

        logger.info(f"MinisweagentRunner initialized with config: {config}")
        logger.info(f"  step_limit: {self.agent_config.get('step_limit', 'default')}")
        logger.info(f"  cost_limit: {self.agent_config.get('cost_limit', 'default')}")

    def create_agent(self, spec: AgentSpec, working_dir: Path, instance_id: str = None) -> Any:
        """Create a mini-swe-agent with configured settings.

        Args:
            spec: Agent specification
            working_dir: Working directory (used as cwd for LocalEnvironment;
                ignored when use_docker=True since container has /testbed)
            instance_id: SWE-bench instance id (required when use_docker=True
                to resolve the per-instance docker image name)
        """
        if not MINISWEAGENT_AVAILABLE:
            raise ImportError("mini-swe-agent is not available")

        # Ensure API keys are loaded from EvoMAS .env
        try:
            from dotenv import load_dotenv
            import os
            evomas_root = Path(__file__).parent.parent.parent.parent
            env_file = evomas_root / '.env'
            if env_file.exists():
                load_dotenv(env_file)
                logger.debug(f"Loaded environment from {env_file}")
        except ImportError:
            logger.warning("dotenv not available, relying on existing environment")

        # Route "openai:*" model IDs to the configured OpenAI-compatible endpoint.
        import os as _os
        if "openai:" in spec.model_id:
            api_base = _os.environ.get("OPENAI_BASE_URL") or _os.environ.get("API_BASE")
            api_key = _os.environ.get("OPENAI_API_KEY") or _os.environ.get("API_KEY")
            if not api_base or not api_key:
                raise RuntimeError(
                    "openai:* model requires OPENAI_BASE_URL/OPENAI_API_KEY "
                    "(or API_BASE/API_KEY) to be set in the environment or .env"
                )
            _os.environ["LITELLM_BASE_URL"] = api_base
            _os.environ["LITELLM_API_KEY"] = api_key
            _os.environ["OPENAI_API_KEY"] = api_key
            import litellm
            litellm.api_base = api_base
            # Cost tracking fails for models not in litellm's price table
            _os.environ.setdefault("MSWEA_COST_TRACKING", "ignore_errors")

        # Use mini-swe-agent's native model loading (uses litellm directly)
        # This ensures we use the exact same model interface as the original
        from minisweagent.models import get_model as miniswe_get_model

        # Convert EvoMAS model_id format to litellm format
        # (fall back to EVO_MAS_FALLBACK_MODEL when a bedrock model_id cannot
        # be used because boto3 is missing)
        # e.g., "openai:gpt-4.1" -> "openai/gpt-4.1" (litellm needs provider prefix
        # to route to OpenAI-compatible endpoint, esp. for non-native models like
        # Deepseek served via OPENAI_BASE_URL)
        # e.g., "anthropic:claude-3-5-sonnet" -> "anthropic/claude-3-5-sonnet"
        model_id = _resolve_model_id(spec.model_id)
        if ':' in model_id:
            provider, model_name = model_id.split(':', 1)
            # litellm uses "provider/model" format
            model_id = f"{provider}/{model_name}"

        logger.info(f"Loading model with litellm: {model_id}")
        model = miniswe_get_model(model_id)

        # Log every step's IN/OUT to run.log.
        _wrap_query_with_step_logger(model, spec.id, work_dir=working_dir)

        # Create environment: docker (per-instance SWE-bench image) or local
        if self.use_docker:
            if not instance_id:
                raise RuntimeError(
                    "use_docker=True requires instance_id in context to resolve "
                    "the per-instance docker image name"
                )
            # Docker tag naming convention from swebench: __ -> _1776_
            iid_docker = instance_id.replace("__", "_1776_").lower()
            image_name = f"swebench/sweb.eval.x86_64.{iid_docker}:latest"
            logger.info(f"Using DockerEnvironment, image={image_name}")
            env = DockerEnvironment(
                image=image_name,
                cwd='/testbed',
                env=self.env_config.get('env', {}),
                forward_env=[
                    "OPENAI_API_KEY", "OPENAI_BASE_URL",
                    "LITELLM_API_KEY", "LITELLM_BASE_URL",
                    "MSWEA_COST_TRACKING",
                ],
                container_timeout=self.env_config.get('container_timeout', '2h'),
            )
        else:
            local_env_config = {
                'cwd': str(working_dir),
                'timeout': self.env_config.get('timeout', 60),
                'env': self.env_config.get('env', {})
            }
            env = LocalEnvironment(**local_env_config)

        # Prepare agent config - replace /testbed with actual path
        agent_config = self.agent_config.copy()
        for key in ['system_template', 'instance_template', 'action_observation_template',
                    'format_error_template', 'timeout_template']:
            if key in agent_config and agent_config[key]:
                agent_config[key] = agent_config[key].replace('/testbed', str(working_dir))

        # Create agent
        agent = DefaultAgent(
            model=model,
            env=env,
            **agent_config
        )

        logger.info(f"Created mini-swe-agent for {spec.id} with model {spec.model_id}")
        logger.info(f"  Config: {self.config_name}, step_limit={agent_config.get('step_limit')}")
        return agent

    def _extract_repo_path(self, task: str, instance_id: str = None) -> Optional[Path]:
        """Extract repository path from SWE-bench task query or instance_id."""
        import re

        # Try to extract from task query first
        match = re.search(r'Repository:\s*(/[^\s\n]+)', task)
        if match:
            return Path(match.group(1))
        match = re.search(r'Repository:\s*([^\s/]+/([^\s/]+))', task)
        if match:
            repo_name = match.group(2)
            candidate = Path("dataset/repos") / repo_name
            if candidate.exists():
                return candidate

        # Try to extract from instance_id (e.g., "sympy__sympy-11400" -> "sympy")
        if instance_id:
            repo_match = re.match(r'(\w+)__', instance_id)
            if repo_match:
                repo_name = repo_match.group(1)
                local_repo = Path("dataset/repos") / repo_name
                if local_repo.exists():
                    return local_repo
        return None

    def _reset_repo(self, repo_path: Path):
        """Reset repository to clean state."""
        try:
            subprocess.run(["git", "reset", "--hard"], cwd=str(repo_path),
                          capture_output=True, timeout=30)
            subprocess.run(["git", "clean", "-fd"], cwd=str(repo_path),
                          capture_output=True, timeout=30)
        except Exception as e:
            logger.warning(f"Failed to reset repo {repo_path}: {e}")

    def _get_git_diff(self, repo_path: Path) -> str:
        """Get git diff of changes in repository."""
        try:
            result = subprocess.run(["git", "diff"], cwd=str(repo_path),
                                   capture_output=True, text=True, timeout=30)
            return result.stdout
        except Exception as e:
            logger.warning(f"Failed to get git diff from {repo_path}: {e}")
            return ""

    def _extract_problem_statement(self, task: str) -> str:
        """
        Extract problem statement from EvoMAS task format.

        For proper alignment with mini-swe-agent:
        - If task contains "Problem Statement:", extract just that part
        - Otherwise, pass the task as-is (for raw SWE-bench format)

        The agent's instance_template will handle formatting via {{task}}.
        """
        import re

        # Check if this is EvoMAS wrapped format (legacy with CRITICAL INSTRUCTIONS)
        if "Problem Statement:" in task and "CRITICAL INSTRUCTIONS:" in task:
            # Extract just the problem statement, ignore the CRITICAL INSTRUCTIONS
            # as the agent's template already has proper instructions
            match = re.search(r'Problem Statement:\s*\n(.*?)(?=\n\nCRITICAL INSTRUCTIONS:)',
                             task, re.DOTALL)
            if match:
                return match.group(1).strip()

        # Check for new simpler format (Problem Statement at end)
        # Use greedy .* to capture everything including blank lines in the problem
        if "Problem Statement:" in task:
            match = re.search(r'Problem Statement:\s*\n(.*)', task, re.DOTALL)
            if match:
                return match.group(1).strip()

        # Return as-is for raw SWE-bench format (just problem_statement)
        return task

    def run(self, spec: AgentSpec, task: str, context: Optional[Dict[str, Any]] = None) -> AgentResult:
        """Run mini-swe-agent with configured settings."""
        instance_id = context.get('instance_id') if context else None

        # Extract repo path BEFORE extracting problem statement (which strips Repository: line)
        repo_path = self._extract_repo_path(task, instance_id)
        use_repo_dir = repo_path is not None and repo_path.exists()
        logger.debug(f"Repo detection: repo_path={repo_path}, use_repo_dir={use_repo_dir}, instance_id={instance_id}")

        if use_repo_dir:
            logger.info(f"Running mini-swe-agent in repository: {repo_path}")
            work_dir = repo_path
            self._reset_repo(repo_path)
        else:
            work_dir = Path(tempfile.mkdtemp(prefix="miniswe_task_"))
            logger.info(f"Running mini-swe-agent in isolated dir: {work_dir}")

        try:
            agent = self.create_agent(spec, work_dir, instance_id=instance_id)

            # Enhance task with context
            enhanced_task = task
            if context:
                context_str = "\n\nContext from previous agents:\n"
                for key, value in context.items():
                    context_str += f"- {key}: {value}\n"
                enhanced_task = task + context_str

            # Extract problem statement (this removes Repository:/Instance ID: lines)
            problem_statement = self._extract_problem_statement(enhanced_task)

            # For minisweagent: prepend repo path so agent knows where to work
            if use_repo_dir:
                problem_statement = f"Repository: {repo_path}\nInstance ID: {instance_id or spec.id}\n\n{problem_statement}"
                # Prepend action-first guidance to counter over-analysis death spiral
                problem_statement = ACTION_FIRST_PREAMBLE + problem_statement

            logger.debug(f"Problem statement preview: {problem_statement[:200]}...")
            logger.info(f"Running agent with step_limit={self.agent_config.get('step_limit')}")
            run_result = agent.run(problem_statement)
            exit_status = run_result.get('exit_status', 'unknown') if isinstance(run_result, dict) else run_result[0]
            exit_message = run_result.get('submission', '') if isinstance(run_result, dict) else (run_result[1] if isinstance(run_result, tuple) else '')

            # The exit_message from Submitted exception contains the git diff
            output = exit_message
            logger.info(f"Agent finished: {exit_status} (messages={len(agent.messages)})")

            # For local execution (non-Docker), also capture git diff as fallback
            patch_content = ""
            if use_repo_dir:
                patch_content = self._get_git_diff(repo_path)
                if not patch_content:
                    try:
                        import subprocess
                        result = subprocess.run(
                            ["git", "diff", "--cached"],
                            cwd=str(repo_path),
                            capture_output=True, text=True, timeout=30
                        )
                        patch_content = result.stdout
                    except Exception:
                        pass
                if patch_content:
                    logger.info(f"Captured git diff fallback ({len(patch_content)} bytes)")

            # Priority: submission output > git diff fallback
            if exit_message and exit_message.strip().startswith('diff --git'):
                final_output = exit_message
                logger.info("Using submission output (valid diff format)")
            elif patch_content:
                final_output = patch_content
                logger.info("Using git diff fallback")
            else:
                final_output = output
                logger.info("Using raw output (no diff captured)")
            success = exit_status == "Submitted"

            # agent.cost / agent.n_calls (mini-swe-agent v2.4+ aggregates at agent level)
            model_cost = getattr(agent, 'cost', getattr(agent.model, 'cost', 0.0))
            model_calls = getattr(agent, 'n_calls', getattr(agent.model, 'n_calls', 0))
            input_tokens = getattr(agent.model, 'total_input_tokens', 0)
            output_tokens = getattr(agent.model, 'total_output_tokens', 0)
            total_tokens = input_tokens + output_tokens

            result = AgentResult(
                agent_id=spec.id,
                content=final_output,
                metadata={
                    'exit_status': exit_status,
                    'exit_message': exit_message,
                    'model_cost': model_cost,
                    'model_calls': model_calls,
                    'total_messages': len(agent.messages),
                    'input_tokens': input_tokens,
                    'output_tokens': output_tokens,
                    'total_tokens': total_tokens,
                    'had_patch_file': bool(patch_content),
                    'used_repo_dir': use_repo_dir,
                    'config': self.config_name,
                    'step_limit': self.agent_config.get('step_limit'),
                },
                error=None if success else exit_message
            )

            logger.info(f"Agent {spec.id} completed: {exit_status}")
            logger.info(f"  Model calls: {model_calls}, Cost: ${model_cost:.4f}, Tokens: {total_tokens}")
            return result

        except Exception as e:
            logger.error(f"Error running mini-swe-agent {spec.id}: {e}", exc_info=True)
            return AgentResult(
                agent_id=spec.id,
                content="",
                success=False,
                metadata={},
                error=str(e)
            )
        finally:
            if use_repo_dir:
                self._reset_repo(repo_path)
            else:
                try:
                    shutil.rmtree(work_dir, ignore_errors=True)
                except Exception as e:
                    logger.warning(f"Failed to clean up {work_dir}: {e}")
