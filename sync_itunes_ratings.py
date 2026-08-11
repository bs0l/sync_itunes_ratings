#!/usr/bin/env python3
"""
sync_itunes_ratings.py  (v2)

Writes iTunes song ratings directly into MP3 file tags (POPM), and ONLY
the rating -- nothing else in the file is touched.

FEATURES
-------------------------
- READ-ONLY against iTunes. This script never opens, modifies, or writes
  back to iTunes or its XML in any way. It only reads the XML once.
- Filter to a single song or album so you can test before touching your
  whole library.
- Always shows a before/after comparison table and requires you to type
  a confirmation before writing anything. Nothing is written silently.
- Every file it's about to modify is fully backed up first (the whole
  file, not just the rating) to a separate backup folder, with a manifest
  so you can restore everything with one command if you don't like the
  result.
- Only ever reads, compares, and writes its OWN two POPM tags (email
  "no@email" and "Windows Media Player 9 Series" -- the second one exists
  so Windows Explorer / WMP display the rating correctly, since that's
  the specific tag they look for). Any OTHER rating-like tag in the file
  -- from MediaMonkey, Traktor, an "Explicit" flag, anything -- is never
  read as "the file's rating" and never modified. It's simply not this
  script's data to touch.
- Files with no iTunes rating get our OWN tag CLEARED (removed) --
  shown explicitly in the comparison table before you confirm. Any
  unrelated foreign tag stays exactly as it was, per above.

DEPENDENCY -- exactly one, nothing else
----------------------------------------
This uses "mutagen": https://mutagen.readthedocs.io/
It's the standard, widely-used open-source Python library for reading/
writing audio file tags (MP3, FLAC, MP4, etc.) -- used by projects like
Beets, Picard, and many others. It is NOT downloading or running any
code beyond what you can inspect at that link. Install it with:

    pip install mutagen --break-system-packages

Nothing else gets installed. You can open the mutagen source yourself
if you want to verify what it does with your files.

USAGE
-----
Test on ONE song first:
    python sync_itunes_ratings.py --song "Comfortably Numb"

Test on an ALBUM:
    python sync_itunes_ratings.py --album "The Wall"

Once you trust the output, run on your whole library:
    python sync_itunes_ratings.py --full-library

If your iTunes XML isn't in the default location:
    python sync_itunes_ratings.py --full-library --xml "D:\\path\\to\\iTunes Music Library.xml"

To undo everything from a previous run:
    python sync_itunes_ratings.py --restore backups\\2026-08-10_142301\\manifest.csv

Every run (test or full) ALWAYS:
  1. Shows a comparison table.
  2. Asks you to type YES before writing anything.
  3. Backs up each file it's about to touch before touching it.
"""

import argparse
import csv
import os
import plistlib
import shutil
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

try:
    from mutagen.id3 import ID3, POPM
    from mutagen.mp3 import MP3
except ImportError:
    print("Missing dependency. Run: pip install mutagen --break-system-packages")
    sys.exit(1)

# The ONLY six values iTunes' star-rating UI produces (0-5 stars, no
# half-stars in normal use). Mapped to the POPM 0-255 values that Windows
# Media Player / MediaMonkey / most players treat as "N stars" -- these
# are NOT proportional to 0-100, they're a fixed convention (e.g. 1-star
# is stored as the literal value 1, not 51, so it's distinguishable from
# "unrated"=0).
#
# Deliberately NOT interpolated or clamped. If iTunes' XML ever contains
# something outside these six values, that's unexpected and worth a human
# looking at -- not something to silently guess an approximation for in a
# script that writes to your files. Such tracks are skipped and reported,
# never guessed at.
ITUNES_TO_POPM = {0: 0, 20: 1, 40: 64, 60: 128, 80: 196, 100: 255}
POPM_EMAILS = ["no@email", "Windows Media Player 9 Series"]


def itunes_to_popm(rating):
    """Look up the POPM value for an iTunes rating. Returns None if there's
    no rating. Returns the string "UNEXPECTED" if the rating exists but
    isn't one of the six standard values -- caller must treat that as a
    skip-and-report case, never guess an approximation."""
    if rating is None:
        return None
    if rating in ITUNES_TO_POPM:
        return ITUNES_TO_POPM[rating]
    return "UNEXPECTED"


# POPM value -> star count, used only to DISPLAY whatever rating already
# exists in a file (which may have been written by some other app with its
# own scale) -- this is purely informational, never used to decide what to
# write. iTunes-side values are handled strictly above, with no guessing.
FILE_STAR_ANCHORS = [(0, 0), (1, 1), (64, 2), (128, 3), (196, 4), (255, 5)]


def popm_to_display(value):
    """Show a POPM value as an approximate star rating AND the raw number,
    so nothing is hidden if the value doesn't land exactly on a known
    star boundary (e.g. written by some other app with its own scale)."""
    if value is None:
        return "none"
    for p, s in FILE_STAR_ANCHORS:
        if value == p:
            return f"{s}\u2605 ({value})"
    for (p0, s0), (p1, s1) in zip(FILE_STAR_ANCHORS, FILE_STAR_ANCHORS[1:]):
        if p0 <= value <= p1:
            frac = (value - p0) / (p1 - p0)
            approx = s0 + frac * (s1 - s0)
            return f"~{approx:.1f}\u2605 ({value})"
    return f"? ({value})"


def itunes_rating_to_display(rating):
    if rating is None:
        return "none"
    if rating in ITUNES_TO_POPM:
        return f"{rating // 20}\u2605"
    return f"{rating} (unexpected!)"


def default_xml_path() -> Path:
    home = Path.home()
    for c in [
        home / "Music" / "iTunes" / "iTunes Music Library.xml",
        home / "My Music" / "iTunes" / "iTunes Music Library.xml",
    ]:
        if c.exists():
            return c
    return home / "Music" / "iTunes" / "iTunes Music Library.xml"


def file_uri_to_path(uri: str) -> str:
    """Convert an iTunes file:// URL to a filesystem path. Handles the
    common local-drive case (file://localhost/C:/... or file:///C:/...)
    AND network/UNC paths (file://ServerName/Share/...), which the
    previous version silently dropped the server name from."""
    parsed = urllib.parse.urlparse(uri)
    path = urllib.parse.unquote(parsed.path)
    netloc = urllib.parse.unquote(parsed.netloc)

    if netloc and netloc.lower() != "localhost":
        # Network path: file://ServerName/Share/... -> \\ServerName\Share\...
        return "\\\\" + netloc + path.replace("/", "\\")

    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path


def get_current_popm(filepath):
    """Read the file's rating, but ONLY from tags this script itself owns
    (POPM_EMAILS). Any other POPM frame in the file -- from MediaMonkey,
    Traktor, an "Explicit" flag, or anything else -- is data this script
    has no business interpreting as a rating or touching in any way. It is
    completely ignored for comparison purposes and left untouched on disk.

    Returns (status, rating, detail, foreign_frames, existing_counts):
      status 'ok'         -> rating is our own tag's value (None if absent)
      status 'unreadable' -> file couldn't be parsed at all; NEVER treat
                              this as "no rating" -- caller must skip.
      status 'conflict'   -> our OWN two email conventions disagree with
                              each other (e.g. a previous inconsistent
                              write, or another tool coincidentally using
                              one of our emails). detail lists them.
      foreign_frames       -> list of (email, rating) for any OTHER POPM
                              frames present, purely informational. These
                              are never read as "current rating" and never
                              modified.
      existing_counts       -> {email: count} for our own existing frames,
                              so a later rewrite can preserve each frame's
                              play-count instead of resetting it to 0.
    """
    try:
        audio = MP3(filepath, ID3=ID3)
    except Exception as e:
        return ("unreadable", None, str(e), [], {})

    if audio.tags is None:
        return ("ok", None, None, [], {})

    frames = audio.tags.getall("POPM")
    if not frames:
        return ("ok", None, None, [], {})

    own_frames = [fr for fr in frames if fr.email in POPM_EMAILS]
    foreign = [(fr.email, fr.rating) for fr in frames if fr.email not in POPM_EMAILS]
    # The ID3 spec's counter field on POPM is optional, so mutagen only
    # sets .count when the file's frame actually included it -- it's not
    # guaranteed to exist, let alone default to 0. Fall back to 0 rather
    # than crash on files that omitted it.
    existing_counts = {fr.email: getattr(fr, "count", 0) for fr in own_frames}

    if not own_frames:
        return ("ok", None, None, foreign, existing_counts)

    own_ratings = {fr.rating for fr in own_frames}
    if len(own_ratings) > 1:
        detail = ", ".join(f"{fr.email}={fr.rating}" for fr in own_frames)
        return ("conflict", own_frames[0].rating, detail, foreign, existing_counts)

    return ("ok", own_frames[0].rating, None, foreign, existing_counts)


def load_itunes_tracks(xml_path, song_filter, album_filter):
    with open(xml_path, "rb") as f:
        library = plistlib.load(f)

    tracks = library.get("Tracks", {})
    matched = []
    for track in tracks.values():
        location = track.get("Location")
        kind = track.get("Kind", "")
        if not location:
            continue
        if "MPEG" not in kind and not location.lower().endswith(".mp3"):
            continue

        name = track.get("Name", "")
        album = track.get("Album", "")
        artist = track.get("Artist", "")

        if song_filter and song_filter.lower() not in name.lower():
            continue
        if album_filter and album_filter.lower() not in album.lower():
            continue

        filepath = file_uri_to_path(location)

        # iTunes silently applies a computed "gray star" rating to unrated
        # tracks on an album whenever you've rated OTHER tracks on that same
        # album -- it's an average, not something you actually set for this
        # song. iTunes' XML export flags these with "Rating Computed": true.
        # We only want ratings you actually gave, so computed ones are
        # treated as if there's no rating at all.
        is_computed = bool(track.get("Rating Computed", False))
        raw_rating = track.get("Rating")
        itunes_rating = None if is_computed else raw_rating

        matched.append({
            "name": name,
            "album": album,
            "artist": artist,
            "filepath": filepath,
            "itunes_rating": itunes_rating,  # None if unrated OR only computed
            "itunes_rating_was_computed": is_computed,
            "itunes_rating_raw": raw_rating,
        })
    return matched


def build_plan(matched):
    """For each matched track, figure out current state and the action."""
    plan = []
    for t in matched:
        exists = os.path.exists(t["filepath"])
        current_popm = None
        popm_status, popm_detail, foreign_frames, existing_counts = "ok", None, [], {}
        modified = None

        if exists:
            popm_status, current_popm, popm_detail, foreign_frames, existing_counts = get_current_popm(t["filepath"])
            try:
                modified = datetime.fromtimestamp(os.path.getmtime(t["filepath"]))
            except OSError:
                modified = None

        if not exists:
            action_type = "SKIP"
            action = "SKIP (file not found on disk)"
            target_popm = None
        elif popm_status == "unreadable":
            # Never guess. A file we can't read is left completely alone.
            action_type = "SKIP"
            action = f"SKIP (file couldn't be read -- {popm_detail})"
            target_popm = None
        elif t["itunes_rating"] is None:
            if current_popm is None:
                action_type = "NO_CHANGE"
                action = "NO CHANGE (no rating either place)"
                target_popm = None
            else:
                action_type = "CLEAR"
                action = "CLEAR (iTunes has no rating, file currently does)"
                target_popm = "CLEAR"
        else:
            target_popm = itunes_to_popm(t["itunes_rating"])
            if target_popm == "UNEXPECTED":
                action_type = "SKIP"
                action = (f'SKIP (unexpected iTunes rating {t["itunes_rating"]} for '
                           f'"{t["name"]}" -- not one of the six standard values '
                           f'0/20/40/60/80/100; left untouched)')
                target_popm = None
            elif current_popm == target_popm:
                action_type = "NO_CHANGE"
                action = "NO CHANGE (already matches)"
            else:
                action_type = "SET"
                action = "SET"

        if popm_status == "conflict":
            # Our own two email conventions disagree with each other. Force
            # a write to consolidate them, even if the one we happened to
            # compare against already matched -- otherwise the disagreement
            # between our own tags would silently persist.
            if action_type == "NO_CHANGE" and t["itunes_rating"] is not None:
                action_type = "SET"
                action = "SET (consolidating disagreement between your own tags)"
            action += f"  [your own tags disagree: {popm_detail} -- will be consolidated to one value]"

        if foreign_frames:
            names = ", ".join(f"'{email}'" for email, _ in foreign_frames)
            action += f"  [also has unrelated tag(s) under {names} -- left untouched, not read as rating]"

        if t.get("itunes_rating_was_computed"):
            action += (f"  [iTunes shows {t['itunes_rating_raw']//20 if t['itunes_rating_raw'] else 0}"
                       f"\u2605 gray/computed stars from the album average -- not a rating you gave, ignored]")

        plan.append({**t, "current_popm": current_popm, "target_popm": target_popm,
                      "action": action, "action_type": action_type, "exists": exists,
                      "foreign_frames": foreign_frames, "existing_counts": existing_counts,
                      "modified": modified})
    return plan


DISPLAY_FILTERS = {
    "all": None,
    "set": {"SET"},
    "clear": {"CLEAR"},
    "nochange": {"NO_CHANGE"},
    "skip": {"SKIP"},
    "changes": {"SET", "CLEAR"},  # convenience: anything that will actually change
}


def print_comparison(plan, show="all"):
    allowed = DISPLAY_FILTERS.get(show)
    rows = plan if allowed is None else [p for p in plan if p["action_type"] in allowed]

    print(f"\n{'Song':<32} {'Artist':<20} {'iTunes':<8} {'File now':<12} {'Modified':<11} {'Action'}")
    print("-" * 130)
    if not rows:
        print(f"(no rows match --show {show})")
    for p in rows:
        itunes_disp = itunes_rating_to_display(p["itunes_rating"])
        file_disp = popm_to_display(p["current_popm"])
        title = p["name"][:30]
        artist = (p.get("artist") or "")[:18]
        mod_disp = p["modified"].strftime("%Y-%m-%d") if p["modified"] else "--"
        print(f"{title:<32} {artist:<20} {itunes_disp:<8} {file_disp:<12} {mod_disp:<11} {p['action']}")
    print("-" * 130)

    to_change = [p for p in plan if p["action_type"] in ("SET", "CLEAR")]
    if allowed is not None and len(rows) != len(plan):
        print(f"\nShowing {len(rows)} of {len(plan)} matched track(s) (--show {show}).")
    print(f"{len(plan)} matched track(s) total. {len(to_change)} will actually be changed.")
    return to_change


def backup_file(filepath, backup_root):
    drive, tail = os.path.splitdrive(os.path.abspath(filepath))
    drive_label = drive.rstrip(":") if drive else "root"
    tail = tail.lstrip("\\/")
    dest = backup_root / drive_label / tail
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(filepath, dest)
    return str(dest)


def make_backup_root():
    """Microsecond-resolution timestamp plus a collision check, so two
    runs started within the same second never share a backup folder."""
    base = datetime.now().strftime("%Y-%m-%d_%H%M%S_%f")
    root = Path("backups") / base
    n = 1
    while root.exists():
        root = Path("backups") / f"{base}_{n}"
        n += 1
    return root


def apply_changes(to_change, backup_root):
    manifest_path = backup_root / "manifest.csv"
    backup_root.mkdir(parents=True, exist_ok=True)

    with open(manifest_path, "w", newline="", encoding="utf-8") as mf:
        writer = csv.writer(mf)
        writer.writerow(["original_path", "backup_path"])

        for p in to_change:
            filepath = p["filepath"]
            try:
                # Back up BEFORE touching anything, and record it in the
                # manifest immediately (flushed to disk). If anything below
                # fails -- even mid-write -- this file is already recoverable.
                backup_path = backup_file(filepath, backup_root)
                writer.writerow([filepath, backup_path])
                mf.flush()

                audio = MP3(filepath, ID3=ID3)
                original_version = audio.tags.version if audio.tags else None
                if audio.tags is None:
                    audio.add_tags()

                # Remove ONLY our own POPM frames (identified by email).
                # Any other POPM frame -- MediaMonkey, Traktor, an
                # "Explicit" flag, whatever -- is left in place, untouched,
                # because it isn't ours to manage.
                existing_frames = audio.tags.getall("POPM")
                audio.tags.delall("POPM")
                for fr in existing_frames:
                    if fr.email not in POPM_EMAILS:
                        audio.tags.add(fr)

                if p["target_popm"] != "CLEAR":
                    for email in POPM_EMAILS:
                        # Reuse this email's existing play count if it had
                        # one already; only brand-new frames start at 0.
                        # Never discard play-count history just because the
                        # rating changed.
                        preserved_count = p.get("existing_counts", {}).get(email, 0)
                        audio.tags.add(POPM(email=email, rating=p["target_popm"], count=preserved_count))

                # Preserve the file's existing ID3 version (v2.3 vs v2.4)
                # instead of letting mutagen silently upgrade it, which is
                # a side effect beyond "just the rating".
                v2_version = 3 if original_version and original_version[:2] == (2, 3) else 4
                audio.save(v2_version=v2_version)

                # Verify: re-read the file we just wrote and confirm the
                # rating actually landed correctly before calling it OK.
                _, written_rating, _, _, _ = get_current_popm(filepath)
                expected = None if p["target_popm"] == "CLEAR" else p["target_popm"]
                if written_rating != expected:
                    raise RuntimeError(
                        f"verification failed after write: expected {expected}, "
                        f"found {written_rating}"
                    )

                print(f"OK: {p['name']}")
            except Exception as e:
                print(f"\nERROR on '{p['name']}' ({filepath}): {e}")
                print("Stopping here. Every file changed so far (including this one,")
                print("via its pre-write backup) is recoverable. Files not yet reached")
                print("were never touched. To undo everything done in this run:")
                print(f"    python {sys.argv[0]} --restore \"{manifest_path}\"")
                sys.exit(1)

    print(f"\nDone. {len(to_change)} file(s) updated.")
    print(f"Backups + manifest saved to: {backup_root}")
    print(f"To undo this run: python {sys.argv[0]} --restore \"{manifest_path}\"")


def restore(manifest_path):
    manifest_path = Path(manifest_path)
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}")
        sys.exit(1)

    with open(manifest_path, newline="", encoding="utf-8") as mf:
        reader = csv.DictReader(mf)
        rows = list(reader)

    print(f"About to restore {len(rows)} file(s) from backup:")
    for row in rows:
        print(f"  {row['original_path']}")
    confirm = input("\nType YES to restore these files: ").strip()
    if confirm != "YES":
        print("Cancelled. Nothing restored.")
        return

    restored = 0
    for row in rows:
        try:
            shutil.copy2(row["backup_path"], row["original_path"])
            restored += 1
        except Exception as e:
            print(f"ERROR restoring {row['original_path']}: {e}")

    print(f"Restored {restored}/{len(rows)} file(s).")


def main():
    parser = argparse.ArgumentParser(description="Sync iTunes ratings into MP3 file tags.")
    parser.add_argument("--xml", type=str, default=None)
    parser.add_argument("--song", type=str, default=None, help="Test on songs matching this text")
    parser.add_argument("--album", type=str, default=None, help="Test on an album matching this text")
    parser.add_argument("--full-library", action="store_true", help="Run against the entire library")
    parser.add_argument("--restore", type=str, default=None, help="Path to a manifest.csv to restore from")
    parser.add_argument("--no-clear", action="store_true",
                         help="Do NOT clear file ratings for tracks with no iTunes rating "
                              "(default behavior clears them, per your instruction)")
    parser.add_argument("--show", type=str, default="all",
                         choices=list(DISPLAY_FILTERS.keys()),
                         help="Filter which rows are PRINTED (all/set/clear/nochange/skip/changes). "
                              "Does not affect which files are actually changed -- that's always "
                              "every SET/CLEAR row, regardless of this filter.")
    args = parser.parse_args()

    if args.restore:
        restore(args.restore)
        return

    if not args.song and not args.album and not args.full_library:
        print("You must specify --song, --album, or --full-library.")
        print("Example test run: python sync_itunes_ratings.py --song \"Comfortably Numb\"")
        sys.exit(1)

    xml_path = Path(args.xml) if args.xml else default_xml_path()
    if not xml_path.exists():
        print(f"Could not find iTunes Music Library.xml at: {xml_path}")
        print("Pass its location with --xml \"path\\to\\file.xml\"")
        sys.exit(1)

    print(f"Reading (read-only): {xml_path}")
    matched = load_itunes_tracks(xml_path, args.song, args.album)

    if not matched:
        print("No matching tracks found. Check your --song / --album spelling.")
        return

    print(f"Checking current file tags for {len(matched)} matched track(s)...")
    plan = build_plan(matched)

    if args.no_clear:
        for p in plan:
            if p["action_type"] == "CLEAR":
                p["action_type"] = "NO_CHANGE"
                p["action"] = "NO CHANGE (--no-clear: leaving existing file rating alone)"

    to_change = print_comparison(plan, show=args.show)

    if not to_change:
        print("\nNothing to do -- all matched files already match iTunes.")
        return

    confirm = input(f"\nType YES to write these {len(to_change)} change(s) (files will be backed up first): ").strip()
    if confirm != "YES":
        print("Cancelled. No files were touched.")
        return

    apply_changes(to_change, make_backup_root())


if __name__ == "__main__":
    main()