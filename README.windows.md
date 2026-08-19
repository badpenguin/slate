# Running SLATE on Windows through WSLg

> **Experimental and untested:** SLATE is designed to take advantage of Linux
> desktop and userspace features. The author does not have access to a Windows
> system and cannot test or support this procedure directly. Corrections and
> reproducible test reports are welcome.

SLATE does not currently support native Windows. The practical adaptation is to
run the unchanged Linux application inside WSL 2 and display it on the Windows
desktop through WSLg. Microsoft documents WSLg support for Linux GUI applications
on Windows 10 build 19044 or later and Windows 11.

## 1. Install WSL 2 and a minimal Ubuntu environment

Open PowerShell as Administrator and run:

```powershell
wsl --install -d Ubuntu
wsl --update
```

This installs the Ubuntu command-line userspace, not Ubuntu Desktop or a complete
desktop environment. WSLg supplies the display integration needed by GTK.

If a compatible WSL 2 distribution is already installed, it can be reused
instead of installing Ubuntu. Restart Windows if requested, complete the Ubuntu
first-run setup, and confirm that the distribution uses WSL 2:

```powershell
wsl --list --verbose
```

If the Ubuntu entry reports version 1, convert it with:

```powershell
wsl --set-version Ubuntu 2
```

See Microsoft's current
[WSLg installation guide](https://learn.microsoft.com/windows/wsl/tutorials/gui-apps)
if GUI applications cannot open a display.

## 2. Install the Linux dependencies

Run the following commands inside the Ubuntu terminal, not in PowerShell:

```console
sudo apt update
sudo apt install git gir1.2-gtk-3.0 gir1.2-gtksource-4 \
    gir1.2-vte-2.91 gir1.2-webkit2-4.1 python3 python3-gi tmux
```

Optional integrations can be installed with:

```console
sudo apt install meld mercurial tortoisehg vim-gtk3 xdg-utils
```

Install Codex or any other CLI agent inside Ubuntu as well. A Windows-side agent
installation is not automatically shared with the Linux environment.

## 3. Clone and launch SLATE

Keep SLATE and its projects in the WSL Linux filesystem:

```console
mkdir -p ~/src
cd ~/src
git clone https://github.com/badpenguin/slate.git
cd slate
./run-slate
```

Do not run `pip install`. SLATE uses the Python code from the checkout and the
GTK libraries installed by `apt`.

Microsoft recommends keeping files used by Linux tools in the Linux filesystem
for the best performance. Prefer paths such as `~/projects/example` over
`/mnt/c/projects/example`. Windows can access them through `\\wsl$\Ubuntu\home`.
See the
[WSL filesystem guidance](https://learn.microsoft.com/windows/wsl/filesystems).

## Windows integration work

The Linux behavior should remain the default. Any optional WSL-specific changes
should be isolated behind explicit platform detection and preserve SLATE's
existing security and tmux invariants.

- Keep the dedicated SLATE tmux socket inside WSL; never target a Windows or
  unrelated tmux server.
- Continue using Linux paths internally. Convert paths with `wslpath` only when
  passing an explicit path to a Windows process.
- Keep `xdg-open` as the Linux default. A future WSL adapter may use `wslview` or
  `explorer.exe` when the user explicitly requests a Windows application.
- Do not place SLATE configuration or state in a project or on a mounted Windows
  working copy.
- Treat `/mnt/*` file monitoring as a compatibility case: manual **Scan** must
  remain available when cross-filesystem events are delayed or lost.

## Validation checklist

Because this procedure is untested by the author, a Windows contribution should
verify at least:

- startup from an Ubuntu shell and from a PowerShell `wsl` invocation;
- GTK clipboard integration and `Ctrl+C` delivery to the terminal process;
- creation, reconnection, rename, and removal of tmux terminals;
- WebKitGTK normal and incognito pages;
- Git and Mercurial monitoring in both `~/projects` and `/mnt/c`;
- file opening behavior for Linux and Windows applications;
- clean recovery after closing SLATE, suspending Windows, and restarting WSL.

Closing SLATE should leave its tmux sessions running. They cannot survive a full
Windows shutdown or an explicit `wsl --shutdown`.
