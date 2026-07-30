# HotS Teammate Stats

A standalone Python tool that scans your local **Heroes of the Storm** replay
files (`.StormReplay`) and answers a simple question:

> *How many games did I win / lose with a specific player on my team?*

It works entirely offline from the replays already on your disk — no account
login, no third-party website, no data leaves your machine. It ships as a
single script that runs either as a desktop **GUI** or from the **command
line**, and can produce a signed **PDF report** whose authenticity you can
later verify with a numeric code.

<!-- Optional: add a screenshot at docs/screenshot.png
![GUI mode](docs/screenshot.png) -->

---

## Features

- **GUI mode** (default) — a Tkinter window with name fields, a replay-folder
  picker, a live progress bar, a headline win/loss summary and a per-game
  table. Wins are tinted green, losses red.
- **Per-game player stats** — each listed game shows, for both you and the
  teammate, the K/D/A (kills/deaths/assists), hero damage and healing recorded
  on the end-of-game score screen.
- **Teammate profile popup** — click any game row to open a popup (the main
  window stays uncluttered) with the teammate's preferred **roles/classes**,
  and his **win/loss % per hero** and **per map** across your shared games.
- **Signed PDF report** — export a report stamped with a numeric **validation
  code**; anyone can later re-check the PDF's integrity by uploading it back
  and entering the code (see [Report & validation](#report--validation)).
- **CLI mode** — the same scan, report generation and validation from the
  command line, for scripted use.
- **Self-bootstrapping** — on first run the script installs its dependencies
  ([`heroprotocol`](https://github.com/Blizzard/heroprotocol) for decoding,
  [`reportlab`](https://pypi.org/project/reportlab/) and
  [`pypdf`](https://pypi.org/project/pypdf/) for the report mode) automatically
  via `pip`.
- **Python 3.12+ compatible** — includes a small compatibility shim for
  `heroprotocol`, which still imports the `imp` module removed in Python 3.12.
- **Auto-detection** of the standard replay location under
  `Documents\Heroes of the Storm\Accounts` (including OneDrive-redirected
  Documents folders).
- **Honest counting** — games where two players share the same display name are
  reported as *ambiguous* and skipped instead of guessed; games where you and
  the other player were on **opposite** teams are counted separately; replays
  without a recorded result are counted as *unknown*.

---

## Requirements

| Requirement | Notes |
|---|---|
| **Windows 10/11** | Where HotS stores replays. The script also runs on Linux/macOS against a copied replay folder. |
| **Python 3.10 or newer** | 3.12/3.13 fully supported (compatibility shim included). |
| **pip** | Included with the official Python installer. |
| **Internet access on first run** | Only to auto-install the dependencies from PyPI. |

### Installing Python on Windows

If double-clicking the script opens a window that **closes immediately**, or
`python` is not recognised in the terminal, Python is missing or incorrectly
installed. Install it properly:

1. Download the latest **Python for Windows** installer:
   - Official download page: <https://www.python.org/downloads/windows/>
   - Or the general downloads page: <https://www.python.org/downloads/>
2. Run the installer and, on the **first screen**, tick **both**:
   - ☑ *Add python.exe to PATH*
   - ☑ *Install pip*

   then click *Install Now*.
3. Verify — open **Command Prompt** (`Win+R` → `cmd`) and run:

   ```bat
   python --version
   pip --version
   ```

   Both commands must print a version number.

Alternative: install "Python 3.x" from the **Microsoft Store**
(<https://apps.microsoft.com/search?query=python>), which configures PATH
automatically. The `py` launcher works too: `py hots_teammate_stats.py`.

---

## Installation

```bat
git clone https://github.com/<your-user>/hots-teammate-stats.git
cd hots-teammate-stats
```

No manual dependency step is needed — the script installs what it needs on
first run.

---

## Usage

### GUI (default)

```bat
python hots_teammate_stats.py
```

1. Fill in **Your name** and the **Teammate**'s in-game name (the `#1234`
   discriminator is optional and ignored — replays store display names without
   it).
2. Confirm the auto-detected **Replay folder** or pick one with *Browse…*
3. Press **Scan** and watch the progress bar; results appear when done.
4. **Click any game row** to open the teammate profile popup.
5. Press **Save PDF report…** to export a signed report, or **Validate PDF…**
   to verify one.

### CLI

Scan and print the report:

```bat
python hots_teammate_stats.py --me "MyName" --teammate "FriendName" --list
```

Scan and also write a signed PDF report:

```bat
python hots_teammate_stats.py --me "MyName" --teammate "FriendName" --report report.pdf
```

Validate an existing report PDF:

```bat
python hots_teammate_stats.py --validate report.pdf --code 1234-5678-9012
```

| Option | Description |
|---|---|
| `--me NAME` | Your in-game display name (required for a scan). |
| `--teammate NAME` | The teammate's display name (required for a scan). |
| `--replays PATH` | Folder scanned recursively for `.StormReplay` files. Default: auto-detect. |
| `--list` | Also print every shared game (date, map, result, heroes, per-player stats). |
| `--report OUT.pdf` | Also write a signed PDF report and print its validation code. |
| `--validate IN.pdf` | Validate an existing report PDF (use with `--code`); no scan needed. |
| `--code CODE` | The validation code to check (digits; dashes and spaces are ignored). |

The `--validate` command exits `0` when the code is valid and `2` when it is
not, so it can be used in scripts.

---

## Report & validation

The **Save PDF report…** mode produces a PDF containing the summary, the
teammate profile (roles, per-hero and per-map records) and the full per-game
table, stamped with a 12-digit **validation code** shown as `XXXX-XXXX-XXXX`.

How the code works:

- A canonical copy of the report's data is embedded inside the PDF (as a
  metadata field and as a file attachment).
- The code is a keyed hash (HMAC-SHA256) of that embedded data.
- Validation re-reads the embedded data, recomputes the code and compares it
  (ignoring dashes/spaces, in constant time) with the one you enter.

This detects tampering with the embedded data and proves the report was
produced by a holder of the tool's secret.

> **Security note.** Because the same tool both signs and validates, the
> secret has to live inside the tool. The signing core is **obfuscated** so
> the secret and algorithm are not readable in the source (or in this public
> repository), which stops casual forgery — but this is *obscurity, not
> cryptography*: anyone who runs the tool can in principle recover the secret
> from memory. If you need true unforgeability, the signing secret must live
> somewhere the verifier cannot reach (e.g. a server you control), which is a
> different design. Regenerate the secret at any time to invalidate every code
> issued by older builds.

---

## How it works

Each `.StormReplay` file is an MPQ archive. The script follows Blizzard's own
reference implementation (`heroprotocol`):

1. Reads the version-independent protocol **header** to get the replay's
   `baseBuild`.
2. Loads the matching `protocolNNNNN` decoder module (falling back to the
   latest one when that exact build is not shipped — the formats read are
   stable).
3. Decodes `replay.details` → `m_playerList` for each player's display name
   (`m_name`), team (`m_teamId`) and result (`m_result`: `1` = win,
   `2` = loss).
4. For qualifying games, decodes `replay.tracker.events` and reads the
   end-of-game `SScoreResultEvent` to pull each player's K/D/A, hero damage
   and healing.
5. Tallies every game where both players are present **and** on the same team.

Hero **roles/classes** are not stored in replays, so the tool carries a
built-in hero→role table (the HotS roster has been final since 2020) following
Blizzard's six-class system, with name normalization so punctuation and
accents (e.g. `E.T.C.`, `Lúcio`, `Lt. Morales`) match correctly.

The scan runs in a background thread in GUI mode; results are passed back to
the Tkinter main loop through a queue polled with `after()` (the thread-safe
Tkinter pattern).

---

## Limitations

- Only replays **still on disk** are counted. HotS has no public match-history
  API, so games whose replays were deleted (or played on another machine) are
  invisible to this tool.
- Matching is by **display name**. Replay details do not include the BattleTag
  discriminator, so if two different players in one match share a display name,
  that game is skipped as ambiguous rather than guessed.
- Replays recorded on a **non-English game client** store localized hero
  names; those fall into an "Unknown" role bucket. Per-hero and per-map records
  are unaffected (they group by the stored name directly).
- Replays saved before a match ended may carry no result (counted as *unknown*)
  or no score screen (their per-player stats show `?`).

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Window flashes and closes immediately | Python missing or broken. Reinstall from <https://www.python.org/downloads/windows/> with *Add python.exe to PATH* ticked, then run the script **from a terminal** so any message stays visible. |
| `'python' is not recognized ...` | PATH not set — reinstall as above, or use `py hots_teammate_stats.py`. |
| `[bootstrap] Failed to install ...` | No internet or pip broken. Run `python -m pip install heroprotocol reportlab pypdf` manually. |
| `Unsupported base build` / many unreadable replays | Update the decoder: `python -m pip install --upgrade heroprotocol`. |
| GUI does not open on Linux | Install Tk: `sudo apt install python3-tk` (Debian/Ubuntu) — or use CLI mode. |
| PDF report uses Helvetica, not Trebuchet MS | The Trebuchet MS TrueType font was not found; it is present by default on Windows but not on most Linux/macOS systems. |

---

## License and credits

- Replay decoding uses [`heroprotocol`](https://github.com/Blizzard/heroprotocol),
  © Blizzard Entertainment, released under the MIT license.
- PDF generation uses [`reportlab`](https://pypi.org/project/reportlab/) and
  [`pypdf`](https://pypi.org/project/pypdf/).
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
| HMAC | Hash-based Message Authentication Code | A keyed hash used to derive the report's validation code from its embedded data. |
| HotS | Heroes of the Storm | Blizzard's multiplayer online battle arena game, whose replays this tool analyses. |
| K/D/A | Kills / Deaths / Assists | Per-player combat record shown for each game, read from the replay's score screen. |
| MPQ | Mo'PaQ (Mike O'Brien Pack) | Blizzard's archive file format; every `.StormReplay` file is an MPQ archive. |
| PATH | — | The operating-system variable listing folders searched for executables; Python must be on it for the `python` command to work. |
| PDF | Portable Document Format | The format of the exported, validatable report. |
| pip | Pip Installs Packages | Python's package installer, used by the script to self-install its dependencies. |
| PyPI | Python Package Index | The public repository from which `pip` downloads the dependencies. |
| Tk / Tkinter | Tool Kit / Tk interface | The GUI toolkit bundled with Python's standard library, used for the window mode. |
