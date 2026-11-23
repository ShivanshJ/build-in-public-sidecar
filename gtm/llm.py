import json
import os
import shlex
import shutil
import subprocess
import time

from .db import add_usage, cfg, home

CLAUDE_CANDIDATES = ["/opt/homebrew/bin/claude", os.path.expanduser("~/.local/bin/claude"), "/usr/local/bin/claude"]


def default_cmd() -> str:
    exe = shutil.which("claude") or next((p for p in CLAUDE_CANDIDATES if os.path.exists(p)), "claude")
    cmd = f"{exe} -p --no-session-persistence"
    model = cfg("model")
    return f"{cmd} --model {model}" if model else cmd


def _why_failed(r) -> str:
    """`claude -p --output-format json` reports errors on stdout, leaving stderr empty."""
    try:
        msg = json.loads(r.stdout).get("result")
    except ValueError:
        msg = None
    return " ".join(str(msg or r.stderr or r.stdout or "no output").split())[:500]


def complete(prompt: str, timeout: int = 300) -> str:
    """Run the prompt through `claude -p` (or GTM_LLM_CMD, which must read stdin and write stdout).

    With the default command we ask for JSON so token usage and cost can be logged; a custom command is
    counted as one call with unknown tokens.
    """
    custom = cfg("llm_cmd")
    cmd = shlex.split(custom or default_cmd() + " --output-format json")
    env = {**os.environ, "GTM_DRAFTING": "1"}
    for attempt in (1, 2):
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, timeout=timeout, cwd=home())
        if r.returncode == 0:
            break
        if attempt == 2:
            raise RuntimeError(f"LLM command failed ({r.returncode}): {_why_failed(r)}")
        time.sleep(5)
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
