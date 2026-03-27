#!/usr/bin/env python3

import re
import shlex
import subprocess
import sys
import argparse
from pathlib import Path

RIP_ROOT = Path("/mnt/Jellyfin/RippedShows")
TEMP_ROOT = Path("/mnt/Jellyfin/Temp")
TV_ROOT = Path("/mnt/Jellyfin/Tv Shows")

# Change if your optical drive is not disc:0
DISC_ID = "disc:0"

# Change this if you want TheTVDB instead
FILEBOT_DB = "TheMovieDB::TV"

def parse_args():
    parser = argparse.ArgumentParser(description="DVD ripper pipeline")
    
    parser.add_argument("--show", type=str, help="Show name")
    parser.add_argument("--season", type=int, help="Season number")
    
    return parser.parse_args()


def run_command(cmd, check=True):
    print(f"\n>>> Running: {' '.join(shlex.quote(str(c)) for c in cmd)}")
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if check and result.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {result.returncode}")
    return result


def get_disc_info():
    result = run_command(["makemkvcon", "-r", "info", DISC_ID])
    return result.stdout


def extract_disc_label(info_text):
    for line in info_text.splitlines():
        if not line.startswith("DRV:"):
            continue

        m = re.match(
            r'^DRV:(\d+),(\d+),(\d+),(\d+),"([^"]*)","([^"]*)","([^"]*)"$',
            line.strip()
        )
        if m:
            visible = m.group(4)
            label = m.group(6).strip()
            device = m.group(7).strip()
            if visible == "1" and label and device:
                return label, device

    raise RuntimeError("Could not find active disc label from makemkvcon output.")


def parse_show_and_disc(label):
    """
    Example:
      ADVENTURE TIME DISC 2
    """
    m = re.match(r"^(.*?)\s+DISC\s+(\d+)$", label.strip(), re.IGNORECASE)
    if m:
        show_name = normalize_title_case(m.group(1).strip())
        disc_num = int(m.group(2))
        return show_name, disc_num

    # Fallback if DISC ## isn't present
    return normalize_title_case(label.strip()), None


def normalize_title_case(name):
    # Basic cleanup; preserves user override later if desired
    words = name.split()
    return " ".join(w.capitalize() for w in words)


def prompt_nonempty(prompt_text, default=None):
    while True:
        if default is not None:
            val = input(f"{prompt_text} [{default}]: ").strip()
            if not val:
                return default
        else:
            val = input(f"{prompt_text}: ").strip()
            if val:
                return val
        if val:
            return val
        print("Please enter a value.")


def prompt_int(prompt_text, default=None):
    while True:
        raw = prompt_nonempty(prompt_text, str(default) if default is not None else None)
        try:
            return int(raw)
        except ValueError:
            print("Please enter a whole number.")


def parse_chapter_groups(raw):
    """
    Accepts:
      1-3,4-6,7-9
      1:3,4:6,7:9
      1-1,2-2,3-3
    Returns:
      [(1,3), (4,6), (7,9)]
    """
    groups = []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("No chapter groups entered.")

    for part in parts:
        m = re.match(r"^(\d+)\s*[-:]\s*(\d+)$", part)
        if not m:
            raise ValueError(
                f"Invalid group '{part}'. Use format like 1-3,4-6,7-9"
            )
        start = int(m.group(1))
        end = int(m.group(2))
        if end < start:
            raise ValueError(f"Invalid range '{part}': end before start.")
        groups.append((start, end))

    return groups


def make_dirs(show_name, season_num, disc_num):
    season_label = f"Season {season_num:02d}"
    disc_label = f"Disc {disc_num:02d}"

    rip_dir = RIP_ROOT / show_name / season_label / disc_label
    temp_dir = TEMP_ROOT / show_name / season_label / disc_label
    tv_dir = TV_ROOT / show_name / season_label

    rip_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    tv_dir.mkdir(parents=True, exist_ok=True)

    return rip_dir, temp_dir, tv_dir


def rip_disc(rip_dir):
    """
    Rip all titles so you can inspect if needed.
    If you later find the correct title index is always predictable,
    this can be changed to rip only one title.
    """
    run_command([
        "makemkvcon",
        "mkv",
        DISC_ID,
        "all",
        str(rip_dir)
    ])


def choose_source_mkv(rip_dir):
    mkvs = sorted(rip_dir.glob("*.mkv"))
    if not mkvs:
        raise RuntimeError(f"No MKV files found in {rip_dir}")

    if len(mkvs) == 1:
        print(f"Using only MKV found: {mkvs[0].name}")
        return mkvs[0]

    print("\nMultiple MKVs found:")
    for i, f in enumerate(mkvs, 1):
        size_gb = f.stat().st_size / (1024 ** 3)
        print(f"  {i}. {f.name} ({size_gb:.2f} GiB)")

    idx = prompt_int("Choose source MKV number") - 1
    if idx < 0 or idx >= len(mkvs):
        raise RuntimeError("Invalid MKV selection.")
    return mkvs[idx]


def encode_episodes(source_mkv, temp_dir, show_name, season_num, first_episode_num, chapter_groups):
    out_files = []

    for offset, (ch_start, ch_end) in enumerate(chapter_groups):
        ep_num = first_episode_num + offset
        out_file = temp_dir / f"{show_name} - S{season_num:02d}E{ep_num:02d}.mkv"

        run_command([
            "HandBrakeCLI",
            "-i", str(source_mkv),
            "-o", str(out_file),
            "--chapters", f"{ch_start}:{ch_end}"
        ])

        out_files.append(out_file)

    return out_files


def rename_and_move_with_filebot(temp_dir, tv_root, show_name):
    """
    FileBot will use the SxxEyy numbers in filenames to fetch titles and move files.
    """
    format_str = "{n}/Season {s.pad(2)}/{n} - {s00e00} - {t}"

    run_command([
        "filebot",
        "-rename",
        str(temp_dir),
        "--db", FILEBOT_DB,
        "--q", show_name,
        "--output", str(tv_root),
        "--action", "move",
        "--format", format_str
    ])


def process_disc(state, args):
    print("Detecting disc...")
    info = get_disc_info()
    detected_label, device = extract_disc_label(info)

    print(f"\nDetected disc label: {detected_label}")
    print(f"Detected device: {device}")

    detected_show, detected_disc = parse_show_and_disc(detected_label)
    
    # Show Name
    show_name = (
        args.show
        or state.get("show_name")
        or prompt_nonempty("Show name", detected_show)

    )
    
    # Season
    season_num = (
        args.season
        or state.get("season_num")
        or prompt_int("Season # loaded in tray")
    )
    # Disc
    disc_num = prompt_int(
        "Disc number",
        detected_disc if detected_disc is not None else None
    )
    #Chapter Groups
    chapter_text = state.get("chapter_text")
    if not chapter_text:
        chapter_text = prompt_nonempty(
            "Chapter groups (example: 1-3,4-6,7-9)"
        )
    chapter_groups = parse_chapter_groups(chapter_text)
    
    #Episode Numbering
    first_episode_num = prompt_int(
        "First episode number on this disc",
        state.get("next_episode")
    )

    print("\nPlanned episode mapping:")
    for i, (start_ch, end_ch) in enumerate(chapter_groups):
        ep = first_episode_num + i
        print(f"  S{season_num:02d}E{ep:02d} <- chapters {start_ch}-{end_ch}")

    confirm = prompt_nonempty("Continue? (y/n)", "y").lower()
    if confirm not in ("y", "yes"):
        print("Cancelled.")
        return

    rip_dir, temp_dir, tv_dir = make_dirs(show_name, season_num, disc_num)

    print(f"\nRip directory:  {rip_dir}")
    print(f"Temp directory: {temp_dir}")
    print(f"TV directory:   {tv_dir}")

    rip_disc(rip_dir)
    source_mkv = choose_source_mkv(rip_dir)

    print(f"\nSelected source MKV: {source_mkv}")
    encode_episodes(
        source_mkv=source_mkv,
        temp_dir=temp_dir,
        show_name=show_name,
        season_num=season_num,
        first_episode_num=first_episode_num,
        chapter_groups=chapter_groups
    )

    rename_and_move_with_filebot(temp_dir, TV_ROOT, show_name)
    
    # Save state for next disc
    state["show_name"] = show_name
    state["season_num"] = season_num
    state["chapter_text"] = chapter_text
    state["next_episode"] = first_episode_num + len(chapter_groups)

    print("\nDone.")

def main():
    args = parse_args()
    state = {}

    print("Starting ripper... Press Ctrl+C to exit.\n")

    while True:
        try:
            input("\nInsert disc and press ENTER to continue...")
            process_disc(state, args)

        except KeyboardInterrupt:
            print("\nExiting.")
            break

        except Exception as e:
            print(f"\nError: {e}")
            print("Continuing to next disc...")
                
                
if __name__ == "__main__":
    main()
