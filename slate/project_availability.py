"""Track project directory access asynchronously through shared ancestor monitors."""

from functools import partial
from pathlib import Path
from typing import Callable

from gi.repository import Gio, GLib


class ProjectAvailability:
    """Keep cached availability for sidebar rows without polling or renderer I/O."""

    def __init__(self, on_change: Callable[[str, bool], None]) -> None:
        """Initialize a single shared directory-access tracker without starting processes."""

        self.on_change = on_change
        self.roots: dict[str, str] = {}
        self.available: dict[str, bool] = {}
        self.dependencies: dict[str, set[str]] = {}
        self.monitors: dict[str, Gio.FileMonitor] = {}
        self.requests: dict[str, Gio.Cancellable] = {}
        self.timers: dict[str, int] = {}
        self.closed = False

    def set_projects(self, projects: list[dict]) -> None:
        """Reconcile configured paths while retaining unchanged requests and cached states."""

        roots = {project["name"]: str(Path(project["path"]).absolute()) for project in projects}
        changed = {name for name in self.roots.keys() | roots.keys() if self.roots.get(name) != roots.get(name)}
        for name in changed:
            self._cancel(name)
            self.available.pop(name, None)
        self.roots = roots
        self.dependencies = {}
        # 2026-11-06: parent monitors are shared across projects. Observing the
        # complete ancestor chain also detects a deleted container returning,
        # without an existence poll or a second recursive project watcher.
        for name, root in roots.items():
            for path in (Path(root), *Path(root).parents):
                self.dependencies.setdefault(str(path), set()).add(name)
        self._sync_monitors()
        for name in changed & roots.keys():
            self.refresh(name)

    def _sync_monitors(self) -> None:
        """Mount only shared parent directories and release obsolete monitor paths."""

        wanted = {str(Path(path).parent) for path in self.dependencies}
        for path in self.monitors.keys() - wanted:
            self.monitors.pop(path).cancel()
        for path in wanted - self.monitors.keys():
            try:
                monitor = Gio.File.new_for_path(path).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            except GLib.Error:
                # A higher ancestor still detects recreation or permission changes.
                continue
            monitor.connect("changed", self._changed)
            self.monitors[path] = monitor

    def _changed(self, _monitor, file, other, event) -> None:
        """Debounce only events affecting a configured root or one of its ancestors."""

        if self.closed:
            return
        paths = {item.get_path() for item in (file, other) if item is not None}
        relevant = paths & self.dependencies.keys()
        if not relevant:
            return
        if event in {Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.DELETED,
                     Gio.FileMonitorEvent.MOVED, Gio.FileMonitorEvent.RENAMED,
                     Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.MOVED_OUT}:
            # Old monitors may retain a deleted inode. Remount affected parents
            # before checking access so subsequent recreations remain observable.
            for path in tuple(self.monitors):
                if any(path == changed or path.startswith(changed.rstrip("/") + "/") for changed in relevant):
                    self.monitors.pop(path).cancel()
            self._sync_monitors()
        names = set().union(*(self.dependencies[path] for path in relevant))
        for name in names:
            self._cancel(name)
            self.timers[name] = GLib.timeout_add(100, self._refresh_pending, name)

    def _refresh_pending(self, name: str) -> bool:
        """Run one trailing access check after a filesystem-event burst."""

        self.timers.pop(name, None)
        self.refresh(name)
        return GLib.SOURCE_REMOVE

    def refresh(self, name: str) -> None:
        """Query directory type and effective read/search access without blocking GTK."""

        if self.closed or name not in self.roots:
            return
        self._cancel(name)
        token = Gio.Cancellable()
        self.requests[name] = token
        try:
            Gio.File.new_for_path(self.roots[name]).query_info_async(
                "standard::type,access::can-read,access::can-execute", Gio.FileQueryInfoFlags.NONE,
                GLib.PRIORITY_DEFAULT, token, partial(self._checked, name, token),
            )
        except (GLib.Error, ValueError):
            self.requests.pop(name, None)
            self._publish(name, False)

    def _checked(self, name, token, file, result) -> None:
        """Publish only the latest access result for a still-configured project."""

        try:
            info = file.query_info_finish(result)
            available = (info.get_file_type() == Gio.FileType.DIRECTORY
                         and info.get_attribute_boolean("access::can-read")
                         and info.get_attribute_boolean("access::can-execute"))
        except GLib.Error:
            available = False
        if self.closed or self.requests.get(name) is not token:
            return
        self.requests.pop(name)
        self._publish(name, available)

    def _publish(self, name: str, available: bool) -> None:
        """Notify the sidebar only when the cached accessibility actually changes."""

        previous = self.available.get(name)
        self.available[name] = available
        if previous != available:
            self.on_change(name, available)

    def _cancel(self, name: str) -> None:
        """Cancel one project's obsolete metadata request and trailing timer."""

        token = self.requests.pop(name, None)
        if token is not None:
            token.cancel()
        timer = self.timers.pop(name, None)
        if timer is not None:
            GLib.source_remove(timer)

    def close(self) -> None:
        """Release all pending checks and shared monitors during GUI shutdown."""

        self.closed = True
        for name in self.requests.keys() | self.timers.keys():
            self._cancel(name)
        for monitor in self.monitors.values():
            monitor.cancel()
        self.monitors.clear()
