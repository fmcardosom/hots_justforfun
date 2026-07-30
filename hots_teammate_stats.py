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
    # (import_name, pip_name) pairs. heroprotocol is the core replay
    # decoder ('mpyq' is its own dependency, declared as a safety net);
    # reportlab + pypdf back the PDF report / validation mode.
    required = [
        ("heroprotocol", "heroprotocol"),
        ("mpyq", "mpyq"),
        ("reportlab", "reportlab"),
        ("pypdf", "pypdf"),
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
def _open_replay(replay_path: str):
    """Open a replay archive and select its protocol decoder.

    Follows Blizzard's reference sequence:
      header (latest protocol) -> baseBuild -> exact protocol module.
    Falls back to the latest protocol module when the exact build is not
    shipped with the installed heroprotocol version — the formats we read
    (details, tracker events) are stable, so this fallback is safe.

    Returns (archive, protocol) for the caller to read sections with.
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
        protocol = latest_protocol()  # Safe fallback.

    return archive, protocol


def decode_details(replay_path: str) -> dict:
    """Decode and return the 'replay.details' structure of one replay.

    Raises any decoding exception to the caller, which counts the replay
    as unreadable instead of crashing the whole scan.
    """
    archive, protocol = _open_replay(replay_path)
    details_contents = archive.read_file("replay.details")
    return protocol.decode_replay_details(details_contents)


# Score stat names to extract from the end-of-game score screen, exactly
# as they appear in the SScoreResultEvent instance list (verified against
# protocol91756.py: m_instanceList -> {m_name, m_values}).
SCORE_STATS = ("SoloKill", "Deaths", "Assists", "HeroDamage",
               "SiegeDamage", "Healing")


def decode_player_score(replay_path: str, slots: set[int]) -> dict:
    """Extract end-of-game score stats for specific player slots.

    Decodes the replay's tracker events and reads the (last)
    'NNet.Replay.Tracker.SScoreResultEvent' — the event that carries the
    end-of-game score screen. Its structure, verified against Blizzard's
    protocol definition, is:

        m_instanceList : [ { m_name  : stat name (bytes, e.g. b'Deaths'),
                             m_values: [ per-slot list of
                                         { m_value, m_time } ] } ]

    Player slots correspond to 'm_workingSetSlotId' in the details
    player list. For each requested slot the LAST recorded m_value of
    each stat in SCORE_STATS is returned.

    This is noticeably slower than reading details (tracker events are
    large), so callers invoke it only for games that already qualified
    (both tracked players on the same team).

    Returns {slot: {stat_name: int}} — slots or stats missing from the
    event are simply absent; callers must handle gaps.
    """
    archive, protocol = _open_replay(replay_path)
    contents = archive.read_file("replay.tracker.events")

    # Keep only the last score event seen (the end-of-game one).
    score_event = None
    for event in protocol.decode_replay_tracker_events(contents):
        if event.get("_event") == "NNet.Replay.Tracker.SScoreResultEvent":
            score_event = event

    result: dict = {slot: {} for slot in slots}
    if score_event is None:
        return result  # Very old/partial replay without a score screen.

    for instance in score_event.get("m_instanceList") or []:
        name = _to_text(instance.get("m_name", b""))
        if name not in SCORE_STATS:
            continue  # Skip the dozens of stats we don't display.
        values = instance.get("m_values") or []
        for slot in slots:
            # m_values is indexed by working-set slot; each entry is a
            # list of {m_value, m_time} samples — take the final value.
            if 0 <= slot < len(values) and values[slot]:
                result[slot][name] = values[slot][-1].get("m_value", 0)

    return result


def _fmt_big(value) -> str:
    """Format a large stat number compactly for tables (e.g. 45.2k).

    Non-numeric or missing values render as '?' so table alignment
    survives replays without score data.
    """
    if not isinstance(value, (int, float)):
        return "?"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(int(value))


def _fmt_kda(score: dict) -> str:
    """Format kills/deaths/assists as 'K/D/A' from a slot's stat dict.

    Uses SoloKill (individual kills), Deaths and Assists; any missing
    component renders as '?'.
    """
    def piece(name):
        """Render one KDA component, or '?' when the stat is missing."""
        value = score.get(name)
        return str(value) if isinstance(value, int) else "?"
    return f"{piece('SoloKill')}/{piece('Deaths')}/{piece('Assists')}"


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

        # --- Per-player score stats (KDA, damage, healing) --------------
        # Slots come from m_workingSetSlotId; fall back to the player's
        # index in m_playerList for very old replays where it is absent.
        my_slot = my_entry.get("m_workingSetSlotId")
        mate_slot = mate_entry.get("m_workingSetSlotId")
        if not isinstance(my_slot, int):
            my_slot = players.index(my_entry)
        if not isinstance(mate_slot, int):
            mate_slot = players.index(mate_entry)

        # Tracker events are big — only decoded for qualifying games, and
        # any failure degrades to '?' stats instead of losing the game row.
        try:
            score = decode_player_score(path, {my_slot, mate_slot})
        except Exception:
            score = {my_slot: {}, mate_slot: {}}
        my_score = score.get(my_slot, {})
        mate_score = score.get(mate_slot, {})

        stats["games"].append({
            "date": _details_datetime(details),
            "map": _to_text(details.get("m_title", b"?")),
            "result": outcome,
            "my_hero": _to_text(my_entry.get("m_hero", b"?")),
            "mate_hero": _to_text(mate_entry.get("m_hero", b"?")),
            # Formatted per-player end-of-game stats for the tables.
            "my_kda": _fmt_kda(my_score),
            "my_dmg": _fmt_big(my_score.get("HeroDamage")),
            "my_heal": _fmt_big(my_score.get("Healing")),
            "mate_kda": _fmt_kda(mate_score),
            "mate_dmg": _fmt_big(mate_score.get("HeroDamage")),
            "mate_heal": _fmt_big(mate_score.get("Healing")),
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
# 5b. TEAMMATE PROFILE (roles, per-hero and per-map records)
# ---------------------------------------------------------------------------
# Replays store only hero NAMES, not their class/role, so the role
# breakdown needs a static hero->role table. The Heroes of the Storm
# roster is final (no new heroes since Hogger, December 2020), which
# makes a hard-coded table safe. Roles follow Blizzard's official
# six-class system.
#
# CAVEAT: replay 'details' store hero names localized to the game client
# of whoever recorded the replay. Names from non-English clients will not
# match this table and fall into the 'Unknown' bucket — the per-hero and
# per-map records are unaffected (they group by the stored name as-is).
HERO_ROLES = {
    "Tank": (
        "Anub'arak", "Arthas", "Blaze", "Cho", "Diablo", "E.T.C.",
        "Garrosh", "Johanna", "Mal'Ganis", "Mei", "Muradin", "Stitches",
        "Tyrael",
    ),
    "Bruiser": (
        "Artanis", "Chen", "D.Va", "Deathwing", "Dehaka", "Gazlowe",
        "Hogger", "Imperius", "Leoric", "Malthael", "Ragnaros", "Rexxar",
        "Sonya", "Thrall", "Varian", "Xul", "Yrel",
    ),
    "Healer": (
        "Alexstrasza", "Ana", "Anduin", "Auriel", "Brightwing", "Deckard",
        "Kharazim", "Li Li", "Lt. Morales", "Lúcio", "Malfurion", "Rehgar",
        "Stukov", "Tyrande", "Uther", "Whitemane",
    ),
    "Support": (
        "Abathur", "Medivh", "The Lost Vikings", "Zarya",
    ),
    "Melee Assassin": (
        "Alarak", "Illidan", "Kerrigan", "Maiev", "Murky", "Qhira",
        "Samuro", "The Butcher", "Valeera", "Zeratul",
    ),
    "Ranged Assassin": (
        "Azmodan", "Cassia", "Chromie", "Falstad", "Fenix", "Gall",
        "Genji", "Greymane", "Gul'dan", "Hanzo", "Jaina", "Junkrat",
        "Kael'thas", "Kel'Thuzad", "Li-Ming", "Lunara", "Mephisto",
        "Nazeebo", "Nova", "Orphea", "Probius", "Raynor", "Sgt. Hammer",
        "Sylvanas", "Tassadar", "Tracer", "Tychus", "Valla", "Zagara",
        "Zul'jin",
    ),
}


def _normalize_hero(name: str) -> str:
    """Normalize a hero name for role lookup.

    Lower-cases, strips accents (Lúcio -> lucio) and removes every
    non-alphanumeric character (E.T.C. -> etc, Lt. Morales -> ltmorales),
    so cosmetic punctuation differences never break the match.
    """
    import unicodedata
    decomposed = unicodedata.normalize("NFD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c for c in stripped.lower() if c.isalnum())


# Reverse lookup built once at import time: normalized hero name -> role.
_ROLE_BY_HERO = {
    _normalize_hero(hero): role
    for role, heroes in HERO_ROLES.items()
    for hero in heroes
}


def hero_role(hero_name: str) -> str:
    """Return the official class of a hero, or 'Unknown' when the name
    is not in the table (e.g. localized names from non-English clients).
    """
    return _ROLE_BY_HERO.get(_normalize_hero(hero_name), "Unknown")


def build_teammate_profile(games: list[dict]) -> dict:
    """Aggregate the teammate's play profile across the shared games.

    Consumes the per-game list produced by analyze() and returns three
    breakdowns about the TEAMMATE (the 'mate_*' fields):

      roles  — [(role, games, pct_of_all_games)] : which classes he
               likes to play; counts every shared game (result known or
               not, since the hero pick is known either way).
      heroes — [(hero, wins, losses, win_pct)]   : his record on each
               hero in your shared games; only decided games (WIN/LOSS)
               enter the win percentage.
      maps   — [(map, wins, losses, win_pct)]    : same record per map.

    All three lists are sorted by games played (desc), then name, so the
    most significant rows come first in the popup.
    """
    role_counts: dict = {}
    hero_records: dict = {}   # hero -> [wins, losses]
    map_records: dict = {}    # map  -> [wins, losses]

    for game in games:
        # Role preference counts every game — the pick is always known.
        role = hero_role(game["mate_hero"])
        role_counts[role] = role_counts.get(role, 0) + 1

        # Win/loss records only use decided games.
        if game["result"] not in ("WIN", "LOSS"):
            continue
        won = 1 if game["result"] == "WIN" else 0
        hero_records.setdefault(game["mate_hero"], [0, 0])[0 if won else 1] += 1
        map_records.setdefault(game["map"], [0, 0])[0 if won else 1] += 1

    total_games = len(games) or 1  # Guard the percentage division.

    def record_rows(records: dict) -> list[tuple]:
        """Turn a {name: [wins, losses]} dict into sorted result rows."""
        rows = []
        for name, (wins, losses) in records.items():
            decided = wins + losses
            pct = (wins / decided * 100) if decided else 0.0
            rows.append((name, wins, losses, pct))
        # Most-played first, alphabetical tiebreak.
        rows.sort(key=lambda r: (-(r[1] + r[2]), r[0]))
        return rows

    roles = [
        (role, count, count / total_games * 100)
        for role, count in sorted(role_counts.items(),
                                  key=lambda item: (-item[1], item[0]))
    ]

    return {
        "roles": roles,
        "heroes": record_rows(hero_records),
        "maps": record_rows(map_records),
    }


# ---------------------------------------------------------------------------
# 5c. PDF REPORT + NUMERIC VALIDATION CODE
# ---------------------------------------------------------------------------
# This mode turns a completed scan into a shareable PDF report stamped with
# a numeric validation code, and can later re-validate such a PDF: upload it
# back, type the code, and the tool confirms whether the code matches the
# report's embedded data.
#
# HOW THE CODE WORKS
#   * A CANONICAL PAYLOAD (stable JSON of the report's essential facts) is
#     embedded inside the PDF, both as a custom metadata key and as a file
#     attachment (redundant channels, since some PDF tools strip one).
#   * The numeric code is a keyed hash (HMAC-SHA256) of that payload,
#     derived by the SEALED CORE below. Validation re-reads the payload,
#     recomputes the code and compares.
#   * This detects tampering with the embedded data and proves the report
#     was produced by a holder of the tool's secret. Because the same tool
#     signs and validates, the secret lives in the tool; the sealed-core
#     obfuscation only hides it from casual inspection (see its note). It
#     is NOT unforgeable against someone who runs the tool.
#
# THE SEALED CORE (obfuscated)
#   `_SEALED_BLOB` is base64( XOR( zlib(sealed source) ) ). `_load_sealed()`
#   reverses that and exec()s the source in an isolated namespace, exposing
#   `derive_code()` and `codes_match()`. The signing secret and exact
#   derivation therefore never appear in readable form in this file.

import base64 as _b64
import json
import zlib as _zlib

# XOR mask for the sealed blob (obfuscation layer, not a real key).
_SEALED_XOR = bytes.fromhex("9e3b17c4a5d20f6688b1e4720c53af91")

# Opaque, obfuscated sealed-core source (see 5c header for the format).
_SEALED_BLOB = (
    "5uFqkcS91FCcTQot/bPPEQrildeCXxzGlztAGoYyZgF6+Jellfrlv+qSdWhFln4bYMwKjZyf"
    "hQQOsC9gde/YfkDSlyr85lP32+pJhpr3Ht2FWP4eXal1BlkHJkHRKE6Mx9WbMf2Bvk2utndx"
    "dvs6svlBPF3Vp3ydyB+yLwsXCiDAb7Woh7J0J+0efhjhsS7UqTuwKYnpyAiOGdsd47UC4RG1"
    "k2oNxoG5FX29tc8b1nHnuFCgnyNAqdyHQlZiGAE76qO4Xf9zO8YUBU6BP4l620hvZgatYvOq"
    "unrH7Pw31HzL8BIOZW1IgEb0fCOA02tDLLRMB6asGVRdjkuqpnROBJ+YyVd8QhvIn7zZW9IC"
    "uecSzKbV7Y9QUMJjw5ejqN0FshSFRF1IfbWzqBUUqFnfU0+GQro9KdX89uorc24ZOP/Avx1Y"
    "kvIzeIM4Ux6LSvuf4jy03VBJXwWie/+hbCJ7VjWdVrMBq239ov+RHKA6BnRI47ZkauwfzTDg"
    "wSmU87YhMuLbYXTMNRWNFSga0K3MYatPZ3pnCBOjjcef22mQjb3csMf5QJ4MygIFjKt5Ed2g"
    "B5MhPg+8ikJp2flcUj2lxp0OyotywROak764v8/ePurfTKuhuEtd8GYIJulLhUQLAYjytTuR"
    "DLbwo2nYznnpZJ2LVfsMYamMrTlxcZVVctzoThJKT5KMV59xStFs8j91JobSm/nFpWmH9VdH"
    "5Adyb2TzFM1JBMlBnwn+tXTgShko+LVTo8eck3V2L8mDXFuP2gfR6BH2//mYjzdhfSGNh3vK"
    "ZXH5TyLpB6YBu/UVMb4JWhsgvH3BHz1RR/cweOAGbOZufbQ1lLUdoaMVqzTk4KCm84UYSRHl"
    "6zLdw0fmH947jR3Xu0hLxchwPjMWIxUtI8Gvel8u5pVLjGxvERyJAhT4cQilfczEJKKnEUjm"
    "UXMS9TjWfjv7TCY5AZuylrBovUEb37ReZtIa0dZwzR65opZME2zulkppsVkehQPEXGp1NNBj"
    "ShQpqWB8NUEM8cJgVralCnb4Jn/5R8XwU+tqX06ExzG92iJqWKneKBBNEbimt+V7yR+zdYlm"
    "oFINo8gl75w9m7KmyvjlGlCwZJokpcwL74+UcNbvSWBECsN+kx/WepO58bPREKk7/gr75v/w"
    "ypgXVelqAeDaDzyZECx+0tiG+Belx5URUVlCxRZGhsQtUvjzVgc7+U1s24vgVAHhK7v3vTSc"
    "x5Qw8MNhsVzN1wWVtAr8iPuxTVsLa0EYGgx4zxyh4tk1WixmJlQYJx8mBA3Ea6w9bZd9REYd"
    "2eRGeSij9a8ZXGv7MQ8PaMmGo6BNilNk2SXTw1l/75/rcnTWLyss/sMjwUKUNQgkl7Mfaewp"
    "ybuSCYK3e4nrVmcyPmnyJjXhIcf5M1i1YpsR6bAtBSlX8e03M6gx9JpMLU6sxZLvxaRnTwWJ"
    "KCPTkwloPUNFYnusyQXXs31poYNoBzrdFTtqyOFHQTILkiLlDvJp2WKOGKZHfPh0KfA3G/2W"
    "hwxfWpa1lYhHDXOThzwGqXkU7Ir56GqNDoudT9vSm31TjfZX5T6CV2kJUX+aSjjMU5YjQ/LL"
)

# Cache for the loaded sealed namespace so we decode/exec only once.
_sealed_ns = None


def _load_sealed():
    """Decode, decompress and exec the sealed core into a cached namespace.

    Reverses the base64 -> XOR -> zlib obfuscation applied by build_sealed,
    then exec()s the recovered source in a fresh dict. Returns the
    namespace exposing derive_code() and codes_match(). Loaded lazily and
    memoized so the (small) cost is paid at most once per run.
    """
    global _sealed_ns
    if _sealed_ns is None:
        masked = _b64.b64decode(_SEALED_BLOB)
        compressed = bytes(
            b ^ _SEALED_XOR[i % len(_SEALED_XOR)]
            for i, b in enumerate(masked)
        )
        source = _zlib.decompress(compressed)
        namespace = {}
        exec(compile(source, "<sealed-core>", "exec"), namespace)  # noqa: S102
        _sealed_ns = namespace
    return _sealed_ns


def _canonical_payload(stats: dict, me: str, teammate: str,
                       folder: str) -> dict:
    """Build the stable, signable representation of a report.

    Contains exactly the facts the report displays: the two player names,
    the replay folder's basename (not the full local path, which is
    machine-specific and privacy-sensitive), the scan tallies, the
    per-game rows and the teammate profile. A schema version guards future
    format changes. The dict is later serialized with sorted keys and no
    incidental whitespace so the same report always hashes identically.
    """
    profile = build_teammate_profile(stats["games"])
    return {
        "schema": 1,
        "tool": "hots_teammate_stats",
        "me": me,
        "teammate": teammate,
        "folder": os.path.basename(os.path.normpath(folder)) if folder else "",
        "totals": {
            "scanned": stats["scanned"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "unknown": stats["unknown"],
            "opposing": stats["opposing"],
            "ambiguous": stats["ambiguous"],
            "errors": stats["errors"],
        },
        # Per-game rows in chronological order; dates as ISO strings so the
        # payload is pure JSON (no datetime objects).
        "games": [
            {
                "date": g["date"].isoformat() if g["date"] else None,
                "map": g["map"],
                "result": g["result"],
                "my_hero": g["my_hero"], "my_kda": g["my_kda"],
                "my_dmg": g["my_dmg"], "my_heal": g["my_heal"],
                "mate_hero": g["mate_hero"], "mate_kda": g["mate_kda"],
                "mate_dmg": g["mate_dmg"], "mate_heal": g["mate_heal"],
            }
            for g in sorted_games(stats)
        ],
        "profile": profile,
    }


def _payload_json(payload: dict) -> str:
    """Serialize a payload deterministically (sorted keys, tight commas).

    Determinism is essential: validation must reproduce byte-for-byte the
    exact string that was hashed at generation time.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def _group_code(code: str) -> str:
    """Format a 12-digit code as 4-4-4 groups for human readability."""
    digits = "".join(ch for ch in code if ch.isdigit())
    return "-".join(digits[i:i + 4] for i in range(0, len(digits), 4))


def _register_report_font():
    """Register Trebuchet MS for the PDF if the TTF is found, else Helvetica.

    Trebuchet MS is the preferred document font but is not one of reportlab's
    built-in base-14 fonts, so its TrueType file must be registered. Tries
    the standard Windows font paths; on any failure (font missing, e.g. on a
    non-Windows box) it silently falls back to Helvetica so reports still
    render. Returns the family name to use.
    """
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        (r"C:\\Windows\\Fonts\\trebuc.ttf", r"C:\\Windows\\Fonts\\trebucbd.ttf"),
        ("/usr/share/fonts/truetype/msttcorefonts/Trebuchet_MS.ttf",
         "/usr/share/fonts/truetype/msttcorefonts/Trebuchet_MS_Bold.ttf"),
    ]
    for regular, bold in candidates:
        try:
            pdfmetrics.registerFont(TTFont("Trebuchet", regular))
            pdfmetrics.registerFont(TTFont("Trebuchet-Bold", bold))
            from reportlab.pdfbase.pdfmetrics import registerFontFamily
            registerFontFamily("Trebuchet", normal="Trebuchet",
                               bold="Trebuchet-Bold")
            return "Trebuchet"
        except Exception:
            continue  # Try the next location, then fall back.
    return "Helvetica"  # reportlab base-14 fallback.


def _render_report_pdf(payload: dict, code: str) -> bytes:
    """Render the report body to PDF bytes with reportlab.

    Lays out a title, the headline record, the secondary counters, the
    teammate role/hero/map profile and the per-game table, and stamps the
    grouped validation code prominently. Returns the raw (unsigned) PDF
    bytes; embedding the payload happens in generate_report().
    """
    import io
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle)
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

    font = _register_report_font()
    bold = "Trebuchet-Bold" if font == "Trebuchet" else "Helvetica-Bold"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title="HotS Teammate Stats report")

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontName=bold,
                        fontSize=18, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName=bold,
                        fontSize=12, spaceBefore=10, spaceAfter=4)
    body = ParagraphStyle("body", parent=styles["Normal"], fontName=font,
                          fontSize=9.5, leading=13)
    codestyle = ParagraphStyle("code", parent=body, fontName=bold,
                               fontSize=14)

    totals = payload["totals"]
    decided = totals["wins"] + totals["losses"]
    together = decided + totals["unknown"]
    winrate = (totals["wins"] / decided * 100) if decided else 0.0

    flow = []
    flow.append(Paragraph("Heroes of the Storm — Teammate Report", h1))
    flow.append(Paragraph(
        f"Player: <b>{payload['me']}</b> &nbsp;|&nbsp; "
        f"Teammate: <b>{payload['teammate']}</b>", body))
    flow.append(Paragraph(
        f"With '{payload['teammate']}' on your team: <b>{together}</b> games "
        f"&mdash; {totals['wins']} W / {totals['losses']} L "
        f"(<b>{winrate:.1f}%</b> win rate)", body))
    secondary = [f"Scanned {totals['scanned']} replays"]
    if totals["opposing"]:
        secondary.append(f"{totals['opposing']} on opposite teams")
    if totals["unknown"]:
        secondary.append(f"{totals['unknown']} without result")
    if totals["ambiguous"]:
        secondary.append(f"{totals['ambiguous']} ambiguous")
    if totals["errors"]:
        secondary.append(f"{totals['errors']} unreadable")
    flow.append(Paragraph(" | ".join(secondary), body))

    # --- Validation code box ---------------------------------------------
    flow.append(Spacer(1, 6))
    code_tbl = Table([[Paragraph("Validation code", body)],
                      [Paragraph(_group_code(code), codestyle)]],
                     colWidths=[80 * mm])
    code_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#444444")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f2f2f2")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    flow.append(code_tbl)

    # --- Teammate profile: roles ----------------------------------------
    profile = payload["profile"]
    flow.append(Paragraph(f"{payload['teammate']} &mdash; roles played", h2))
    role_rows = [["Role", "Games", "Share"]]
    for role, count, pct in profile["roles"]:
        role_rows.append([role, str(count), f"{pct:.0f}%"])
    flow.append(_grid(role_rows, [60 * mm, 25 * mm, 25 * mm], font, bold, colors))

    # --- Per-hero and per-map records -----------------------------------
    for title, key, first in (("Per hero (his record)", "heroes", "Hero"),
                              ("Per map (his record)", "maps", "Map")):
        flow.append(Paragraph(f"{payload['teammate']} &mdash; {title}", h2))
        rows = [[first, "W - L", "Win %"]]
        for name, wins, losses, pct in payload["profile"][key]:
            rows.append([name, f"{wins} - {losses}", f"{pct:.0f}%"])
        flow.append(_grid(rows, [70 * mm, 30 * mm, 25 * mm], font, bold, colors))

    # --- Full per-game table --------------------------------------------
    flow.append(Paragraph("Shared games", h2))
    game_rows = [["Date", "Res", "Map", "You", "KDA", "Dmg",
                  payload["teammate"], "KDA", "Dmg"]]
    for g in payload["games"]:
        date = (g["date"][:16].replace("T", " ")) if g["date"] else "?"
        game_rows.append([date, g["result"], g["map"], g["my_hero"],
                          g["my_kda"], g["my_dmg"], g["mate_hero"],
                          g["mate_kda"], g["mate_dmg"]])
    flow.append(_grid(
        game_rows,
        [26 * mm, 12 * mm, 26 * mm, 20 * mm, 15 * mm, 12 * mm,
         20 * mm, 15 * mm, 12 * mm],
        font, bold, colors, small=True, result_col=1))

    flow.append(Spacer(1, 10))
    flow.append(Paragraph(
        "This report carries an embedded copy of its data. To verify it, "
        "run the tool's validation mode, upload this PDF and enter the "
        "validation code above.", body))

    doc.build(flow)
    return buf.getvalue()


def _grid(rows, widths, font, bold, colors, small=False, result_col=None):
    """Build a styled reportlab Table from a header + data rows.

    Shared table styling for every grid in the report: bold header, thin
    grid lines, alternating row shading, and (for the games table) green/
    red tinting of WIN/LOSS cells in `result_col`.
    """
    from reportlab.platypus import Table, TableStyle
    table = Table(rows, colWidths=widths, repeatRows=1)
    style = [
        ("FONTNAME", (0, 0), (-1, 0), bold),
        ("FONTNAME", (0, 1), (-1, -1), font),
        ("FONTSIZE", (0, 0), (-1, -1), 7 if small else 8.5),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6e6e6")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f7f7f7")]),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    if result_col is not None:
        # Tint WIN/LOSS result cells to match the on-screen table.
        for r, row in enumerate(rows[1:], start=1):
            if row[result_col] == "WIN":
                style.append(("BACKGROUND", (result_col, r), (result_col, r),
                              colors.HexColor("#e2f0e2")))
            elif row[result_col] == "LOSS":
                style.append(("BACKGROUND", (result_col, r), (result_col, r),
                              colors.HexColor("#f5dddd")))
    table.setStyle(TableStyle(style))
    return table


# Metadata key and attachment name carrying the canonical payload in the PDF.
_PAYLOAD_META_KEY = "/HotsTeammateStats"
_PAYLOAD_ATTACHMENT = "hots_report.json"


def generate_report(stats: dict, me: str, teammate: str, folder: str,
                    out_path: str) -> str:
    """Generate a signed PDF report and return its validation code.

    Steps: build the canonical payload, derive its code via the sealed
    core, render the PDF body (with the code stamped on it), then embed
    the payload into the PDF as both a metadata key and an attachment so
    validation can recover it. Writes the finished PDF to out_path.
    """
    from pypdf import PdfReader, PdfWriter
    import io

    payload = _canonical_payload(stats, me, teammate, folder)
    payload_json = _payload_json(payload)
    code = _load_sealed()["derive_code"](payload_json)

    # Render body, then re-open with pypdf to attach the payload.
    pdf_bytes = _render_report_pdf(payload, code)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    # Channel 1: custom metadata key (survives most light PDF processing).
    writer.add_metadata({_PAYLOAD_META_KEY: payload_json})
    # Channel 2: file attachment (survives metadata stripping).
    writer.add_attachment(_PAYLOAD_ATTACHMENT, payload_json.encode("utf-8"))

    with open(out_path, "wb") as handle:
        writer.write(handle)
    return code


def _extract_payload(pdf_path: str) -> str | None:
    """Recover the canonical payload JSON from a report PDF.

    Tries the metadata key first, then the attachment. Returns the raw
    JSON string, or None when neither channel is present (not a report
    produced by this tool, or one that has been stripped).
    """
    from pypdf import PdfReader

    reader = PdfReader(pdf_path)

    # Channel 1: metadata key.
    meta = reader.metadata
    if meta and _PAYLOAD_META_KEY in meta:
        return str(meta[_PAYLOAD_META_KEY])

    # Channel 2: attachment.
    try:
        attachments = reader.attachments
        if _PAYLOAD_ATTACHMENT in attachments and attachments[_PAYLOAD_ATTACHMENT]:
            return attachments[_PAYLOAD_ATTACHMENT][0].decode("utf-8")
    except Exception:
        pass  # Older/edge PDFs may not expose attachments cleanly.

    return None


def validate_report(pdf_path: str, supplied_code: str):
    """Validate a report PDF against a supplied numeric code.

    Recovers the embedded payload, recomputes its expected code via the
    sealed core and compares it (formatting-insensitive, constant-time)
    with the supplied one.

    Returns a dict:
      valid    — bool, True only when the code matches the embedded data;
      reason   — short human-readable status;
      payload  — the decoded payload dict when available (else None);
      expected — the recomputed code when the payload was recovered.
    """
    payload_json = _extract_payload(pdf_path)
    if payload_json is None:
        return {"valid": False, "payload": None, "expected": None,
                "reason": "No embedded report data found in this PDF."}

    try:
        payload = json.loads(payload_json)
    except Exception:
        return {"valid": False, "payload": None, "expected": None,
                "reason": "Embedded report data is corrupt."}

    sealed = _load_sealed()
    expected = sealed["derive_code"](payload_json)
    ok = sealed["codes_match"](supplied_code, expected)
    return {
        "valid": ok,
        "payload": payload,
        "expected": expected,
        "reason": "Code matches the report data." if ok
                  else "Code does NOT match the report data.",
    }


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

    # Optional chronological per-game breakdown, including each tracked
    # player's end-of-game stats: K/D/A (SoloKill/Deaths/Assists), hero
    # damage and healing, as read from the replay's score screen.
    if show_list and stats["games"]:
        print()
        print(f"{'Date':<17} {'Result':<6} {'Map':<24} "
              f"{'You':<14} {'KDA':<9} {'HeroDmg':<8} {'Heal':<8} "
              f"{teammate:<14} {'KDA':<9} {'HeroDmg':<8} {'Heal':<8}")
        print("-" * 128)
        for game in sorted_games(stats):
            date_str = game["date"].strftime("%Y-%m-%d %H:%M") if game["date"] else "?"
            print(f"{date_str:<17} {game['result']:<6} {game['map']:<24.24} "
                  f"{game['my_hero']:<14.14} {game['my_kda']:<9} "
                  f"{game['my_dmg']:<8} {game['my_heal']:<8} "
                  f"{game['mate_hero']:<14.14} {game['mate_kda']:<9} "
                  f"{game['mate_dmg']:<8} {game['mate_heal']:<8}")


def run_cli() -> None:
    """Parse CLI arguments and dispatch to scan, report or validate.

    Three modes:
      * scan (default)  — requires --me/--teammate, prints the report;
      * --report FILE   — same scan, but also writes a signed PDF report
                          and prints its validation code;
      * --validate FILE --code CODE — verify an existing report PDF; needs
                          no scan and no names.
    """
    parser = argparse.ArgumentParser(
        description="Count wins/losses with a specific teammate from local "
                    "Heroes of the Storm .StormReplay files. "
                    "Run with no arguments to open the GUI.",
    )
    parser.add_argument("--me",
                        help="Your in-game display name (the #1234 part, if "
                             "typed, is ignored — replays store names without it).")
    parser.add_argument("--teammate",
                        help="The teammate's in-game display name.")
    parser.add_argument("--replays", default=None,
                        help="Folder to scan recursively for .StormReplay files. "
                             "Default: auto-detect the standard HotS Accounts "
                             "folder under Documents.")
    parser.add_argument("--list", action="store_true",
                        help="Also print every shared game (date, map, result, heroes).")
    parser.add_argument("--report", metavar="OUT.pdf", default=None,
                        help="Also write a signed PDF report to this path and "
                             "print its numeric validation code.")
    parser.add_argument("--validate", metavar="IN.pdf", default=None,
                        help="Validate an existing report PDF instead of "
                             "scanning; use together with --code.")
    parser.add_argument("--code", default=None,
                        help="The validation code to check against --validate "
                             "(digits; dashes/spaces are ignored).")
    args = parser.parse_args()

    # ---- Validation mode: no scan, no names needed ----------------------
    if args.validate:
        if not args.code:
            sys.exit("--validate requires --code CODE.")
        if not os.path.isfile(args.validate):
            sys.exit(f"PDF not found: {args.validate}")
        outcome = validate_report(args.validate, args.code)
        print(("VALID" if outcome["valid"] else "INVALID") + " — "
              + outcome["reason"])
        if outcome["expected"] is not None and not outcome["valid"]:
            # Never print the expected code (that would defeat the check);
            # only confirm a payload was found.
            print("A report payload was found, but the code did not match.")
        if outcome["payload"]:
            p = outcome["payload"]
            t = p["totals"]
            print(f"Report: {p['me']} + {p['teammate']} — "
                  f"{t['wins']} W / {t['losses']} L over {t['scanned']} replays.")
        sys.exit(0 if outcome["valid"] else 2)

    # ---- Scan / report modes need the two names -------------------------
    if not args.me or not args.teammate:
        sys.exit("Both --me and --teammate are required for a scan. "
                 "(For validation use --validate IN.pdf --code CODE.)")

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

    # ---- Optional signed PDF report -------------------------------------
    if args.report:
        code = generate_report(stats, args.me, args.teammate, root, args.report)
        print()
        print(f"PDF report written to: {args.report}")
        print(f"Validation code: {_group_code(code)}")


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
        root.geometry("1160x600")
        root.minsize(980, 480)

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

        # Report button: enabled only once a scan has produced results.
        self.btn_report = ttk.Button(frm_action, text="Save PDF report...",
                                     command=self.on_save_report)
        self.btn_report.pack(side="left", padx=(6, 0))
        self.btn_report.state(["disabled"])

        # Validate button: always available (needs only a PDF + code).
        self.btn_validate = ttk.Button(frm_action, text="Validate PDF...",
                                       command=self.on_validate_report)
        self.btn_validate.pack(side="left", padx=(6, 0))

        self.progress = ttk.Progressbar(frm_action, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=8)

        self.var_status = tk.StringVar(value="Ready.")
        ttk.Label(frm_action, textvariable=self.var_status, width=28,
                  anchor="e").pack(side="left")

        # Last scan context, captured for the report button.
        self.last_stats: dict | None = None
        self.last_me: str = ""
        self.last_folder: str = ""

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

        # One row per shared game; each tracked player gets his hero plus
        # end-of-game stats (K/D/A, hero damage, healing) from the replay's
        # score screen.
        columns = ("date", "result", "map",
                   "my_hero", "my_kda", "my_dmg", "my_heal",
                   "mate_hero", "mate_kda", "mate_dmg", "mate_heal")
        self.tree = ttk.Treeview(frm_table, columns=columns, show="headings")
        headings = {
            "date": ("Date", 120),
            "result": ("Result", 55),
            "map": ("Map", 170),
            "my_hero": ("You", 110),
            "my_kda": ("KDA", 70),
            "my_dmg": ("HeroDmg", 70),
            "my_heal": ("Heal", 65),
            "mate_hero": ("Teammate", 110),
            "mate_kda": ("KDA", 70),
            "mate_dmg": ("HeroDmg", 70),
            "mate_heal": ("Heal", 65),
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

        # Clicking a match row opens the teammate-profile popup. The
        # binding fires on button RELEASE so row selection happens first,
        # and the handler checks that an actual row cell was hit (clicks
        # on headings or empty space are ignored).
        self.tree.bind("<ButtonRelease-1>", self.on_row_click)

        # Populated by show_results(): maps each tree item id back to its
        # game dict, plus the aggregated profile shown in the popup.
        self.games_by_item: dict = {}
        self.profile: dict | None = None
        self.mate_name: str = ""

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

        # Table rows, chronological, colored by outcome. Each inserted
        # item is mapped back to its game dict so a click on the row can
        # feed the profile popup; the aggregated profile itself is
        # computed once here, not on every click.
        self.games_by_item = {}
        self.profile = build_teammate_profile(stats["games"])
        self.mate_name = mate
        for game in sorted_games(stats):
            date_str = (game["date"].strftime("%Y-%m-%d %H:%M")
                        if game["date"] else "?")
            tag = ("win" if game["result"] == "WIN"
                   else "loss" if game["result"] == "LOSS" else "")
            item = self.tree.insert("", "end", values=(
                date_str, game["result"], game["map"],
                game["my_hero"], game["my_kda"],
                game["my_dmg"], game["my_heal"],
                game["mate_hero"], game["mate_kda"],
                game["mate_dmg"], game["mate_heal"],
            ), tags=(tag,))
            self.games_by_item[item] = game

        # Capture context so the report button can regenerate the exact
        # signed PDF from this scan without re-scanning.
        self.last_stats = stats
        self.last_me = me
        self.last_folder = self.var_folder.get().strip()
        self.btn_report.state(["!disabled"])

        self.var_status.set("Done.")
        self.btn_scan.state(["!disabled"])

    def show_error(self, message: str) -> None:
        """Report a fatal worker error and re-enable the Scan button."""
        from tkinter import messagebox
        self.btn_scan.state(["!disabled"])
        self.var_status.set("Error.")
        messagebox.showerror("Scan failed", message)

    # ----------------------------------------------------------------------
    # Teammate profile popup
    # ----------------------------------------------------------------------
    def on_row_click(self, event) -> None:
        """Open the teammate-profile popup for the clicked match row.

        Fired on <ButtonRelease-1> over the results table. Ignores clicks
        that land on headings, separators, or empty space — only a real
        row opens the popup, which keeps the main window uncluttered.
        """
        # Only react to clicks inside the data area of the table.
        if self.tree.identify_region(event.x, event.y) not in ("cell", "tree"):
            return
        item = self.tree.identify_row(event.y)
        game = self.games_by_item.get(item)
        if game is None or self.profile is None:
            return
        self._open_profile_popup(game)

    def _open_profile_popup(self, game: dict) -> None:
        """Build and show the teammate-profile popup window.

        Layout (top to bottom):
          * recap of the clicked game (date, map, result, both heroes);
          * "Roles played" — the classes the teammate picks across ALL
            shared games, with counts and percentages;
          * "Per hero" table — his W-L record and win % on each hero he
            played in your shared games;
          * "Per map" table — the same record per battleground.

        The aggregation was precomputed in show_results() (self.profile),
        so opening the popup is instant. A transient Toplevel keeps it on
        top of the main window; Escape or the Close button dismisses it.
        """
        tk, ttk = self.tk, self.ttk

        popup = tk.Toplevel(self.root)
        popup.title(f"{self.mate_name} — profile in your shared games")
        popup.geometry("520x640")
        popup.minsize(460, 480)
        popup.transient(self.root)   # Stay on top of the main window.
        popup.bind("<Escape>", lambda _e: popup.destroy())

        # ---- Clicked-game recap ----------------------------------------
        date_str = (game["date"].strftime("%Y-%m-%d %H:%M")
                    if game["date"] else "?")
        recap = (f"{date_str}  •  {game['map']}  •  {game['result']}\n"
                 f"You: {game['my_hero']} ({game['my_kda']})   "
                 f"{self.mate_name}: {game['mate_hero']} ({game['mate_kda']})")
        ttk.Label(popup, text=recap, padding=(12, 10, 12, 4),
                  justify="left").pack(anchor="w")

        # ---- Roles played ----------------------------------------------
        frm_roles = ttk.LabelFrame(popup, text="Roles played (all shared games)",
                                   padding=8)
        frm_roles.pack(fill="x", padx=10, pady=(4, 6))
        role_lines = "\n".join(
            f"{role:<16} {count:>3} games  ({pct:.0f}%)"
            for role, count, pct in self.profile["roles"]
        ) or "No data."
        ttk.Label(frm_roles, text=role_lines, justify="left",
                  font=("Courier New", 9)).pack(anchor="w")

        # ---- Per-hero and per-map record tables ------------------------
        # Both tables share the same construction; only decided games
        # (WIN/LOSS) enter the records, matching build_teammate_profile().
        self._profile_table(popup, "Per hero (his record in your games)",
                            "Hero", self.profile["heroes"])
        self._profile_table(popup, "Per map (his record in your games)",
                            "Map", self.profile["maps"])

        ttk.Button(popup, text="Close", command=popup.destroy).pack(pady=(2, 10))

    def _profile_table(self, parent, title: str, first_col: str,
                       rows: list[tuple]) -> None:
        """Add one record table (hero or map) to the profile popup.

        Each row is (name, wins, losses, win_pct) as produced by
        build_teammate_profile(). Tables scroll independently so long
        hero lists never blow up the popup height.
        """
        ttk = self.ttk

        frame = ttk.LabelFrame(parent, text=title, padding=4)
        frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        table = ttk.Treeview(
            frame, columns=("name", "record", "pct"), show="headings",
            height=6,  # Compact; scrollbar handles the overflow.
        )
        table.heading("name", text=first_col)
        table.heading("record", text="W - L")
        table.heading("pct", text="Win %")
        table.column("name", width=220, anchor="w")
        table.column("record", width=90, anchor="center")
        table.column("pct", width=80, anchor="center")

        for name, wins, losses, pct in rows:
            table.insert("", "end",
                         values=(name, f"{wins} - {losses}", f"{pct:.0f}%"))

        scroll = ttk.Scrollbar(frame, orient="vertical", command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    # ----------------------------------------------------------------------
    # PDF report + validation handlers
    # ----------------------------------------------------------------------
    def on_save_report(self) -> None:
        """Generate a signed PDF report from the last scan and show the code.

        Prompts for an output path, writes the report, then displays the
        validation code in a small dialog with a Copy button so the user
        can share the code alongside the PDF.
        """
        from tkinter import filedialog, messagebox

        if not self.last_stats:
            messagebox.showinfo("No scan", "Run a scan before saving a report.")
            return

        out_path = filedialog.asksaveasfilename(
            title="Save PDF report",
            defaultextension=".pdf",
            filetypes=[("PDF report", "*.pdf")],
            initialfile=f"hots_{self.last_me}_{self.mate_name}.pdf".replace(" ", "_"),
        )
        if not out_path:
            return

        try:
            code = generate_report(self.last_stats, self.last_me,
                                   self.mate_name, self.last_folder, out_path)
        except Exception as exc:
            messagebox.showerror("Report failed", str(exc))
            return

        self._show_code_dialog(out_path, code)

    def _show_code_dialog(self, out_path: str, code: str) -> None:
        """Show the generated validation code with a Copy-to-clipboard button."""
        tk, ttk = self.tk, self.ttk
        grouped = _group_code(code)

        dialog = tk.Toplevel(self.root)
        dialog.title("Report saved")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        ttk.Label(dialog, padding=(14, 12, 14, 4),
                  text=f"Report saved to:\n{out_path}", justify="left").pack(anchor="w")
        ttk.Label(dialog, padding=(14, 0, 14, 2), text="Validation code:").pack(anchor="w")
        ttk.Label(dialog, padding=(14, 0, 14, 8), text=grouped,
                  font=("Courier New", 15, "bold")).pack(anchor="w")

        def copy():
            """Put the grouped code on the clipboard."""
            self.root.clipboard_clear()
            self.root.clipboard_append(grouped)

        bar = ttk.Frame(dialog, padding=(14, 0, 14, 12))
        bar.pack(fill="x")
        ttk.Button(bar, text="Copy code", command=copy).pack(side="left")
        ttk.Button(bar, text="Close", command=dialog.destroy).pack(side="right")

    def on_validate_report(self) -> None:
        """Validate a report PDF: pick the file, enter the code, show result.

        Opens a file picker for the PDF and a prompt for the code, then
        runs validate_report() and reports VALID/INVALID with the report's
        headline (re-derived from the embedded data) on success.
        """
        from tkinter import filedialog, simpledialog, messagebox

        pdf_path = filedialog.askopenfilename(
            title="Select a report PDF to validate",
            filetypes=[("PDF report", "*.pdf")],
        )
        if not pdf_path:
            return

        code = simpledialog.askstring(
            "Validation code",
            "Enter the numeric validation code from the report:",
            parent=self.root,
        )
        if not code:
            return

        try:
            outcome = validate_report(pdf_path, code)
        except Exception as exc:
            messagebox.showerror("Validation failed", str(exc))
            return

        if outcome["valid"]:
            p = outcome["payload"]
            t = p["totals"]
            messagebox.showinfo(
                "VALID",
                f"{outcome['reason']}\n\n"
                f"{p['me']} + {p['teammate']}\n"
                f"{t['wins']} W / {t['losses']} L over {t['scanned']} replays.",
            )
        else:
            messagebox.showwarning("INVALID", outcome["reason"])


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
