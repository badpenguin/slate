#!/usr/bin/env bash
# Esegue in un'unica sessione le verifiche automatiche e d'integrazione locali.

set -euo pipefail

project_root=$(cd "$(dirname "$0")/.." && pwd)
test_root=$(mktemp -d)

# 2026-08-18: no automatic check maps a GTK window because every tested window
# manager may grant focus briefly; only temporary repository fixtures remain.
trap 'rm -rf "$test_root"' EXIT

cd "$project_root"
python3 -m compileall -q slate tests
python3 -m unittest discover -v

mkdir -p "$test_root/repo"
hg init "$test_root/repo"
printf 'base\n' > "$test_root/repo/a.txt"
printf 'removed base\n' > "$test_root/repo/removed.txt"
hg -R "$test_root/repo" add "$test_root/repo/a.txt"
hg -R "$test_root/repo" add "$test_root/repo/removed.txt"
HGUSER=Test hg -R "$test_root/repo" commit -m init
printf 'changed\n' > "$test_root/repo/a.txt"

# Verifica il requisito principale: dopo un commit esterno il watcher pubblica
# lo stato pulito entro un secondo dal completamento del comando.
TEST_REPO="$test_root/repo" python3 - <<'PY'
"""Exercise RepoWatcher against a real temporary Mercurial repository."""

import os
import time

from gi.repository import GLib

from slate.processes import CommandResult, run_async
from slate.scm.hg import MercurialSCM
from slate.watcher import RepoWatcher

root = os.environ["TEST_REPO"]
loop = GLib.MainLoop()
commit_finished_at: float | None = None
commit_started = False
timed_out = False
commands = []


def fail_on_timeout() -> bool:
    """Stop the test loop if the expected clean snapshot never arrives."""

    global timed_out
    timed_out = True
    loop.quit()
    return GLib.SOURCE_REMOVE


def commit_finished(result: CommandResult) -> None:
    """Record the exact completion time of the external Mercurial commit."""

    global commit_finished_at
    if not result.ok:
        raise AssertionError(result.stderr)
    commit_finished_at = time.monotonic()


def status_changed(items, _branch: str) -> None:
    """Start the external commit, then validate watcher reconciliation latency."""

    global commit_started
    rows = [(item.path, item.state) for item in items]
    if rows and not commit_started:
        commit_started = True
        commands.append(
            run_async(
                ["hg", "commit", "-u", "Test", "-m", "watcher acceptance"],
                commit_finished,
                cwd=root,
                env={"HGPLAIN": "1", "LC_ALL": "C"},
            )
        )
    elif commit_started and not rows and commit_finished_at is not None:
        latency = time.monotonic() - commit_finished_at
        print(f"watcher_commit_latency={latency:.3f}s")
        if latency >= 1.0:
            raise AssertionError(f"watcher latency {latency:.3f}s >= 1s")
        loop.quit()


watcher = RepoWatcher(root, MercurialSCM(root), status_changed, print)
watcher.set_active(True)
GLib.timeout_add(6000, fail_on_timeout)
loop.run()
watcher.close()
if timed_out or not commit_started or commit_finished_at is None:
    raise AssertionError("watcher acceptance timed out")
PY
printf 'final_checks=ok\n'
