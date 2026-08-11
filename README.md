# sync_itunes_ratings.py — Guide

Writes iTunes ratings into your MP3 files' tags so other apps (Windows, MediaMonkey, etc.) can read them. iTunes stays the only source of truth; nothing is ever written back to iTunes.

## One-time setup

1. **iTunes:** Edit > Preferences > Advanced > check **"Share iTunes Library XML with other apps."**
2. **Install the one dependency:**
   ```
   pip install mutagen --break-system-packages
   ```

## Basic usage

Every run shows a comparison table and asks you to type `YES` before writing anything. Nothing happens silently.

**Test on one song:**
```
python sync_itunes_ratings.py --song "Comfortably Numb"
```

**Test on an album:**
```
python sync_itunes_ratings.py --album "The Wall"
```

**Run on your whole library** (once you trust the results):
```
python sync_itunes_ratings.py --full-library
```

**If your XML isn't in the default location:**
```
python sync_itunes_ratings.py --full-library --xml "D:\path\to\iTunes Music Library.xml"
```

## Useful options

| Flag | What it does |
|---|---|
| `--show set` | Only print rows that will actually change (also: `clear`, `nochange`, `skip`, `changes`, `all`) |
| `--no-clear` | Don't clear a file's rating just because iTunes has none — leave it alone instead |

## Undo a run

Every run prints a manifest path when it finishes:
```
python sync_itunes_ratings.py --restore "backups\2026-08-11_115233\manifest.csv"
```
Restores every file it touched back to its exact original state.

## What it will and won't touch

- ✅ Writes only iTunes' rating, only into its own two tag slots
- ✅ Preserves existing play counts on those tags
- ✅ Leaves every other tag (title, artist, artwork, ratings from other apps) completely alone
- ⏭️ Skips unreadable files, missing files, and any iTunes rating that isn't one of the standard 0/1/2/3/4/5 stars — reported in the table, never guessed at
- 🔒 Never opens or writes to iTunes itself — read-only against the XML

## Typical first run

```
python sync_itunes_ratings.py --song "one song you know the rating of"
   → check the table, type YES, verify the file
python sync_itunes_ratings.py --album "one album"
   → same check
python sync_itunes_ratings.py --full-library
   → same check, now for everything
```
