"""Standalone participant entry point, copied into the sandbox image.

This file runs INSIDE the untrusted environment. Nothing it writes is a score.
"""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path


def main():
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("contest_strategy", root / "strategy.py")
    module = importlib.util.module_from_spec(spec)
    # Keep ordinary participant prints away from the framing protocol.
    with contextlib.redirect_stdout(sys.stderr):
        spec.loader.exec_module(module)
        strategy = module.Strategy()
    for line in sys.stdin:
        request = json.loads(line)
        kind = request["type"]
        with contextlib.redirect_stdout(sys.stderr):
            if kind == "start":
                orders = strategy.on_start(request["config"], request["portfolio"])
            elif kind == "market":
                event = dict(request["event"], now_ns=request["now_ns"])
                orders = strategy.on_market_event(event, request["portfolio"])
            elif kind == "execution":
                orders = strategy.on_execution_report(request, request["portfolio"])
            else:
                raise ValueError("unknown request type")
        sys.stdout.write(
            json.dumps({"orders": [] if orders is None else orders}, allow_nan=False) + "\n"
        )
        sys.stdout.flush()


if __name__ == "__main__":
    main()
