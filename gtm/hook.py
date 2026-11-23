"""Claude Code Stop/SessionEnd hook: stdlib only, fast, never fails the session."""
import json
import os
import sys
import traceback


def main() -> None:
    if os.environ.get("GTM_DRAFTING"):
        return
    try:
        path = json.load(sys.stdin).get("transcript_path")
        if path and os.path.exists(path):
            from .db import connect
            from .ingest import ingest_file

            ingest_file(connect(), path)
    except Exception:
        try:
            from .db import home

            with open(home() / "hook.log", "a") as f:
                f.write(traceback.format_exc() + "\n")
        except Exception:
            pass


if __name__ == "__main__":
    main()
