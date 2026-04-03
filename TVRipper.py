#!/usr/bin/env python3

import argparse
import difflib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent / "config.json"

DISC_ID = "disc:0"
TMDB_API_BASE = "https://api.themoviedb.org/3"


def parse_args():
    parser = argparse.ArgumentParser(description="DVD ripper pipeline with local episode list + TMDB mapping")
    parser.add_argument("--show", type=str, help="Show name")
    parser.add_argument("--season", type=int, help="Season number printed on the disc/box section")
    parser.add_argument("--group-size", type=int, default=3, help="Auto chapter group size per episode (default: 3)")
    parser.add_argument("--junk-threshold", type=int, default=15, help="Ignore final chapter if shorter than this many seconds (default: 15)")
    parser.add_argument("--episode-file", type=str, help="Optional path to the local episode list file")
    parser.add_argument("--tmdb-api-key", type=str, help="TMDB API key. If omitted, uses TMDB_API_KEY environment variable")
    parser.add_argument("--reset-config", action="store_true", help="Reset saved directory configuration")
    return parser.parse_args()

def load_or_create_config(reset=False):
    if CONFIG_FILE.exists() and not reset:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
        return {
            "rip_root": Path(data["rip_root"]),
            "temp_root": Path(data["temp_root"]),
            "tv_root": Path(data["tv_root"]),
        }

    print("\nFirst-time setup: configure directories\n")

    rip_root = Path(prompt_nonempty("Path for ripped MKVs (RippedShows)")).expanduser()
    temp_root = Path(prompt_nonempty("Path for temp encodes (Temp)")).expanduser()
    tv_root = Path(prompt_nonempty("Path for Jellyfin TV Shows")).expanduser()

    data = {
        "rip_root": str(rip_root),
        "temp_root": str(temp_root),
        "tv_root": str(tv_root),
    }

    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nSaved config to {CONFIG_FILE}\n")

    return {
        "rip_root": rip_root,
        "temp_root": temp_root,
        "tv_root": tv_root,
    }
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

        m = re.match(r'^DRV:(\d+),(\d+),(\d+),(\d+),"([^"]*)","([^"]*)","([^"]*)"$', line.strip())
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
    return " ".join(part.capitalize() for part in name.split())


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


def make_dirs(show_name, season_num, disc_num, config):
	season_label = f"Season {season_num:02d}"
	disc_label = f"Disc {disc_num:02d}"

	rip_dir = config["rip_root"] / show_name / season_label / disc_label
	temp_dir = config["temp_root"] / show_name / season_label / disc_label
	tv_dir = config["tv_root"] / show_name

	rip_dir.mkdir(parents=True, exist_ok=True)
	temp_dir.mkdir(parents=True, exist_ok=True)
	tv_dir.mkdir(parents=True, exist_ok=True)

	return rip_dir, temp_dir, tv_dir


def find_existing_mkvs(rip_dir):
    return sorted(rip_dir.glob("*.mkv"))


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
    result = run_command(["HandBrakeCLI", "-i", str(source_mkv), "--scan"], check=False)
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
        if prompt_yes_no(f"Reuse previous chapter groups [{previous}]? (y/n)", "n"):
            return previous, parse_chapter_groups(previous)

    if auto_text:
        if prompt_yes_no(f"Use suggested chapter groups based on group size {args.group_size}? (y/n)", "y"):
            return auto_text, auto_groups

    max_ch = chapters[-1][0] if chapters else "unknown"
    raw = prompt_nonempty(
        f"Chapter groups or group size (1-{max_ch}, examples: 1-3,4-6,7-9 or just 3)"
    )

    if raw.isdigit():
        manual_group_size = int(raw)
        groups = auto_group_chapters_by_duration(
            chapters,
            group_size=manual_group_size,
            min_last_chapter_seconds=args.junk_threshold
        )
        text = chapter_groups_to_text(groups)
        print(f"\nGenerated chapter groups from group size {manual_group_size}: {text}")
        return text, groups

    return raw, parse_chapter_groups(raw)


def script_dir():
    return Path(__file__).resolve().parent


def slugify(value):
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def find_boxeset_file(show_name, explicit_path=None):
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"Episode file not found: {path}")
        return path

    base_names = [
        show_name,
        show_name.replace(" ", ""),
        slugify(show_name),
    ]
    exts = ["", ".md", ".txt"]

    for base in base_names:
        for ext in exts:
            candidate = script_dir() / f"{base}{ext}"
            if candidate.exists():
                return candidate

    raise RuntimeError(
        f"Could not find an episode list file in {script_dir()} for show '{show_name}'. "
        f"Try naming it '{show_name}.md' or pass --episode-file."
    )


def parse_boxeset_file(path):
    sections = {}
    current_season = None
    current_disc = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = html.unescape(raw_line).replace("\t", " ").strip()
        if not line:
            continue

        line = re.sub(r"^[\-\*\u2022]+\s*", "", line).strip()

        season_match = re.match(r"^Season\s+(\d+)$", line, re.IGNORECASE)
        if season_match:
            current_season = int(season_match.group(1))
            current_disc = None
            continue

        disc_match = re.match(r"^Disc\s+(\d+)$", line, re.IGNORECASE)
        if disc_match:
            if current_season is None:
                raise RuntimeError(f"Found disc before season in episode file: {path}")
            current_disc = int(disc_match.group(1))
            sections[(current_season, current_disc)] = []
            continue

        if current_season is not None and current_disc is not None:
            sections[(current_season, current_disc)].append(line)

    if not sections:
        raise RuntimeError(f"No season/disc sections found in episode file: {path}")
    return sections


def tmdb_api_get(endpoint, api_key, params=None):
    params = dict(params or {})
    params["api_key"] = api_key
    url = f"{TMDB_API_BASE}{endpoint}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def tmdb_cache_path(show_name):
    return script_dir() / f".tmdb_cache_{slugify(show_name)}.json"


def load_or_fetch_tmdb_episodes(show_name, api_key):
    cache_file = tmdb_cache_path(show_name)
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))

    search_data = tmdb_api_get("/search/tv", api_key, {"query": show_name})
    results = search_data.get("results", [])
    if not results:
        raise RuntimeError(f"TMDB could not find a show named '{show_name}'")

    show = results[0]
    show_id = show["id"]

    details = tmdb_api_get(f"/tv/{show_id}", api_key)
    number_of_seasons = details.get("number_of_seasons", 0)

    episodes = []
    for season_num in range(1, number_of_seasons + 1):
        season_details = tmdb_api_get(f"/tv/{show_id}/season/{season_num}", api_key)
        for ep in season_details.get("episodes", []):
            episodes.append({
                "season": ep["season_number"],
                "episode": ep["episode_number"],
                "name": ep["name"],
            })

    payload = {
        "show_name": show_name,
        "tmdb_show_id": show_id,
        "episodes": episodes,
    }
    cache_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def normalize_match_text(text):
    text = html.unescape(text)
    text = text.lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[’'`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


TITLE_OVERRIDES = {
    "mirror gen": "mirror gem",
    "ocean gen": "ocean gem",
    "chille tid": "chille tid",
    "cant go back": "cant go back",
}


def apply_title_override(title):
    key = normalize_match_text(title)
    return TITLE_OVERRIDES.get(key, title)


def match_disc_titles_to_tmdb(disc_titles, tmdb_payload):
    episodes = tmdb_payload["episodes"]
    normalized_map = {}
    for ep in episodes:
        normalized_map[normalize_match_text(ep["name"])] = ep

    matched = []
    for title in disc_titles:
        cleaned_title = apply_title_override(title)
        norm = normalize_match_text(cleaned_title)

        if norm in normalized_map:
            ep = normalized_map[norm]
            matched.append({
                "box_title": title,
                "tmdb_title": ep["name"],
                "season": ep["season"],
                "episode": ep["episode"],
            })
            continue

        candidates = []
        for ep in episodes:
            score = difflib.SequenceMatcher(None, norm, normalize_match_text(ep["name"])).ratio()
            candidates.append((score, ep))

        best_score, best_ep = max(candidates, key=lambda x: x[0])
        if best_score < 0.78:
            raise RuntimeError(
                f"Could not confidently match '{title}' to TMDB. Best match was '{best_ep['name']}' "
                f"(score {best_score:.2f})."
            )

        print(
            f"Fuzzy-matched '{title}' -> "
            f"S{best_ep['season']:02d}E{best_ep['episode']:02d} '{best_ep['name']}' "
            f"(score {best_score:.2f})"
        )
        matched.append({
            "box_title": title,
            "tmdb_title": best_ep["name"],
            "season": best_ep["season"],
            "episode": best_ep["episode"],
        })

    return matched


def safe_filename(text):
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def encode_episodes(source_mkv, temp_dir, show_name, mapped_episodes, chapter_groups):
    encoded = []

    for (ch_start, ch_end), ep_info in zip(chapter_groups, mapped_episodes):
        season_num = ep_info["season"]
        episode_num = ep_info["episode"]
        title = ep_info["tmdb_title"]

        out_name = f"{show_name} - S{season_num:02d}E{episode_num:02d} - {safe_filename(title)}.mkv"
        out_file = temp_dir / out_name

        run_command([
            "HandBrakeCLI",
            "-i", str(source_mkv),
            "-o", str(out_file),
            "--chapters", f"{ch_start}:{ch_end}"
        ])

        encoded.append({
            "path": out_file,
            "season": season_num,
            "episode": episode_num,
            "title": title,
        })

    return encoded


def move_encoded_files_to_tv(encoded_files, show_name, config):
    moved = []

    for item in encoded_files:
        season_dir = config["tv_root"] / show_name / f"Season {item['season']:02d}"
        season_dir.mkdir(parents=True, exist_ok=True)

        final_name = f"{show_name} - S{item['season']:02d}E{item['episode']:02d} - {safe_filename(item['title'])}.mkv"
        final_path = season_dir / final_name

        if final_path.exists():
            if prompt_yes_no(f"{final_name} already exists. Overwrite? (y/n)", "n"):
                final_path.unlink()
            else:
                print(f"Skipping existing file: {final_path}")
                continue

        shutil.move(str(item["path"]), str(final_path))
        moved.append(final_path)

    return moved


def process_disc(state, args, config):
    api_key = args.tmdb_api_key or os.environ.get("TMDB_API_KEY")
    if not api_key:
        raise RuntimeError("TMDB API key not found. Pass --tmdb-api-key or set TMDB_API_KEY.")

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

    episode_file = find_boxeset_file(show_name, args.episode_file)
    sections = parse_boxeset_file(episode_file)

    if (season_num, disc_num) not in sections:
        raise RuntimeError(
            f"No entry for Season {season_num} Disc {disc_num} in episode file {episode_file.name}"
        )

    disc_titles = sections[(season_num, disc_num)]
    print(f"\nEpisode list file: {episode_file}")
    print(f"Found {len(disc_titles)} titles for Season {season_num} Disc {disc_num}")

    tmdb_payload = load_or_fetch_tmdb_episodes(show_name, api_key)
    mapped_episodes = match_disc_titles_to_tmdb(disc_titles, tmdb_payload)

    rip_dir, temp_dir, tv_dir = make_dirs(show_name, season_num, disc_num, config)

    print(f"\nRip directory:  {rip_dir}")
    print(f"Temp directory: {temp_dir}")
    print(f"TV directory:   {tv_dir}")

    source_mkv = None
    existing_mkvs = find_existing_mkvs(rip_dir)
    if existing_mkvs:
        print(f"\nFound {len(existing_mkvs)} existing MKV(s) in rip folder.")
        if prompt_yes_no("Reuse an existing rip instead of ripping again? (y/n)", "y"):
            source_mkv = choose_source_mkv(rip_dir)

    if source_mkv is None:
        confirm = prompt_yes_no("Start rip? (y/n)", "y")
        if not confirm:
            print("Cancelled.")
            return

        rip_disc(rip_dir)
        source_mkv = choose_source_mkv(rip_dir)

    print(f"\nSelected source MKV: {source_mkv}")

    chapter_text, chapter_groups = get_chapter_groups_from_user_or_auto(source_mkv, state, args)

    if len(chapter_groups) != len(mapped_episodes):
        raise RuntimeError(
            f"Chapter groups count ({len(chapter_groups)}) does not match disc episode count "
            f"from the episode file ({len(mapped_episodes)})."
        )

    print("\nPlanned episode mapping:")
    for (start_ch, end_ch), ep in zip(chapter_groups, mapped_episodes):
        print(
            f"  chapters {start_ch}-{end_ch} -> "
            f"S{ep['season']:02d}E{ep['episode']:02d} {ep['tmdb_title']}"
        )

    confirm = prompt_yes_no("Continue to encode and move files into Jellyfin TV Shows? (y/n)", "y")
    if not confirm:
        print("Cancelled.")
        return

    encoded_files = encode_episodes(
        source_mkv=source_mkv,
        temp_dir=temp_dir,
        show_name=show_name,
        mapped_episodes=mapped_episodes,
        chapter_groups=chapter_groups
    )

    moved_files = move_encoded_files_to_tv(encoded_files, show_name, config)

    print("\nMoved files:")
    for path in moved_files:
        print(f"  {path}")

    state["show_name"] = show_name
    state["season_num"] = season_num
    state["chapter_text"] = chapter_text

    print("\nDone.")


def main():
    args = parse_args()
    config = load_or_create_config(reset=args.reset_config)
    state = {}

    print("Starting ripper... Press Ctrl+C to exit.\n")

    while True:
        try:
            input("\nInsert disc and press ENTER to continue...")
            process_disc(state, args, config)

        except KeyboardInterrupt:
            print("\nExiting.")
            break
        except Exception as e:
            print(f"\nError: {e}")
            print("Continuing to next disc...")


if __name__ == "__main__":
    main()
