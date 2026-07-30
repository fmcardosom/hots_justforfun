#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hots_teammate_stats.py
======================

Heroes of the Storm — teammate win/loss analyzer (GUI + CLI).

Scans a folder of ``.StormReplay`` files and answers the question:
"How many games did I win / lose with player X on MY team?"

Architecture overview
---------------------
1. BOOTSTRAP   : At startup the script checks that its only third-party
                 dependency (``heroprotocol``, which itself pulls in
                 ``mpyq``) is installed, and pip-installs it via a
                 subprocess if missing. The script therefore runs
                 standalone on any machine with Python 3 + pip.
                 The GUI uses only Tkinter/ttk from the standard library,
                 so no extra packages are needed for it.

2. COMPAT SHIM : Blizzard's heroprotocol package still does ``import imp``,
                 a module that was removed in Python 3.12. Before importing
                 heroprotocol we register a minimal fake ``imp`` module in
                 ``sys.modules`` that implements the two functions
                 heroprotocol actually uses (``find_module`` and
                 ``load_module``) on top of ``importlib``. On Python <=3.11
                 the real ``imp`` module is used untouched.

3. DISCOVERY   : If no replay path is given, the script auto-detects the
                 standard HotS replay location on Windows:
                 ``Documents\\Heroes of the Storm\\Accounts\\...`` (including
                 the OneDrive-redirected Documents variant). All
                 ``*.StormReplay`` files are collected recursively.

4. DECODING    : Each replay is an MPQ archive. Per Blizzard's reference
                 implementation (hero_cli.py):
                   a) read the protocol header with the *latest* protocol
                      (the header format is version-independent),
                   b) extract ``m_version.m_baseBuild`` from the header,
                   c) load the matching ``protocolNNNNN`` module,
                      falling back to the latest protocol if that exact
                      build is not shipped (the ``replay.details`` format
                      is stable across builds),
                   d) decode ``replay.details`` and read ``m_playerList``.

5. ANALYSIS    : For every replay, both players (you and the teammate) are
                 located in ``m_playerList`` by display name
                 (case-insensitive; any ``#1234`` discriminator in the
                 input is stripped, because replay *details* store names
                 without it). Games where both are present AND on the same
                 team (``m_teamId``) are tallied using ``m_result``
                 (1 = win, 2 = loss, per Blizzard's protocol definition).
                 The analysis accepts an optional progress callback so the
                 GUI can display a live progress bar.

6. FRONT-ENDS  : * GUI (default, no arguments): a Tkinter window with
                   name fields, folder picker, progress bar, summary
                   panel and a per-game table. The scan runs in a
                   background thread; results are marshalled back to the
                   Tk main loop through a queue polled with ``after()``,
                   which is the thread-safe Tkinter pattern.
                 * CLI (any argument present): same behaviour as the
                   original command-line version, for scripted use.

Usage
-----
    python hots_teammate_stats.py
        -> opens the GUI.

    python hots_teammate_stats.py --me "MyName" --teammate "FriendName" --list
        -> command-line mode, prints the report to stdout.

Limitations
-----------
* Only replays still on disk are counted — HotS has no public match
  history API, so deleted replays are gone.
* Matching is by display name; if two different players in the same match
  share a display name the game is reported as ambiguous and skipped.
"""

# ---------------------------------------------------------------------------
# 1. DEPENDENCY BOOTSTRAP
#    Ensure 'heroprotocol' (and transitively 'mpyq') is available before any
#    other import that needs it. Uses a pip subprocess so the script is
#    fully standalone.
# ---------------------------------------------------------------------------
import importlib.util
import subprocess
import sys


def _bootstrap_dependencies() -> None:
    """Install any missing third-party packages via pip.

    Checks each required distribution with importlib and, when absent,
    invokes ``pip install`` in a subprocess using the same interpreter
    that is running this script. Aborts with a clear message if the
    installation fails (e.g. no network access).
    """
    # (import_name, pip_name) pairs — only one real dependency here;
    # 'mpyq' is declared explicitly as a safety net even though it is
    # normally installed automatically as a dependency of heroprotocol.
    required = [
        ("heroprotocol", "heroprotocol"),
        ("mpyq", "mpyq"),
    ]
    for import_name, pip_name in required:
        if importlib.util.find_spec(import_name) is None:
            print(f"[bootstrap] Installing missing dependency: {pip_name} ...")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", pip_name],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                sys.stderr.write(result.stdout + result.stderr)
                sys.exit(
                    f"[bootstrap] Failed to install '{pip_name}'. "
                    "Install it manually with: pip install " + pip_name
                )


_bootstrap_dependencies()

# ---------------------------------------------------------------------------
# 2. PYTHON 3.12+ COMPATIBILITY SHIM FOR heroprotocol
#    heroprotocol/versions/__init__.py does 'import imp' and calls exactly
#    two functions: imp.find_module() and imp.load_module(). The 'imp'
#    module was removed in Python 3.12, so we register a minimal
#    replacement in sys.modules BEFORE heroprotocol is imported.
# ---------------------------------------------------------------------------
import os
import types


def _install_imp_shim() -> None:
    """Register a minimal 'imp' stand-in on Python 3.12+.

    Only the subset of the old imp API that heroprotocol uses is
    implemented, built on top of importlib. On Python <= 3.11 (where the
    real 'imp' still exists) this function does nothing.
    """
    if sys.version_info < (3, 12) or "imp" in sys.modules:
        return  # Real 'imp' is available, or a shim is already in place.

    shim = types.ModuleType("imp")

    def find_module(name, paths):
        """Locate '<name>.py' in the given search paths.

        Returns the (file_object, pathname, description) triple that the
        old imp.find_module() returned, which heroprotocol passes straight
        into load_module().
        """
        for base in paths:
            candidate = os.path.join(base, name + ".py")
            if os.path.isfile(candidate):
                return (open(candidate, "rb"), candidate, (".py", "rb", 1))
        raise ImportError(f"Cannot find module {name!r} in {paths!r}")

    def load_module(name, fp, pathname, description):
        """Load a module from a source file path using importlib.

        Mirrors the old imp.load_module() contract closely enough for
        heroprotocol: the module is executed, registered in sys.modules
        and returned. The file object is closed by the caller.
        """
        spec = importlib.util.spec_from_file_location(name, pathname)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # Register first (heroprotocol caches by name).
        spec.loader.exec_module(module)
        return module

    shim.find_module = find_module
    shim.load_module = load_module
    sys.modules["imp"] = shim


_install_imp_shim()

# ---------------------------------------------------------------------------
# Standard-library + third-party imports (safe now that bootstrap/shim ran)
# ---------------------------------------------------------------------------
import argparse
import datetime
import glob
import queue
import threading

import mpyq  # MPQ archive reader — .StormReplay files are MPQ archives.
from heroprotocol.versions import build as protocol_for_build
from heroprotocol.versions import latest as latest_protocol

# ---------------------------------------------------------------------------
# Constants — values verified against Blizzard's protocol definitions
# (protocol91756.py) and reference CLI (hero_cli.py).
# ---------------------------------------------------------------------------
RESULT_WIN = 1   # m_result value meaning the player's team won.
RESULT_LOSS = 2  # m_result value meaning the player's team lost.

# Windows epoch offset: m_timeUTC in replay details is a FILETIME-style
# timestamp — 100-nanosecond intervals since 1601-01-01. To convert to a
# Unix timestamp we subtract the number of such intervals between
# 1601-01-01 and 1970-01-01 and divide by 10^7.
_FILETIME_EPOCH_DELTA = 116444736000000000  # 100ns units between 1601 and 1970.


# ---------------------------------------------------------------------------
# 3. REPLAY DISCOVERY
# ---------------------------------------------------------------------------
def find_replay_folder() -> str | None:
    """Auto-detect the standard HotS replay folder on Windows.

    Checks both the classic Documents location and the OneDrive-redirected
    Documents location. Returns the first 'Accounts' folder that exists,
    or None if nothing is found (the user then picks a folder manually in
    the GUI or passes --replays on the CLI).
    """
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "Documents", "Heroes of the Storm", "Accounts"),
        os.path.join(home, "OneDrive", "Documents", "Heroes of the Storm", "Accounts"),
    ]
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


def collect_replays(root: str) -> list[str]:
    """Recursively collect every .StormReplay file under *root*.

    The standard layout is Accounts/<id>/<region>-Hero-.../Replays/Multiplayer,
    but a recursive glob keeps this robust to custom folder layouts too.
    """
    pattern = os.path.join(root, "**", "*.StormReplay")
    return sorted(glob.glob(pattern, recursive=True))


# ---------------------------------------------------------------------------
# 4. REPLAY DECODING
# ---------------------------------------------------------------------------
def decode_details(replay_path: str) -> dict:
    """Decode and return the 'replay.details' structure of one replay.

    Follows Blizzard's reference sequence:
      header (latest protocol) -> baseBuild -> exact protocol -> details.
    Falls back to the latest protocol module when the exact build is not
    shipped with the installed heroprotocol version — the details format
    is stable, so this fallback is safe for our fields.

    Raises any decoding exception to the caller, which counts the replay
    as unreadable instead of crashing the whole scan.
    """
    archive = mpyq.MPQArchive(replay_path)

    # The user-data header is version-independent; any protocol reads it.
    header_contents = archive.header["user_data_header"]["content"]
    header = latest_protocol().decode_replay_header(header_contents)

    # Pick the protocol module matching the replay's build, if available.
    base_build = header["m_version"]["m_baseBuild"]
    try:
        protocol = protocol_for_build(base_build)
    except Exception:
        protocol = latest_protocol()  # Safe fallback for details decoding.

    details_contents = archive.read_file("replay.details")
    return protocol.decode_replay_details(details_contents)


def _to_text(value) -> str:
    """Decode a bytes field from the replay (names, map titles) to str.

    Replay strings are UTF-8 encoded bytes; errors='replace' guards
    against the occasional malformed name without raising.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _details_datetime(details: dict) -> datetime.datetime | None:
    """Convert the replay's m_timeUTC FILETIME timestamp to a datetime.

    Returns None when the field is missing or malformed, so the caller
    can still report the game without a date.
    """
    filetime = details.get("m_timeUTC")
    if not isinstance(filetime, int):
        return None
    unix_seconds = (filetime - _FILETIME_EPOCH_DELTA) / 10_000_000
    try:
        return datetime.datetime.fromtimestamp(unix_seconds, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 5. ANALYSIS
# ---------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """Normalize a player name for comparison.

    Replay *details* store display names WITHOUT the BattleTag
    discriminator, so any '#1234' suffix the user typed is stripped, and
    comparison is case-insensitive (casefold handles non-ASCII names).
    """
    return name.split("#", 1)[0].strip().casefold()


def find_player(player_list: list[dict], wanted_norm: str) -> tuple[dict | None, bool]:
    """Find a player entry by normalized display name.

    Returns (entry, ambiguous):
      * entry     — the matching m_playerList entry, or None if absent;
      * ambiguous — True when MORE than one player in this match carries
                    the same display name, in which case the caller skips
                    the game rather than guessing which person it was.
    """
    matches = [
        p for p in player_list
        if normalize_name(_to_text(p.get("m_name", b""))) == wanted_norm
    ]
    if len(matches) > 1:
        return None, True
    return (matches[0] if matches else None), False


def analyze(replays: list[str], me: str, teammate: str,
            progress_cb=None) -> dict:
    """Scan all replays and build the win/loss statistics structure.

    Parameters
    ----------
    replays     : list of .StormReplay file paths to scan.
    me          : the user's display name.
    teammate    : the teammate's display name.
    progress_cb : optional callable(done, total) invoked after every
                  replay — used by the GUI to drive the progress bar.
                  Must be cheap and thread-safe (the GUI passes a
                  queue.put, never a direct widget call).

    Returns a dict with counters and a per-game list:
      games      — list of dicts (date, map, result, my_hero, mate_hero)
      wins/losses/unknown — tallies for games together on the same team
      opposing   — games where both appear but on OPPOSITE teams
      ambiguous  — games skipped due to duplicate display names
      errors     — replays that could not be decoded
      scanned    — total replay files processed
    """
    me_norm = normalize_name(me)
    mate_norm = normalize_name(teammate)
    total = len(replays)

    stats = {
        "games": [],
        "wins": 0,
        "losses": 0,
        "unknown": 0,
        "opposing": 0,
        "ambiguous": 0,
        "errors": 0,
        "scanned": 0,
    }

    for path in replays:
        stats["scanned"] += 1
        # Report progress first so the bar moves even on unreadable files.
        if progress_cb is not None:
            progress_cb(stats["scanned"], total)

        try:
            details = decode_details(path)
        except Exception:
            stats["errors"] += 1  # Corrupt/partial replay — skip silently.
            continue

        players = details.get("m_playerList") or []

        my_entry, my_dup = find_player(players, me_norm)
        mate_entry, mate_dup = find_player(players, mate_norm)

        # Duplicate display names inside one match: cannot attribute
        # reliably, so count as ambiguous and move on.
        if my_dup or mate_dup:
            stats["ambiguous"] += 1
            continue

        # Either player absent from this match — not a shared game.
        if my_entry is None or mate_entry is None:
            continue

        # Both present but on different teams — track separately; the
        # user asked about games together, but this is worth surfacing.
        if my_entry.get("m_teamId") != mate_entry.get("m_teamId"):
            stats["opposing"] += 1
            continue

        # Same team: classify by MY result field (1 = win, 2 = loss).
        result = my_entry.get("m_result")
        if result == RESULT_WIN:
            outcome = "WIN"
            stats["wins"] += 1
        elif result == RESULT_LOSS:
            outcome = "LOSS"
            stats["losses"] += 1
        else:
            outcome = "?"
            stats["unknown"] += 1  # e.g. replay saved before game ended.

        stats["games"].append({
            "date": _details_datetime(details),
            "map": _to_text(details.get("m_title", b"?")),
            "result": outcome,
            "my_hero": _to_text(my_entry.get("m_hero", b"?")),
            "mate_hero": _to_text(mate_entry.get("m_hero", b"?")),
            "file": os.path.basename(path),
        })

    return stats


def sorted_games(stats: dict) -> list[dict]:
    """Return the per-game list sorted chronologically.

    Games without a decodable date (None) sort last via a max sentinel,
    so both the CLI table and the GUI table share the same ordering.
    """
    sentinel = datetime.datetime.max.replace(tzinfo=datetime.timezone.utc)
    return sorted(stats["games"], key=lambda g: g["date"] or sentinel)


# ---------------------------------------------------------------------------
# 6a. CLI FRONT-END
# ---------------------------------------------------------------------------
def print_report(stats: dict, me: str, teammate: str, show_list: bool) -> None:
    """Print the summary (and optional per-game list) to stdout."""
    together = stats["wins"] + stats["losses"] + stats["unknown"]
    decided = stats["wins"] + stats["losses"]
    winrate = (stats["wins"] / decided * 100) if decided else 0.0

    print()
    print(f"Replays scanned      : {stats['scanned']}")
    print(f"Unreadable replays   : {stats['errors']}")
    print(f"Ambiguous name games : {stats['ambiguous']}")
    print()
    print(f"Games with '{teammate}' on {me}'s team : {together}")
    print(f"  Wins    : {stats['wins']}")
    print(f"  Losses  : {stats['losses']}")
    if stats["unknown"]:
        print(f"  Unknown : {stats['unknown']} (no result recorded)")
    print(f"  Win rate: {winrate:.1f}%  ({stats['wins']}-{stats['losses']})")
    if stats["opposing"]:
        print(f"Games on OPPOSITE teams: {stats['opposing']}")

    # Optional chronological per-game breakdown.
    if show_list and stats["games"]:
        print()
        print(f"{'Date':<17} {'Result':<6} {'Map':<28} {'You':<14} {teammate}")
        print("-" * 80)
        for game in sorted_games(stats):
            date_str = game["date"].strftime("%Y-%m-%d %H:%M") if game["date"] else "?"
            print(f"{date_str:<17} {game['result']:<6} {game['map']:<28} "
                  f"{game['my_hero']:<14} {game['mate_hero']}")


def run_cli() -> None:
    """Parse CLI arguments, locate replays, run the analysis, print report."""
    parser = argparse.ArgumentParser(
        description="Count wins/losses with a specific teammate from local "
                    "Heroes of the Storm .StormReplay files. "
                    "Run with no arguments to open the GUI.",
    )
    parser.add_argument("--me", required=True,
                        help="Your in-game display name (the #1234 part, if "
                             "typed, is ignored — replays store names without it).")
    parser.add_argument("--teammate", required=True,
                        help="The teammate's in-game display name.")
    parser.add_argument("--replays", default=None,
                        help="Folder to scan recursively for .StormReplay files. "
                             "Default: auto-detect the standard HotS Accounts "
                             "folder under Documents.")
    parser.add_argument("--list", action="store_true",
                        help="Also print every shared game (date, map, result, heroes).")
    args = parser.parse_args()

    # Resolve the replay folder: explicit flag wins, otherwise auto-detect.
    root = args.replays or find_replay_folder()
    if not root or not os.path.isdir(root):
        sys.exit("Could not locate the HotS replay folder automatically. "
                 "Pass it explicitly with --replays "
                 '"...\\Heroes of the Storm\\Accounts".')

    replays = collect_replays(root)
    if not replays:
        sys.exit(f"No .StormReplay files found under: {root}")

    print(f"Scanning {len(replays)} replays under: {root}")
    stats = analyze(replays, args.me, args.teammate)
    print_report(stats, args.me, args.teammate, args.list)


# ---------------------------------------------------------------------------
# 6b. GUI FRONT-END (Tkinter — standard library only, no extra dependencies)
# ---------------------------------------------------------------------------
# Tkinter is imported lazily inside run_gui() so the CLI mode still works
# on headless systems where Tk is not available.

class HotsStatsGUI:
    """Tkinter front-end for the teammate analyzer.

    Layout (top to bottom):
      * input row      — "Your name" and "Teammate" entry fields;
      * folder row     — replay folder entry + Browse button (pre-filled
                         by auto-detection when possible);
      * action row     — Scan button + determinate progress bar + status;
      * summary panel  — games together, wins, losses, win rate, and the
                         secondary counters (opposite teams, ambiguous,
                         unreadable);
      * results table  — ttk.Treeview with one row per shared game
                         (date, result, map, your hero, teammate's hero),
                         wins tinted green and losses tinted red.

    Threading model
    ---------------
    Decoding hundreds of MPQ archives takes noticeable time, so the scan
    runs in a daemon worker thread. Tkinter widgets are NOT thread-safe,
    therefore the worker never touches them: it pushes ('progress', ...)
    and ('done'/'error', ...) tuples into a queue.Queue, and the Tk main
    loop polls that queue every 100 ms with ``root.after()`` — the
    canonical thread-safe Tkinter pattern.
    """

    #: Polling interval (ms) for the worker->GUI message queue.
    POLL_MS = 100

    def __init__(self, root):
        """Build all widgets and pre-fill the replay folder if detectable."""
        # Local import names bound in run_gui(); kept as attributes for reuse.
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.ttk = ttk

        self.root = root
        root.title("HotS Teammate Stats")
        root.geometry("860x600")
        root.minsize(720, 480)

        # Queue carrying messages from the worker thread to the Tk loop.
        self.msg_queue: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None

        # ------------------------------------------------------------------
        # Input row: player names
        # ------------------------------------------------------------------
        frm_names = ttk.Frame(root, padding=(10, 10, 10, 0))
        frm_names.pack(fill="x")

        ttk.Label(frm_names, text="Your name:").grid(row=0, column=0, sticky="w")
        self.var_me = tk.StringVar()
        ttk.Entry(frm_names, textvariable=self.var_me, width=24).grid(
            row=0, column=1, sticky="w", padx=(4, 20))

        ttk.Label(frm_names, text="Teammate:").grid(row=0, column=2, sticky="w")
        self.var_mate = tk.StringVar()
        ttk.Entry(frm_names, textvariable=self.var_mate, width=24).grid(
            row=0, column=3, sticky="w", padx=(4, 0))

        # ------------------------------------------------------------------
        # Folder row: replay folder entry + Browse
        # ------------------------------------------------------------------
        frm_folder = ttk.Frame(root, padding=(10, 8, 10, 0))
        frm_folder.pack(fill="x")

        ttk.Label(frm_folder, text="Replay folder:").pack(side="left")
        # Pre-fill with the auto-detected Accounts folder when available.
        self.var_folder = tk.StringVar(value=find_replay_folder() or "")
        ttk.Entry(frm_folder, textvariable=self.var_folder).pack(
            side="left", fill="x", expand=True, padx=4)
        ttk.Button(frm_folder, text="Browse...", command=self.on_browse).pack(
            side="left")

        # ------------------------------------------------------------------
        # Action row: Scan button, progress bar, status label
        # ------------------------------------------------------------------
        frm_action = ttk.Frame(root, padding=(10, 8, 10, 0))
        frm_action.pack(fill="x")

        self.btn_scan = ttk.Button(frm_action, text="Scan", command=self.on_scan)
        self.btn_scan.pack(side="left")

        self.progress = ttk.Progressbar(frm_action, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        self.var_status = tk.StringVar(value="Ready.")
        ttk.Label(frm_action, textvariable=self.var_status, width=28,
                  anchor="e").pack(side="left")

        # ------------------------------------------------------------------
        # Summary panel: headline record + secondary counters
        # ------------------------------------------------------------------
        frm_summary = ttk.LabelFrame(root, text="Summary", padding=10)
        frm_summary.pack(fill="x", padx=10, pady=8)

        self.var_headline = tk.StringVar(value="No scan yet.")
        ttk.Label(frm_summary, textvariable=self.var_headline,
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w")

        self.var_secondary = tk.StringVar(value="")
        ttk.Label(frm_summary, textvariable=self.var_secondary).pack(anchor="w")

        # ------------------------------------------------------------------
        # Results table: one row per shared game
        # ------------------------------------------------------------------
        frm_table = ttk.Frame(root, padding=(10, 0, 10, 10))
        frm_table.pack(fill="both", expand=True)

        columns = ("date", "result", "map", "my_hero", "mate_hero")
        self.tree = ttk.Treeview(frm_table, columns=columns, show="headings")
        headings = {
            "date": ("Date", 130),
            "result": ("Result", 60),
            "map": ("Map", 220),
            "my_hero": ("You", 140),
            "mate_hero": ("Teammate", 140),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")

        # Row background tints: soft green for wins, soft red for losses.
        self.tree.tag_configure("win", background="#e2f0e2")
        self.tree.tag_configure("loss", background="#f5dddd")

        # Vertical scrollbar wired to the tree.
        scroll = ttk.Scrollbar(frm_table, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        # Start the queue polling loop (runs for the lifetime of the app).
        self.root.after(self.POLL_MS, self.poll_queue)

    # ----------------------------------------------------------------------
    # Event handlers
    # ----------------------------------------------------------------------
    def on_browse(self) -> None:
        """Open a folder picker and store the chosen replay folder."""
        from tkinter import filedialog
        chosen = filedialog.askdirectory(
            title="Select the Heroes of the Storm replay folder",
            initialdir=self.var_folder.get() or os.path.expanduser("~"),
        )
        if chosen:
            self.var_folder.set(chosen)

    def on_scan(self) -> None:
        """Validate inputs and launch the background scan thread."""
        from tkinter import messagebox

        # Ignore clicks while a scan is already running.
        if self.worker is not None and self.worker.is_alive():
            return

        me = self.var_me.get().strip()
        mate = self.var_mate.get().strip()
        folder = self.var_folder.get().strip()

        # Input validation with user-friendly dialogs.
        if not me or not mate:
            messagebox.showwarning("Missing name",
                                   "Fill in both your name and the teammate's name.")
            return
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("Missing folder",
                                   "Select a valid replay folder.")
            return

        replays = collect_replays(folder)
        if not replays:
            messagebox.showinfo("No replays",
                                f"No .StormReplay files found under:\n{folder}")
            return

        # Reset UI state for a fresh scan.
        self.tree.delete(*self.tree.get_children())
        self.var_headline.set("Scanning...")
        self.var_secondary.set("")
        self.progress.configure(maximum=len(replays), value=0)
        self.var_status.set(f"0 / {len(replays)} replays")
        self.btn_scan.state(["disabled"])

        # Launch the worker. Daemon=True so it never blocks app exit.
        self.worker = threading.Thread(
            target=self._scan_worker, args=(replays, me, mate), daemon=True)
        self.worker.start()

    # ----------------------------------------------------------------------
    # Worker thread + queue plumbing
    # ----------------------------------------------------------------------
    def _scan_worker(self, replays: list[str], me: str, mate: str) -> None:
        """Background thread: run analyze() and post results to the queue.

        Never touches Tkinter widgets directly — all communication goes
        through self.msg_queue.
        """
        try:
            stats = analyze(
                replays, me, mate,
                # Progress callback: cheap queue.put, safe from any thread.
                progress_cb=lambda done, total:
                    self.msg_queue.put(("progress", done, total)),
            )
            self.msg_queue.put(("done", stats, me, mate))
        except Exception as exc:  # Defensive: surface unexpected failures.
            self.msg_queue.put(("error", str(exc)))

    def poll_queue(self) -> None:
        """Drain worker messages inside the Tk main loop (thread-safe).

        Re-schedules itself every POLL_MS milliseconds for the lifetime
        of the window.
        """
        try:
            while True:  # Drain everything currently queued.
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "progress":
                    _, done, total = msg
                    self.progress.configure(value=done)
                    self.var_status.set(f"{done} / {total} replays")
                elif kind == "done":
                    _, stats, me, mate = msg
                    self.show_results(stats, me, mate)
                elif kind == "error":
                    self.show_error(msg[1])
        except queue.Empty:
            pass  # Nothing more to process this tick.
        self.root.after(self.POLL_MS, self.poll_queue)

    # ----------------------------------------------------------------------
    # Result rendering
    # ----------------------------------------------------------------------
    def show_results(self, stats: dict, me: str, mate: str) -> None:
        """Populate the summary panel and the per-game table."""
        together = stats["wins"] + stats["losses"] + stats["unknown"]
        decided = stats["wins"] + stats["losses"]
        winrate = (stats["wins"] / decided * 100) if decided else 0.0

        # Headline: the number the user actually asked for.
        self.var_headline.set(
            f"With '{mate}' on your team: {together} games — "
            f"{stats['wins']} W / {stats['losses']} L "
            f"({winrate:.1f}% win rate)"
        )

        # Secondary counters: context and data-quality indicators.
        parts = [f"Scanned {stats['scanned']} replays"]
        if stats["opposing"]:
            parts.append(f"{stats['opposing']} on opposite teams")
        if stats["unknown"]:
            parts.append(f"{stats['unknown']} without result")
        if stats["ambiguous"]:
            parts.append(f"{stats['ambiguous']} ambiguous (duplicate names)")
        if stats["errors"]:
            parts.append(f"{stats['errors']} unreadable")
        self.var_secondary.set(" | ".join(parts))

        # Table rows, chronological, colored by outcome.
        for game in sorted_games(stats):
            date_str = (game["date"].strftime("%Y-%m-%d %H:%M")
                        if game["date"] else "?")
            tag = ("win" if game["result"] == "WIN"
                   else "loss" if game["result"] == "LOSS" else "")
            self.tree.insert("", "end", values=(
                date_str, game["result"], game["map"],
                game["my_hero"], game["mate_hero"],
            ), tags=(tag,))

        self.var_status.set("Done.")
        self.btn_scan.state(["!disabled"])

    def show_error(self, message: str) -> None:
        """Report a fatal worker error and re-enable the Scan button."""
        from tkinter import messagebox
        self.btn_scan.state(["!disabled"])
        self.var_status.set("Error.")
        messagebox.showerror("Scan failed", message)


def run_gui() -> None:
    """Create the Tk root window and start the GUI main loop.

    Tkinter is imported here (not at module level) so CLI mode keeps
    working on systems without a display / Tk installation.
    """
    try:
        import tkinter as tk  # noqa: F401 — availability check + root creation.
    except ImportError:
        sys.exit("Tkinter is not available in this Python installation. "
                 "Use the CLI mode instead: --me NAME --teammate NAME")

    root = tk.Tk()
    HotsStatsGUI(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# ENTRY POINT — GUI by default, CLI when any argument is given
# ---------------------------------------------------------------------------
def main() -> None:
    """Dispatch to the GUI (no arguments) or the CLI (any argument)."""
    if len(sys.argv) > 1:
        run_cli()
    else:
        run_gui()


if __name__ == "__main__":
    main()
