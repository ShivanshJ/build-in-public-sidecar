"""Live progress for long-running commands: a spinner with elapsed seconds on a terminal, plain lines in logs."""
import os
import re
import sys
import threading
import time
from contextlib import contextmanager

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_lock = threading.Lock()
_stack: list[list] = []  # [label, started_at] of active spinners, innermost last
_thread: threading.Thread | None = None


def tty() -> bool:
    return sys.stdout.isatty()


def color() -> bool:
    return tty() and not os.environ.get("NO_COLOR")


def dim(t: str) -> str:
    return f"\033[2m{t}\033[0m" if color() else t


def _spin():
    i = 0
    while True:
        time.sleep(0.1)
        with _lock:
            if _stack:
                label, t0 = _stack[-1]
                sys.stdout.write(f"\r\033[K  {FRAMES[i % len(FRAMES)]} {label}  {dim(f'{time.time() - t0:.0f}s')}")
                sys.stdout.flush()
        i += 1


def say(msg: str = "") -> None:
    """Print a line without tearing the spinner."""
    with _lock:
        if tty() and _stack:
            sys.stdout.write("\r\033[K")
        print(msg, flush=True)


@contextmanager
def working(label: str):
    """Show `label` with a spinner until the block ends. Prints nothing when not on a terminal."""
    global _thread
    if not tty():
        yield
        return
    with _lock:
        _stack.append([label, time.time()])
    if _thread is None:
        _thread = threading.Thread(target=_spin, daemon=True)
        _thread.start()
    try:
        yield
    finally:
        with _lock:
            _stack.pop()
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


def update(label: str) -> None:
    with _lock:
        if _stack:
            _stack[-1][0] = label


def snippet(text: str, limit: int = 80, floor: int = 50) -> str:
    """The opening sentence(s) of a post, about 50-80 characters, ending in '...' when something was cut."""
    t = " ".join((text or "").split())
    shown = ""
    for sent in re.findall(r".+?(?:[.!?](?=\s|$)|$)", t):
        shown = f"{shown} {sent.strip()}".strip()
        if len(shown) >= floor:
            break
    if shown == t and len(t) <= limit:
        return t
    if len(shown) > limit:
        shown = shown[:limit].rsplit(" ", 1)[0] if " " in shown[:limit] else shown[:limit]
    return shown.rstrip(" ,;:.!?") + "..."


def show_post(post: dict, is_new: bool) -> None:
    tag = "new    " if is_new else "updated"
    n_l, n_r = post["likes"], post["replies"]
    stats = f"{n_l} like{'' if n_l == 1 else 's'}, {n_r} repl{'y' if n_r == 1 else 'ies'}"
    say(f"    {tag}  {snippet(post['text']):<84} {dim(stats)}")
