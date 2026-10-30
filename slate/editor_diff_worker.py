"""Compute experimental editor diffs over stdin/stdout without filesystem state."""

import json
import sys
from dataclasses import asdict

from .editor_diff_model import project_diff


def main() -> None:
    """Read a bounded UTF-8 request and emit the derived visual rows as JSON."""

    # 2026-10-30: CPU-heavy matching runs outside GTK; both texts travel in
    # memory and no temporary copies of project files are written to disk.
    maximum = 5 * 1024 * 1024
    payload = sys.stdin.buffer.read(maximum * 12 + 1)
    if len(payload) > maximum * 12:
        raise ValueError("Diff request too large")
    request = json.loads(payload)
    before = request["before"]
    current = request["current"]
    if not isinstance(before, str) or not isinstance(current, str):
        raise ValueError("Diff inputs must be text")
    if max(len(before.encode("utf-8")), len(current.encode("utf-8"))) > maximum:
        raise ValueError("Diff input exceeds the editor size limit")
    if before.count("\n") + current.count("\n") > 50000:
        raise ValueError("Diff too large to display safely; use Code")
    json.dump([asdict(row) for row in project_diff(before, current)], sys.stdout)


if __name__ == "__main__":
    main()
