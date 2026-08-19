# SLATE — Simple Linux Agent Terminal Environment

English | [Italiano](README.it.md)

SLATE brings projects, persistent terminals, Git/Mercurial changes, files,
an editor, and web pages together in a single GTK 3 application for Linux.

It is designed for people who work on multiple projects at the same time and
want to find their terminals and tools in one place, without creating state
files inside working directories.

## Features

- an ordered list of projects and their terminals;
- persistent terminals through a dedicated tmux server;
- quick launch of Codex or any shell command;
- status and essential operations for Git and Mercurial repositories;
- file browsing with previews and context actions;
- an integrated GtkSourceView editor;
- a WebKitGTK browser with regular, incognito, and responsive preview modes;
- on-demand loading: no terminals, editors, or web pages are opened at startup
  until they are selected.

## Quick installation

### Requirements

SLATE requires:

- Linux with a desktop environment and graphical session;
- Python 3.11 or later with PyGObject;
- GTK 3, GtkSourceView 4, Vte 2.91, and WebKitGTK 4.1;
- tmux 3.x;
- Git to clone SLATE and manage Git repositories.

On Ubuntu 24.04 and Linux Mint 22, install the required components with:

```console
sudo apt update
sudo apt install git gir1.2-gtk-3.0 gir1.2-gtksource-4 \
    gir1.2-vte-2.91 gir1.2-webkit2-4.1 python3 python3-gi tmux
```

These tools enable additional features:

```console
sudo apt install meld mercurial tortoisehg vim-gtk3 xdg-utils
```

| Package      | Feature                                   |
|--------------|-------------------------------------------|
| `meld`       | Graphical comparisons                     |
| `mercurial`  | Mercurial repositories                    |
| `tortoisehg` | TortoiseHg interface for Mercurial repos  |
| `vim-gtk3`   | External editing with gVim                |
| `xdg-utils`  | Open with the default desktop application |

The **Codex** button requires the
[Codex CLI](https://learn.chatgpt.com/docs/codex/cli), already installed and
authenticated. All other SLATE features remain available without Codex.

### Clone and launch

```console
git clone https://github.com/badpenguin/slate.git
cd slate
./run-slate
```

There is no need to run `pip install`: the checkout contains the application's
Python code, while the GTK libraries are provided by system packages.

## First use

1. Click **New Project** and choose an existing directory.
2. Select the project in the left column.
3. Click **New Terminal** to create the project's first terminal.
4. Use **Codex** to open `codex resume`, or **Execute** to start and remember a
   custom command.

SLATE associates each terminal with the selected project. Switching to another
project does not interrupt any processes, and returning to it reuses the same
terminal.

## Interface

The window is divided into three columns:

1. **Projects**: open projects, terminals, editors, and web pages.
2. **Workspace**: the selected terminal, editor, or browser.
3. **Changes/Files**: repository changes or the file tree.

```text
New Project | New Terminal | Execute | Codex | Open URL | Incognito
┌────────────────────────┬──────────────────────────────────────────┬────────────────────────────┐
│ PROJECTS               │ WORKSPACE                                │ CHANGES | FILES            │
├────────────────────────┼──────────────────────────────────────────┼────────────────────────────┤
│ ▾ demo-project         │ $ npm run dev                            │ Git · main                 │
│   terminal-1           │ Starting the development server...       │                            │
│   codex-1              │                                          │ Modified                   │
│   src/app.py           │ The selected terminal, editor, or        │   [×] src/app.py           │
│   Web Page             │ browser occupies this column.            │                            │
│                        │                                          │ New                        │
│ ▸ second-project       │ Switching items does not interrupt       │   [ ] tests/test_app.py    │
│                        │ processes in other terminals.            │                            │
│                        │                                          │ Commit message             │
│                        │                                          │ [Select all] [Commit]      │
└────────────────────────┴──────────────────────────────────────────┴────────────────────────────┘
```

The active project keeps its terminals, open files, and web pages separate.
The text sizes for the terminal, Changes, Files, and Editor views can be
adjusted in **Settings**.

## Terminals

### Creation and persistence

**New Terminal** creates a persistent shell. **Execute** accepts a command line and
automatically assigns a name such as `ssh-1` or `npm-1`. **Codex** creates a
`codex-N` terminal and starts `codex resume`.

SLATE also remembers the command associated with each terminal. If the tmux
session is still active, it simply reconnects to it; if the session no longer
exists, SLATE recreates it when it is first selected. Running `exit` removes
the finished terminal from the list.

A terminal can be renamed with **Rename** or `F2`.

### Shortcuts

| Action                | Shortcuts                       |
|-----------------------|---------------------------------|
| Copy                  | `Ctrl+Shift+C` or `Ctrl+Insert`  |
| Paste                 | `Ctrl+Shift+V` or `Shift+Insert` |
| Interrupt the process | `Ctrl+C`                        |
| Rename the terminal   | `F2`                            |

`Ctrl+C` without Shift always reaches the process running in the terminal.

## Git and Mercurial changes

The **Changes** tab detects Git and Mercurial repositories in the
project directory and its subdirectories. Status is updated automatically when
files or repository metadata change.

Selecting a modified file displays a diff preview. New files show their current
contents; removed files show their base version; recognized moves appear as
`source → destination`.

### Commit and revert

- tracked files are included in a commit using the checkboxes;
- new files must be added explicitly with **Add**;
- **Select all** selects all tracked files;
- the commit becomes available after entering a message;
- `Ctrl+Enter` performs the commit;
- reverting and deleting require confirmation.

The Git view is intentionally similar to the Mercurial view: it does not split
staged and unstaged changes, and it never runs `git add` automatically before a
commit.

### Repository operations

The repository root context menu provides:

- **Update**;
- **Publish**;
- **New branch**;
- **Switch branch**;
- **Merge branch**;
- **Assign tag**;
- opening in Meld and, for Mercurial, in TortoiseHg.

SLATE only uses remotes and upstreams that are already configured. It does not
configure destinations automatically, perform force pushes or rebases, or
create commits automatically during merges. Conflicts, divergences, and
ambiguous situations are reported and left for the user to handle explicitly.

During **Publish**, Git automatically requests any credentials
required by an HTTPS remote. SLATE displays a username and password or token
dialog and uses the values only for that publication, without saving them in
its configuration, the repository, or a credential manager. GitHub requires a
personal access token instead of the account password.

Git worktrees and submodules where `.git` is a file are not supported.

### Quick file actions

| Action                                 | Shortcut |
|----------------------------------------|----------|
| View with the default application      | `V`      |
| Edit in the built-in editor            | `M`      |
| Edit with gVim                         | `E`      |
| Add a new file                         | `A`      |
| Delete with confirmation               | `Delete` |
| Match checkboxes for highlighted files | `Space`  |

## Files and editor

The **Files** tab displays the project tree. Directories are loaded on demand
and retain their expansion and position when switching between projects.

- **+ File** creates a file;
- the context menu can create directories, rename and delete items, and open a
  terminal at the selected location;
- **Hidden** shows hidden files;
- **Excluded** shows files ignored by the repository;
- **Expand** recursively opens the tree while stopping at large
  excluded directories.

The `.git`, `.hg`, and `.svn` metadata directories are never displayed.
Symbolic links and paths that leave the project directory cannot be used to
read, edit, or delete external files.

**Edit in SLATE** opens a file in the central column with
syntax highlighting, line numbers, search, undo/redo, and atomic saving. If the
file also changes on disk, SLATE asks which version to keep instead of
overwriting it automatically.

The editor supports UTF-8 text files and does not retain drafts of unsaved
content.

## Browser

**Open URL** adds a web page to the project. Regular pages retain
their URL, title, and position and are loaded only when first selected.

**Incognito** creates a separate ephemeral context for each page. SLATE
remembers the tab and its last URL, but discards anonymous cookies and storage;
after a restart, it creates a new incognito context.

The **Responsive** menu centers the page within predefined dimensions and
scales it down when it does not fit the available space. It does not emulate
touch, DPR, or the user agent. `F12` or `Ctrl+Shift+I` opens the WebKit developer
tools.

| Action                   | Shortcut               |
|--------------------------|------------------------|
| Focus the URL bar        | `Ctrl+L`               |
| Back/Forward             | `Alt+←` / `Alt+→`      |
| Reload                   | `Ctrl+R` or `F5`        |
| Stop                     | `Esc`                  |
| Close the page           | `Ctrl+W`               |
| Open the developer tools | `F12` or `Ctrl+Shift+I` |

## Data and tmux sessions

The configuration is stored in:

```text
~/.config/slate/config.json
```

SLATE does not write configuration, cache, or state files inside project
directories.

Regular web pages share the profile stored in:

```text
$XDG_DATA_HOME/slate/webkit
$XDG_CACHE_HOME/slate/webkit
```

Terminals exclusively use the separate `tmux -L slate` server, isolated from
the user's personal tmux server. Sessions survive a GUI crash, but not a reboot
or logout that terminates user processes.

Active sessions can be listed and reached manually:

```console
tmux -L slate list-sessions
tmux -L slate attach-session -t SESSION_NAME
```

To detach from tmux without ending the session, press `Ctrl+B`, then `D`.

On systems configured with `KillUserProcesses=yes`, persistence beyond logout
may require:

```console
loginctl enable-linger "$USER"
```

Consider this setting carefully because it affects the entire user session,
not only SLATE.

## Diagnostics

To keep SLATE attached to the terminal and display errors and tracebacks:

```console
./run-slate --debug
```

Debug mode uses the production configuration and sessions. It may also print
complete URLs: review the output before sharing it because query strings and
fragments can contain tokens or other sensitive data.

To start an isolated instance with temporary configuration, application ID,
and tmux socket:

```console
./run-slate --agent-debug
```

A second normal launch does not create another window: it presents the already
running instance.

## Tests

The automated suite does not open GTK windows:

```console
./tests/run-final-checks.sh
```

Manual interface verification remains separate because the window manager may
briefly give focus to a test window.

## Issues and contributions

Bug reports and proposals can be opened in the
[GitHub issue tracker](https://github.com/badpenguin/slate/issues). Include the
Linux distribution, SLATE version, reproduction steps, and any diagnostic
messages after removing sensitive data.

## License

- **SLATE** is free software distributed under the terms of the GNU General
  Public License, version 2 or, at your option, any later version. The complete
  text is available in [`LICENSE`](LICENSE).
- The **Git logomark** was created by Jason Long and is distributed under the
  [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) license.
- The Mercurial **“droplets” logo** was created by Cali Mastny and Matt Mackall
  and is distributed under the GPLv2+ license.
- The **Incognito** icon is an original SLATE design distributed under the
  GPLv2+ license.
- The **Codex** button uses the grayscale Blossom to identify the OpenAI service
  it launches; OpenAI and its related graphics are trademarks of OpenAI.
- **Git and the Git logo** are either registered trademarks or trademarks of
  Software Freedom Conservancy, Inc., the corporate host of the Git project.
