#!/usr/bin/env python3
"""Download NBA player headshots and build player-name lookup map.

Copyright (c) 2026 Lucas Berry.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from nba_api.stats.static import players as nba_players

HEADSHOT_URL_TEMPLATE = "https://ak-static.cms.nba.com/wp-content/uploads/headshots/nba/latest/260x190/{player_id}.png"
OUT_DIR = Path("assets/player_headshots")
MAP_PATH = OUT_DIR / "player_name_map.json"
DEFAULT_DATA_DIR = Path("data")
DEFAULT_WORKERS = 20
SOURCES = ("active-and-reports", "active", "all")

HEADSHOT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://official.nba.com/nba-injury-report-2025-26-season/",
    "Origin": "https://official.nba.com",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}
THREAD_LOCAL = threading.local()
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def strip_accents(value: str) -> str:
  normalized = unicodedata.normalize("NFKD", value or "")
  return "".join(char for char in normalized if not unicodedata.combining(char))


def normalize_key(value: str) -> str:
  compact = strip_accents(value).strip().lower()
  compact = compact.replace("\u00a0", " ")
  compact = re.sub(r"[.'`]", "", compact)
  compact = compact.replace("-", " ")
  compact = re.sub(r"\s+", " ", compact)
  return compact


def strip_suffix(value: str) -> str:
  parts = normalize_key(value).split()
  if parts and parts[-1] in SUFFIXES:
    parts = parts[:-1]
  return " ".join(parts)


def build_name_keys(player: dict[str, object]) -> set[str]:
  first_name = str(player.get("first_name") or "").strip()
  last_name = str(player.get("last_name") or "").strip()
  full_name = str(player.get("full_name") or "").strip()

  keys = {
      normalize_key(full_name),
      normalize_key(f"{last_name}, {first_name}"),
      normalize_key(f"{last_name} {first_name}"),
      normalize_key(f"{first_name} {last_name}"),
  }

  last_no_suffix = strip_suffix(last_name)
  full_no_suffix = strip_suffix(full_name)
  if last_no_suffix:
    keys.update(
        {
            normalize_key(f"{last_no_suffix}, {first_name}"),
            normalize_key(f"{last_no_suffix} {first_name}"),
            normalize_key(f"{first_name} {last_no_suffix}"),
        }
    )
  if full_no_suffix:
    keys.add(normalize_key(full_no_suffix))

  return {key for key in keys if key}


def build_file_stem(value: str) -> str:
  base = normalize_key(value)
  base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
  return base or "player"


def headshot_filename(player: dict[str, object]) -> str:
  player_id = int(player["id"])
  full_name = str(player.get("full_name") or "").strip()
  stem = build_file_stem(full_name)
  return f"{stem}-{player_id}.png"


def thread_session() -> requests.Session:
  session = getattr(THREAD_LOCAL, "session", None)
  if session is None:
    session = requests.Session()
    session.headers.update(HEADSHOT_HEADERS)
    THREAD_LOCAL.session = session
  return session


def download_headshot(player: dict[str, object]) -> tuple[str, str, bytes]:
  player_id = int(player["id"])
  url = HEADSHOT_URL_TEMPLATE.format(player_id=player_id)
  session = thread_session()
  for attempt in range(3):
    try:
      response = session.get(url, timeout=20)
    except Exception:
      if attempt < 2:
        time.sleep(0.4 * (attempt + 1))
        continue
      return "failed", "", b""
    if response.status_code == 200 and response.content:
      return "success", headshot_filename(player), response.content
    if response.status_code in {404, 410}:
      return "missing", "", b""
    if response.status_code == 403 and attempt < 2:
      time.sleep(0.4 * (attempt + 1))
      continue
    return "failed", "", b""
  return "failed", "", b""


def load_names_from_report_csvs(data_dir: Path) -> set[str]:
  names: set[str] = set()
  if not data_dir.exists():
    return names
  for csv_path in data_dir.glob("Injury-Report_*.csv"):
    try:
      with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
          player = (row.get("player") or row.get("Player Name") or "").strip()
          if player:
            names.add(player)
    except Exception:
      continue
  return names


def build_player_index(players: list[dict[str, object]]) -> dict[str, dict[str, object]]:
  index: dict[str, dict[str, object]] = {}
  for player in players:
    for key in build_name_keys(player):
      index.setdefault(key, player)
  return index


def select_players(source: str, data_dir: Path) -> list[dict[str, object]]:
  if source == "all":
    return nba_players.get_players()
  active_players = nba_players.get_active_players()
  if source == "active":
    return active_players

  all_players = nba_players.get_players()
  index_by_name = build_player_index(all_players)
  selected: dict[int, dict[str, object]] = {int(player["id"]): player for player in active_players}
  for raw_name in load_names_from_report_csvs(data_dir):
    key = normalize_key(raw_name)
    matched = index_by_name.get(key)
    if matched is None:
      continue
    selected.setdefault(int(matched["id"]), matched)
  return list(selected.values())


def sync_headshots(
    source: str = "active-and-reports",
    data_dir: Path = DEFAULT_DATA_DIR,
    max_workers: int = DEFAULT_WORKERS,
) -> tuple[int, int, int, int]:
  OUT_DIR.mkdir(parents=True, exist_ok=True)
  players = select_players(source, data_dir)
  success = 0
  missing = 0
  failed = 0
  player_name_map: dict[str, str] = {}

  def worker(player: dict[str, object]) -> tuple[dict[str, object], str, str, bytes]:
    status, filename, content = download_headshot(player)
    return player, status, filename, content

  with ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = [executor.submit(worker, player) for player in players]
    for future in as_completed(futures):
      player, status, filename, content = future.result()
      if status == "success":
        output_path = OUT_DIR / filename
        output_path.write_bytes(content)
        success += 1
        for key in build_name_keys(player):
          player_name_map.setdefault(key, filename)
      elif status == "missing":
        missing += 1
      else:
        failed += 1

  if success > 0:
    MAP_PATH.write_text(json.dumps(player_name_map, ensure_ascii=True, sort_keys=True, indent=2), encoding="utf-8")
  return len(players), success, missing, failed


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--source",
      choices=SOURCES,
      default="active-and-reports",
      help="Choose player pool: active-and-reports (default), active, or all.",
  )
  parser.add_argument(
      "--data-dir",
      type=Path,
      default=DEFAULT_DATA_DIR,
      help="Directory containing Injury-Report_*.csv files used by active-and-reports mode.",
  )
  parser.add_argument(
      "--max-workers",
      type=int,
      default=DEFAULT_WORKERS,
      help="Number of concurrent download workers.",
  )
  return parser.parse_args()


def main() -> int:
  args = parse_args()
  total, success, missing, failed = sync_headshots(
      source=args.source,
      data_dir=args.data_dir,
      max_workers=max(1, args.max_workers),
  )
  print(f"Players considered: {total}")
  print(f"Headshots downloaded: {success}")
  print(f"Headshots missing (404/410): {missing}")
  print(f"Headshots failed: {failed}")
  print(f"Name map written: {MAP_PATH}")
  return 0 if failed == 0 else 1


if __name__ == "__main__":
  raise SystemExit(main())
