"""Ephemeral GTK credential helper for explicit Git HTTPS operations."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Mapping, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # noqa: E402


# 2026-08-19: Git's credential protocol decides when HTTPS authentication is
# needed; SLATE supplies one-use values without putting secrets in argv, URLs,
# environment variables, files or persistent credential helpers.
class GitCredentialDialog(Gtk.Dialog):
    """Collect one username and password/token pair for a Git HTTPS request."""

    def __init__(self, host: str, username: str) -> None:
        """Build the credential form with an optional username suggested by Git."""

        super().__init__(title="Git HTTPS Authentication", modal=True)
        self.set_default_size(440, -1)
        self.set_keep_above(True)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.confirm_button = self.add_button("Continue", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.OK)

        content = self.get_content_area()
        content.set_border_width(18)
        content.set_spacing(12)

        explanation = Gtk.Label(
            label="Enter credentials valid only for this publication.",
            xalign=0,
        )
        explanation.set_line_wrap(True)
        content.pack_start(explanation, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        content.pack_start(grid, False, False, 0)

        host_title = Gtk.Label(label="Server", xalign=0)
        host_value = Gtk.Label(label=host or "Remote repository", xalign=0)
        host_value.set_selectable(True)
        host_value.set_ellipsize(Pango.EllipsizeMode.END)
        grid.attach(host_title, 0, 0, 1, 1)
        grid.attach(host_value, 1, 0, 1, 1)

        username_label = Gtk.Label(label="Username", xalign=0)
        self.username_entry = Gtk.Entry()
        self.username_entry.set_text(username)
        self.username_entry.set_activates_default(True)
        self.username_entry.connect("changed", self._on_credentials_changed)
        grid.attach(username_label, 0, 1, 1, 1)
        grid.attach(self.username_entry, 1, 1, 1, 1)

        password_label = Gtk.Label(
            label=(
                "Personal access token"
                if host.lower() == "github.com"
                else "Password or token"
            ),
            xalign=0,
        )
        self.password_entry = Gtk.Entry()
        self.password_entry.set_visibility(False)
        self.password_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
        self.password_entry.set_activates_default(True)
        self.password_entry.connect("changed", self._on_credentials_changed)
        grid.attach(password_label, 0, 2, 1, 1)
        grid.attach(self.password_entry, 1, 2, 1, 1)

        self._on_credentials_changed(self.username_entry)
        self.show_all()
        if username:
            self.password_entry.grab_focus()
        else:
            self.username_entry.grab_focus()

    def _on_credentials_changed(self, _entry: Gtk.Entry) -> None:
        """Allow confirmation only when both required values are present."""

        self.confirm_button.set_sensitive(
            bool(self.username_entry.get_text())
            and bool(self.password_entry.get_text())
        )

    def credentials(self) -> tuple[str, str]:
        """Return the values currently entered without persisting them."""

        return self.username_entry.get_text(), self.password_entry.get_text()


def credential_environment(base: Mapping[str, str]) -> dict[str, str]:
    """Return a Git environment using only SLATE's ephemeral credential helper."""

    environment = dict(base)
    helper = f"!{shlex.quote(sys.executable)} -m slate.git_credentials"
    # An empty first value clears configured helpers for this process only;
    # otherwise Git could persist the entered token through a global helper.
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": helper,
        }
    )
    return environment


def _read_request() -> dict[str, str]:
    """Read one Git credential-protocol request from standard input."""

    request: dict[str, str] = {}
    for raw_line in sys.stdin:
        line = raw_line.rstrip("\n")
        if not line:
            break
        key, separator, value = line.partition("=")
        if separator:
            request[key] = value
    return request


def _discard_request() -> None:
    """Consume a store/erase request without retaining credential values."""

    for raw_line in sys.stdin:
        if raw_line in {"\n", "\r\n"}:
            break


def _prompt(request: Mapping[str, str]) -> tuple[str, str] | None:
    """Show the isolated GTK prompt and return credentials unless cancelled."""

    initialized, _arguments = Gtk.init_check(None)
    if not initialized:
        return None
    dialog = GitCredentialDialog(
        request.get("host", ""),
        request.get("username", ""),
    )
    try:
        if dialog.run() != Gtk.ResponseType.OK:
            return None
        return dialog.credentials()
    finally:
        dialog.destroy()


def main(argv: Sequence[str] | None = None) -> int:
    """Serve Git's get/store/erase credential-helper operations without storage."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    operation = arguments[0] if arguments else ""
    if operation != "get":
        _discard_request()
        return 0

    request = _read_request()
    credentials = _prompt(request)
    if credentials is None:
        return 1
    username, password = credentials
    if any(character in username + password for character in ("\n", "\0")):
        return 1
    sys.stdout.write(f"username={username}\npassword={password}\n\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
