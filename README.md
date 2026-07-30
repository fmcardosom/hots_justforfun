# HotS Teammate Stats

A standalone Python tool that scans your local **Heroes of the Storm** replay files
(`.StormReplay`) and answers a simple question:

> *How many games did I win / lose with a specific player on my team?*

It works entirely offline from the replays already on your disk — no account login,
no third-party website, no data leaves your machine.

![GUI mode](docs/screenshot.png) <!-- optional: add a screenshot here -->

---

## Features

- **GUI mode** (default) — Tkinter window with name fields, replay-folder picker,
  live progress bar, headline win/loss summary and a per-game table
  (date, map, result, hero you played, hero your teammate played).
  Wins are tinted green, losses red.
- **CLI mode** — same analysis from the command line, for scripted use.
- **Self-bootstrapping** — on first run the script installs its only dependency
  ([`heroprotocol`](https://github.com/Blizzard/heroprotocol), Blizzard's official
  replay decoder) automatically via `pip`.
- **Python 3.12+ compatible** — includes a small compatibility shim for
  `heroprotocol`, which still imports the `imp` module removed in Python 3.12.
- **Auto-detection** of the standard replay location under
  `Documents\Heroes of the Storm\Accounts` (including OneDrive-redirected
  Documents folders).
- **Honest counting** — games where two players share the same display name are
  reported as *ambiguous* and skipped instead of guessed; games where you and the
  other player were on **opposite** teams are counted separately.

---

## Requirements

| Requirement | Notes |
|---|---|
| **Windows 10/11** | Where HotS stores replays. The script itself also runs on Linux/macOS if you point it at a copied replay folder. |
| **Python 3.10 or newer** | 3.12/3.13 fully supported (compatibility shim included). |
| **pip** | Included with the official Python installer. |
| **Internet access on first run** | Only to auto-install `heroprotocol` from PyPI. |

### Installing Python on Windows

If double-clicking the script opens a window that **closes immediately**, or
`python` is not recognised in the terminal, Python is missing or incorrectly
installed. Install it properly:

1. Download the latest **Python for Windows** installer:
   - Official download page: <https://www.python.org/downloads/windows/>
   - Direct "latest release" page: <https://www.python.org/downloads/>
2. Run the installer and, on the **first screen**, tick **both**:
   - ☑ *Add python.exe to PATH*
   - ☑ *Install pip*
   (then click *Install Now*).
3. Verify the installation — open **Command Prompt** (`Win+R` → `cmd`) and run:

   ```bat
   python --version
   pip --version
   ```

   Both commands must print a version number.

Alternative: install "Python 3.x" from the **Microsoft Store**
(<https://apps.microsoft.com/search?query=python>), which configures PATH
automatically.

> **Note:** the `py` launcher (installed by the python.org installer) also works:
> `py hots_teammate_stats.py`.

---

## Installation

```bat
git clone https://github.com/<your-user>/hots-teammate-stats.git
cd hots-teammate-stats
```

No `pip install -r requirements.txt` step is needed — the script installs
`heroprotocol` by itself on first run.

---

## Usage

### GUI (default)

```bat
python hots_teammate_stats.py
```

1. Fill in **Your name** and the **Teammate**'s in-game name
   (the `#1234` BattleTag discriminator is optional and ignored — replays store
   display names without it).
2. Confirm the auto-detected **Replay folder** or pick one with *Browse…*
3. Press **Scan** and watch the progress bar; results appear when done.

### CLI

```bat
python hots_teammate_stats.py --me "MyName" --teammate "FriendName" --list
```

| Option | Description |
|---|---|
| `--me NAME` | Your in-game display name (required). |
| `--teammate NAME` | The teammate's display name (required). |
| `--replays PATH` | Folder scanned recursively for `.StormReplay` files. Default: auto-detect. |
| `--list` | Also print every shared game (date, map, result, heroes), chronologically. |

Example output:

```
Scanning 412 replays under: C:\Users\me\Documents\Heroes of the Storm\Accounts

Replays scanned      : 412
Unreadable replays   : 3
Ambiguous name games : 0

Games with 'FriendName' on MyName's team : 57
  Wins    : 31
  Losses  : 26
  Win rate: 54.4%  (31-26)
Games on OPPOSITE teams: 4
```

---

## How it works

Each `.StormReplay` file is an MPQ archive. The script follows Blizzard's own
reference implementation (`heroprotocol`):

1. Reads the version-independent protocol **header** to get the replay's
   `baseBuild`.
2. Loads the matching `protocolNNNNN` decoder module (falling back to the latest
   one when that exact build is not shipped — the *details* format is stable).
3. Decodes `replay.details` → `m_playerList`, which contains each player's
   display name (`m_name`), team (`m_teamId`) and result
   (`m_result`: `1` = win, `2` = loss).
4. Tallies every game where both players are present **and** on the same team.

The scan runs in a background thread in GUI mode; results are passed back to the
Tkinter main loop through a queue polled with `after()` (the thread-safe Tkinter
pattern).

---

## Limitations

- Only replays **still on disk** are counted. HotS has no public match-history
  API, so games whose replays were deleted (or played on another machine) are
  invisible to this tool.
- Matching is by **display name**. Replay *details* do not include the BattleTag
  discriminator, so if two different players in one match share a display name,
  that game is skipped as ambiguous rather than guessed.
- Replays saved before a match ended may carry no result and are counted as
  *unknown*.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Window flashes and closes immediately | Python missing or broken. Reinstall from <https://www.python.org/downloads/windows/> with *Add python.exe to PATH* ticked, then run the script **from a terminal** so any message stays visible. |
| `'python' is not recognized ...` | PATH not set — reinstall as above, or use `py hots_teammate_stats.py`. |
| `[bootstrap] Failed to install 'heroprotocol'` | No internet or pip broken. Run `python -m pip install heroprotocol` manually. |
| `Unsupported base build` / many unreadable replays | Update the decoder: `python -m pip install --upgrade heroprotocol`. |
| GUI does not open on Linux | Install Tk: `sudo apt install python3-tk` (Debian/Ubuntu) — or use CLI mode. |

---

## License and credits

- Replay decoding uses [`heroprotocol`](https://github.com/Blizzard/heroprotocol),
  © Blizzard Entertainment, released under the MIT license.
- *Heroes of the Storm* is a trademark of Blizzard Entertainment. This is an
  unofficial fan tool, not affiliated with or endorsed by Blizzard.

---

## Glossary

| Term | Expansion | Explanation |
|---|---|---|
| API | Application Programming Interface | A programmatic interface for accessing a service's data; Blizzard offers none for HotS match history, hence the local-replay approach. |
| BattleTag | — | Blizzard account identifier in the form `Name#1234`; replay details store only the name part. |
| CLI | Command-Line Interface | Running the tool from a terminal with arguments instead of the graphical window. |
| GUI | Graphical User Interface | The Tkinter window mode of the tool. |
| HotS | Heroes of the Storm | Blizzard's multiplayer online battle arena game, whose replays this tool analyses. |
| MPQ | Mo'PaQ (Mike O'Brien Pack) | Blizzard's archive file format; every `.StormReplay` file is an MPQ archive. |
| PATH | — | The operating-system variable listing folders searched for executables; Python must be on it for the `python` command to work. |
| pip | Pip Installs Packages | Python's package installer, used by the script to self-install its dependency. |
| PyPI | Python Package Index | The public repository from which `pip` downloads `heroprotocol`. |
| Tk / Tkinter | Tool Kit / Tk interface | The GUI toolkit bundled with Python's standard library, used for the window mode. |
