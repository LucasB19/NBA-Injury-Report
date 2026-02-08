#!/usr/bin/env python3
"""Sync NBA team logos and player headshots for the dashboard.

Copyright (c) 2026 Lucas Berry.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import argparse
from pathlib import Path

import requests

try:
  from scripts.assets.sync_player_headshots import SOURCES, sync_headshots
except ModuleNotFoundError:
  from sync_player_headshots import SOURCES, sync_headshots

TEAM_LOGO_CODES = (
    "atl",
    "bkn",
    "bos",
    "cha",
    "chi",
    "cle",
    "dal",
    "den",
    "det",
    "gs",
    "hou",
    "ind",
    "lac",
    "lal",
    "mem",
    "mia",
    "mil",
    "min",
    "no",
    "ny",
    "okc",
    "orl",
    "phi",
    "phx",
    "por",
    "sa",
    "sac",
    "tor",
    "utah",
    "wsh",
)
TEAM_LOGO_URL_TEMPLATE = "https://a.espncdn.com/i/teamlogos/nba/500/{code}.png"
TEAM_LOGO_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}


def team_logo_url(code: str) -> str:
  return TEAM_LOGO_URL_TEMPLATE.format(code=code)


def sync_team_logos(
    output_dir: Path = Path("assets/team_logos"),
    force: bool = False,
    timeout: int = 20,
) -> tuple[int, int, int]:
  output_dir.mkdir(parents=True, exist_ok=True)
  downloaded = 0
  skipped = 0
  failed = 0
  session = requests.Session()
  session.headers.update(TEAM_LOGO_HEADERS)

  for code in TEAM_LOGO_CODES:
    target = output_dir / f"{code}.png"
    if target.exists() and not force:
      skipped += 1
      continue

    try:
      response = session.get(team_logo_url(code), timeout=timeout)
      if response.status_code == 200 and response.content:
        target.write_bytes(response.content)
        downloaded += 1
      else:
        failed += 1
    except Exception:
      failed += 1

  return downloaded, skipped, failed


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "--only",
      choices=("all", "logos", "players"),
      default="all",
      help="Sync both assets (all), team logos only, or player headshots only.",
  )
  parser.add_argument(
      "--players-source",
      choices=SOURCES,
      default="active-and-reports",
      help="Player pool used for headshots.",
  )
  parser.add_argument(
      "--data-dir",
      type=Path,
      default=Path("data"),
      help="Directory containing Injury-Report_*.csv files for active-and-reports mode.",
  )
  parser.add_argument(
      "--max-workers",
      type=int,
      default=20,
      help="Concurrent workers used for player headshot downloads.",
  )
  parser.add_argument(
      "--force-logos",
      action="store_true",
      help="Re-download team logos even when files already exist.",
  )
  return parser.parse_args()


def main() -> int:
  args = parse_args()
  failed_total = 0

  if args.only in {"all", "logos"}:
    downloaded, skipped, failed = sync_team_logos(force=args.force_logos)
    failed_total += failed
    print(f"Team logos downloaded: {downloaded}")
    print(f"Team logos skipped: {skipped}")
    print(f"Team logos failed: {failed}")

  if args.only in {"all", "players"}:
    total, success, missing, failed = sync_headshots(
        source=args.players_source,
        data_dir=args.data_dir,
        max_workers=max(1, args.max_workers),
    )
    failed_total += failed
    print(f"Players considered: {total}")
    print(f"Headshots downloaded: {success}")
    print(f"Headshots missing (404/410): {missing}")
    print(f"Headshots failed: {failed}")

  return 0 if failed_total == 0 else 1


if __name__ == "__main__":
  raise SystemExit(main())
