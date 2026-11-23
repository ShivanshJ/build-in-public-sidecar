import json
import os
import shlex
import shutil
import subprocess

from .db import add_usage, cfg, home

CLAUDE_CANDIDATES = ["/opt/homebrew/bin/claude", os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude"]


def default_cmd() -> str:
    exe = shutil.which("claude") or next((p for p in CLAUDE_CANDIDATES if os.path.exists(p)), "claude")
    cmd = f"{exe} -p --no-session-persistence"
    model = cfg("model")
    return f"{cmd} --model {model}" if model else cmd


def complete(prompt: str, timeout: int = 300, web: bool = False) -> str:
    """Run the prompt through `claude -p` (or GTM_LLM_CMD, which must read stdin and write stdout).

    web=True lets Claude use its WebSearch and WebFetch tools (default command only).

    With the default command we ask for JSON so token usage and cost can be logged; a custom command is
    counted as one call with unknown tokens.
    """
    custom = cfg("llm_cmd")
    if web and custom:
        raise RuntimeError("web search needs the default `claude -p` command; unset llm_cmd")
    tools = " --allowedTools WebSearch,WebFetch" if web else ""
    cmd = shlex.split(custom or default_cmd() + tools + " --output-format json")
    env = {**os.environ, "GTM_DRAFTING": "1"}
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, timeout=timeout, cwd=home())
    if r.returncode != 0:
        raise RuntimeError(f"LLM command failed ({r.returncode}): {r.stderr.strip()[:500]}")
    if custom:
        add_usage()
        return r.stdout.strip()
    try:
        d = json.loads(r.stdout)
        u = d.get("usage", {})
        add_usage(
            u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
            u.get("output_tokens", 0),
            d.get("total_cost_usd", 0.0),
        )
        return str(d["result"]).strip()
    except (ValueError, KeyError):
        add_usage()
        return r.stdout.strip()
