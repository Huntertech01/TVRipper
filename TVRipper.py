#!/usr/bin/env python3

import argparse
import re
import shlex
import subprocess
import sys
from pathlib import Path

RIP_ROOT = Path("/mnt/Jellyfin/RippedShows")
TEMP_ROOT = Path("/mnt/Jellyfin/Temp")
TV_ROOT = Path("/mnt/Jellyfin/Tv Shows")

DISC_ID = "disc:0"
FILEBOT_DB = "TheMovieDB::TV"


def parse_args():
    parser = argparse.ArgumentParser(description="DVD ripper pipeline")
    parser.add_argument("--show", type=str, help="Show name")
    parser.add_argument("--season", type=int, help="Season number")
    parser.add_argument(
        "--group-size",
        type=int,
        default=3,
        help="Auto chapter group size per episode (default: 3)"
    )
    parser.add_argument(
        "--junk-threshold",
        type=int,
        default=15,
        help="Ignore final chapter if shorter than this many seconds (default: 15)"
    )
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
    m = re.match(r"^(.*?)\s+DISC\s+(\d+)$", label.strip(), re.IGNORECASE)
    if m:
        show_name = normalize_title_case(m.group(1).strip())
        disc_num = int(m.group(2))
        return show_name, disc_num

    return normalize_title_case(label.strip()), None


def normalize_title_case(name):
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


def prompt_yes_no(prompt_text, default="y"):
    while True:
        val = prompt_nonempty(prompt_text, default).strip().lower()
        if val in ("y", "yes"):
            return True
        if val in ("n", "no"):
            return False
        print("Please enter y or n.")


def parse_chapter_groups(raw):
    groups = []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("No chapter groups entered.")

    for part in parts:
        m = re.match(r"^(\d+)\s*[-:]\s*(\d+)$", part)
        if not m:
            raise ValueError(f"Invalid group '{part}'. Use format like 1-3,4-6,7-9")
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


def scan_chapters_with_handbrake(source_mkv):
    result = run_command([
        "HandBrakeCLI",
        "-i", str(source_mkv),
        "--scan"
    ], check=False)

    scan_text = (result.stdout or "") + "\n" + (result.stderr or "")
    chapters = []

    for line in scan_text.splitlines():
        m = re.search(r"\+\s+(\d+):\s+duration\s+([0-9:]+)", line)
        if m:
            chapters.append((int(m.group(1)), m.group(2)))

    return chapters


def print_chapter_list(chapters):
    if not chapters:
        print("\nNo chapters found from HandBrake scan.")
        return

    print("\nDetected chapters:")
    for num, duration in chapters:
        print(f"  Chapter {num:02d}: {duration}")

    print(f"\nTotal chapters detected: {len(chapters)}")


def duration_to_seconds(duration_str):
    parts = [int(p) for p in duration_str.split(":")]
    if len(parts) == 3:
        h, m, s = parts
        return h * 3600 + m * 60 + s
    if len(parts) == 2:
        m, s = parts
        return m * 60 + s
    return 0


def auto_group_chapters_by_duration(chapters, group_size=3, min_last_chapter_seconds=15):
    if not chapters:
        return []

    filtered = list(chapters)

    # Drop very short final chapter as likely junk
    last_num, last_duration = filtered[-1]
    if duration_to_seconds(last_duration) < min_last_chapter_seconds:
        filtered = filtered[:-1]

    if not filtered:
        return []

    groups = []
    start = filtered[0][0]
    max_ch = filtered[-1][0]

    while start + group_size - 1 <= max_ch:
        end = start + group_size - 1
        groups.append((start, end))
        start += group_size

    return groups


def chapter_groups_to_text(groups):
    return ",".join(f"{start}-{end}" for start, end in groups)


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


def get_chapter_groups_from_user_or_auto(source_mkv, state, args):
    chapters = scan_chapters_with_handbrake(source_mkv)
    print_chapter_list(chapters)

    auto_groups = auto_group_chapters_by_duration(
        chapters,
        group_size=args.group_size,
        min_last_chapter_seconds=args.junk_threshold
    )
    auto_text = chapter_groups_to_text(auto_groups)

    if auto_text:
        print(f"\nSuggested chapter groups: {auto_text}")

    previous = state.get("chapter_text")

    if previous:
        if prompt_yes_no(f"Reuse previous chapter groups [{previous}]? (y/n)", "y"):
            return previous, parse_chapter_groups(previous)

    if auto_text:
        if prompt_yes_no("Use suggested chapter groups? (y/n)", "y"):
            return auto_text, auto_groups

    max_ch = chapters[-1][0] if chapters else "unknown"
    chapter_text = prompt_nonempty(
        f"Chapter groups (1-{max_ch}, example: 1-3,4-6,7-9)"
    )
    return chapter_text, parse_chapter_groups(chapter_text)


def process_disc(state, args):
    print("Detecting disc...")
    info = get_disc_info()
    detected_label, device = extract_disc_label(info)

    print(f"\nDetected disc label: {detected_label}")
    print(f"Detected device: {device}")

    detected_show, detected_disc = parse_show_and_disc(detected_label)

    show_name = (
        args.show
        or state.get("show_name")
        or prompt_nonempty("Show name", detected_show)
    )

    season_num = (
        args.season
        or state.get("season_num")
        or prompt_int("Season number loaded in tray")
    )

    disc_num = prompt_int(
        "Disc number",
        detected_disc if detected_disc is not None else None
    )

    rip_dir, temp_dir, tv_dir = make_dirs(show_name, season_num, disc_num)

    print(f"\nRip directory:  {rip_dir}")
    print(f"Temp directory: {temp_dir}")
    print(f"TV directory:   {tv_dir}")

    confirm = prompt_yes_no("Start rip? (y/n)", "y")
    if not confirm:
        print("Cancelled.")
        return

    rip_disc(rip_dir)
    source_mkv = choose_source_mkv(rip_dir)

    print(f"\nSelected source MKV: {source_mkv}")

    chapter_text, chapter_groups = get_chapter_groups_from_user_or_auto(source_mkv, state, args)

    first_episode_num = prompt_int(
        "First episode number on this disc",
        state.get("next_episode")
    )

    print("\nPlanned episode mapping:")
    for i, (start_ch, end_ch) in enumerate(chapter_groups):
        ep = first_episode_num + i
        print(f"  S{season_num:02d}E{ep:02d} <- chapters {start_ch}-{end_ch}")

    confirm = prompt_yes_no("Continue to encode and rename? (y/n)", "y")
    if not confirm:
        print("Cancelled.")
        return

    encode_episodes(
        source_mkv=source_mkv,
        temp_dir=temp_dir,
        show_name=show_name,
        season_num=season_num,
        first_episode_num=first_episode_num,
        chapter_groups=chapter_groups
    )

    rename_and_move_with_filebot(temp_dir, TV_ROOT, show_name)

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