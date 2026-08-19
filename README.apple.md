# Adapting SLATE to macOS

> **Experimental and untested:** SLATE is designed to take advantage of Linux
> desktop and userspace features. The author does not have access to a macOS
> system and cannot test or support this port directly. Corrections and
> reproducible test reports are welcome.

SLATE does not currently run on macOS without source changes. Homebrew provides
GTK 3, GtkSourceView 4, PyGObject, VTE, tmux, and Git for macOS, but its
`webkitgtk` formula requires Linux. The current SLATE preflight intentionally
requires WebKitGTK 4.1, so installing the other dependencies is not sufficient.

The smallest realistic port keeps the GTK 3 and tmux architecture and disables
the integrated browser when WebKitGTK is unavailable. A native browser based on
Apple WKWebView would be a separate backend and substantially more work.

## 1. Install candidate dependencies

Install Homebrew using its official instructions, then install the components
currently available for macOS:

```console
brew install git gtk+3 gtksourceview4 pygobject3 python@3.13 tmux vte3
```

These package names are taken from the current Homebrew formulae but the complete
combination has not been tested with SLATE. In particular, confirm that the same
Homebrew Python can import the required typelibs:

```console
python3.13 -c 'import gi; gi.require_version("Gtk", "3.0")'
python3.13 -c 'import gi; gi.require_version("GtkSource", "4")'
python3.13 -c 'import gi; gi.require_version("Vte", "2.91")'
```

Do not attempt `brew install webkitgtk` on macOS: Homebrew currently builds that
formula only for Linux.

## 2. Make WebKitGTK optional on macOS

The existing tolerant import in `slate/browser.py` already records a missing
WebKitGTK dependency. The port must complete that separation without weakening
Linux validation:

1. Keep WebKitGTK 4.1 mandatory on Linux.
2. Exclude WebKitGTK from the mandatory preflight only when the detected platform
   is macOS.
3. Disable browser actions without changing the layout when WebKitGTK is absent.
4. Preserve existing browser rows in the configuration, but show a clear
   unavailable message instead of deleting them.
5. Keep browser profile data and URLs out of project working copies.

Do not silently substitute Safari for an embedded browser. Opening an external
browser is a different operation and must remain explicit.

## 3. Add platform-specific process integration

The normal launcher uses a Linux double-fork. The macOS path should run the GTK
application directly while developing, with application bundling handled as a
later packaging step. Linux startup behavior must remain unchanged.

SLATE currently delegates external file opening to `xdg-open`. Introduce a
platform adapter that selects:

```text
Linux:  xdg-open PATH
macOS:  open PATH
```

Commands must continue to use argument arrays through the existing asynchronous
process helpers. Do not invoke a shell to build these commands.

The dedicated tmux socket invariant also applies on macOS: every tmux invocation
must keep using SLATE's configured socket and must never reach the user's personal
tmux server.

## 4. Clone and run during port development

After implementing the platform changes, use the Homebrew Python explicitly so
the PyGObject installation is not confused with Apple's system Python:

```console
git clone https://github.com/badpenguin/slate.git
cd slate
python3.13 run-slate --debug
```

Do not create an application bundle until source-checkout startup and the test
suite work reliably. A later bundle will need GTK resources, typelibs, schemas,
icons, and an application launcher; it must not bundle or create a second tmux
server implementation.

## Validation checklist

Because the author cannot test macOS, a port should be considered experimental
until contributors verify at least:

- Apple Silicon and Intel where Homebrew still supplies bottles;
- startup, activation, clean shutdown, and single-instance behavior;
- `Ctrl+C`, clipboard shortcuts, URL detection, and terminal reconnection;
- persistence through closing and reopening SLATE;
- Git and Mercurial watchers, commits, reverts, branches, and tags;
- file creation, editing, atomic saving, external opening, and symlink rejection;
- disabled browser controls and preservation of configured browser rows;
- light and dark system themes without Linux-specific assumptions.

The minimum macOS port should not change the Linux UI or migrate the project to
GTK 4. Native WKWebView integration should be proposed and reviewed separately.

## Relevant upstream packages

- [GTK 3 for Homebrew](https://formulae.brew.sh/formula/gtk%2B3)
- [GtkSourceView 4 for Homebrew](https://formulae.brew.sh/formula/gtksourceview4)
- [PyGObject for Homebrew](https://formulae.brew.sh/formula/pygobject3)
- [VTE for Homebrew](https://formulae.brew.sh/formula/vte3)
- [tmux for Homebrew](https://formulae.brew.sh/formula/tmux)
- [WebKitGTK for Homebrew, Linux only](https://formulae.brew.sh/formula/webkitgtk)
