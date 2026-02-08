"""NBA Injury Report dashboard and extraction pipeline.

Copyright (c) 2026 Lucas Berry.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
import unicodedata
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup
from dash import Dash, Input, Output, State, dcc, html
import pandas as pd

Row = dict[str, Any]
Payload = dict[str, Any]


OFFICIAL_PAGE = "https://official.nba.com/nba-injury-report-2025-26-season/"
PDF_NAME_PATTERN = re.compile(r"Injury-Report_\d{4}-\d{2}-\d{2}_.+?\.pdf", re.I)
CACHE_LOGGER = logging.getLogger("injury_report")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

STATUS_ORDER = ["Out", "Doubtful", "Questionable", "Probable", "Available", "Not With Team"]
STATUS_DISPLAY_ORDER = ["Available", "Probable", "Questionable", "Doubtful", "Not With Team", "Out"]
STATUS_TOKENS = ["Not With Team", "Questionable", "Doubtful", "Probable", "Available", "Out"]
CACHE_TTL_SECONDS = 3600
TEAM_LOGO_CODE_BY_NAME = {
    "Atlanta Hawks": "atl",
    "Boston Celtics": "bos",
    "Brooklyn Nets": "bkn",
    "Charlotte Hornets": "cha",
    "Chicago Bulls": "chi",
    "Cleveland Cavaliers": "cle",
    "Dallas Mavericks": "dal",
    "Denver Nuggets": "den",
    "Detroit Pistons": "det",
    "Golden State Warriors": "gs",
    "Houston Rockets": "hou",
    "Indiana Pacers": "ind",
    "LA Clippers": "lac",
    "Los Angeles Clippers": "lac",
    "Los Angeles Lakers": "lal",
    "Memphis Grizzlies": "mem",
    "Miami Heat": "mia",
    "Milwaukee Bucks": "mil",
    "Minnesota Timberwolves": "min",
    "New Orleans Pelicans": "no",
    "New York Knicks": "ny",
    "Oklahoma City Thunder": "okc",
    "Orlando Magic": "orl",
    "Philadelphia 76ers": "phi",
    "Phoenix Suns": "phx",
    "Portland Trail Blazers": "por",
    "Sacramento Kings": "sac",
    "San Antonio Spurs": "sa",
    "Toronto Raptors": "tor",
    "Utah Jazz": "utah",
    "Washington Wizards": "wsh",
}

CACHE_LOCK = threading.Lock()
CACHE_STATE = {
    "data": None,
    "last_updated": 0
}
DATA_DIR = os.environ.get("PDF_STORAGE_DIR", "data")
PLAYER_HEADSHOT_DIR = os.path.join("assets", "player_headshots")
PLAYER_NAME_MAP_PATH = os.path.join(PLAYER_HEADSHOT_DIR, "player_name_map.json")


def normalize_player_name_key(value: str) -> str:
  normalized = unicodedata.normalize("NFKD", value or "")
  ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
  compact = ascii_value.strip().lower()
  compact = compact.replace("\u00a0", " ")
  compact = re.sub(r"[.'`]", "", compact)
  compact = compact.replace("-", " ")
  compact = re.sub(r"\s+", " ", compact)
  return compact


def load_player_name_map(path: str) -> dict[str, str]:
  if not os.path.exists(path):
    return {}
  try:
    with open(path, encoding="utf-8") as handle:
      payload = json.load(handle)
  except Exception:
    return {}
  loaded: dict[str, str] = {}
  for key, value in payload.items():
    normalized = normalize_player_name_key(str(key))
    if not normalized:
      continue
    if isinstance(value, int):
      loaded[normalized] = f"{value}.png"
      continue
    if isinstance(value, str):
      if value.endswith(".png"):
        loaded[normalized] = os.path.basename(value)
      elif value.isdigit():
        loaded[normalized] = f"{value}.png"
  return loaded


def available_headshot_files(headshot_dir: str) -> set[str]:
  if not os.path.isdir(headshot_dir):
    return set()
  return {name for name in os.listdir(headshot_dir) if name.endswith(".png")}


PLAYER_HEADSHOT_FILE_BY_NAME_KEY = load_player_name_map(PLAYER_NAME_MAP_PATH)
AVAILABLE_PLAYER_HEADSHOT_FILES = available_headshot_files(PLAYER_HEADSHOT_DIR)


def base_headers() -> dict[str, str]:
  return {
      "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
      "Accept": "text/html,application/pdf",
      "Accept-Language": "en-US,en;q=0.9"
  }


def pdf_headers() -> dict[str, str]:
  headers = base_headers()
  headers.update({
      "Accept": "application/pdf",
      "Referer": OFFICIAL_PAGE,
      "Origin": "https://official.nba.com",
      "Sec-Fetch-Dest": "document",
      "Sec-Fetch-Mode": "navigate",
      "Sec-Fetch-Site": "same-origin",
      "Upgrade-Insecure-Requests": "1"
  })
  return headers


def fetch_with_retry(
    url: str,
    timeout: int = 15,
    retries: int = 2,
    delay: float = 0.8,
    stream: bool = False,
    session: requests.Session | None = None,
) -> requests.Response:
  last_error = None
  for attempt in range(retries + 1):
    try:
      headers = base_headers()
      if session is None:
        response = requests.get(url, headers=headers, timeout=timeout, stream=stream)
      else:
        response = session.get(url, headers=headers, timeout=timeout, stream=stream)
      response.raise_for_status()
      return response
    except Exception as exc:  # noqa: BLE001
      last_error = exc
      if attempt < retries:
        time.sleep(delay * (attempt + 1))
  raise last_error


def fetch_pdf_with_retry(
    url: str,
    session: requests.Session,
    timeout: int = 20,
    retries: int = 3,
    delay: float = 1.0,
) -> requests.Response:
  last_error = None
  for attempt in range(retries + 1):
    try:
      response = session.get(
          url,
          headers=pdf_headers(),
          timeout=timeout,
          stream=True,
          allow_redirects=True
      )
      if response.status_code == 403:
        CACHE_LOGGER.warning("PDF request 403, warming up session and retrying.")
        session.get(OFFICIAL_PAGE, headers=base_headers(), timeout=timeout)
        time.sleep(delay * (attempt + 1))
        continue
      response.raise_for_status()
      return response
    except Exception as exc:  # noqa: BLE001
      last_error = exc
      if attempt < retries:
        time.sleep(delay * (attempt + 1))
  raise last_error


def parse_pdf_time_parts(url: str) -> tuple[str, int, int, str] | None:
  file_name = url.split("/")[-1]
  match = re.search(r"Injury-Report_(\d{4}-\d{2}-\d{2})_([0-9_:\-]{1,8})(AM|PM)", file_name, re.I)
  if not match:
    return None
  date_str, raw_time_str, meridiem = match.group(1), match.group(2), match.group(3).upper()
  digits = re.sub(r"\D", "", raw_time_str)
  if not digits:
    return None
  if len(digits) <= 2:
    hour = int(digits)
    minutes = 0
  else:
    hour = int(digits[:-2])
    minutes = int(digits[-2:])
  if hour < 1 or hour > 12 or minutes < 0 or minutes > 59:
    return None
  return date_str, hour, minutes, meridiem


def parse_pdf_datetime(url: str) -> int:
  parts = parse_pdf_time_parts(url)
  if not parts:
    return 0
  date_str, hour, minutes, meridiem = parts
  if meridiem == "PM" and hour < 12:
    hour += 12
  if meridiem == "AM" and hour == 12:
    hour = 0
  try:
    return int(datetime.strptime(f"{date_str}T{hour:02d}:{minutes:02d}:00", "%Y-%m-%dT%H:%M:%S").timestamp())
  except ValueError:
    return 0


def parse_pdf_time_label(url: str) -> str:
  parts = parse_pdf_time_parts(url)
  if not parts:
    return ""
  _, hour, minutes, meridiem = parts
  return f"{hour:02d}:{minutes:02d} {meridiem} ET"


def parse_pdf_date(url: str) -> str:
  file_name = url.split("/")[-1]
  match = re.search(r"Injury-Report_(\d{4})-(\d{2})-(\d{2})_", file_name)
  if not match:
    return ""
  year, month, day = match.group(1), match.group(2), match.group(3)
  return f"{month}/{day}/{year}"


def extract_pdf_links(html_text: str) -> list[str]:
  soup = BeautifulSoup(html_text, "html.parser")
  links = set()
  for link in soup.select("a[href$='.pdf']"):
    href = link.get("href")
    if href and PDF_NAME_PATTERN.search(href):
      links.add(urljoin(OFFICIAL_PAGE, href))
  inline_matches = PDF_NAME_PATTERN.findall(html_text)
  for match in inline_matches:
    links.add(urljoin(OFFICIAL_PAGE, match))
  return list(links)


def prefer_link(links: list[str]) -> str | None:
  if not links:
    return None
  ranked = sorted(
      links,
      key=lambda link: (parse_pdf_datetime(link), "ak-static.cms.nba.com" in link),
      reverse=True
  )
  return ranked[0]


def fetch_latest_pdf_link(session: requests.Session | None = None) -> str | None:
  html_response = fetch_with_retry(OFFICIAL_PAGE, timeout=12, retries=3, session=session)
  links = extract_pdf_links(html_response.text)
  if not links:
    return None
  return prefer_link(links)


def fallback_pdf_url(url: str) -> str | None:
  file_name = url.split("/")[-1]
  if not file_name:
    return None
  return f"https://ak-static.cms.nba.com/referee/injury/{file_name}"


def parse_rows_per_page(text: str, page_num: int = 1) -> list[Row]:
  """Version corrigée - détection correcte des continuations"""
  rows = []
  lines = [line.strip() for line in text.splitlines() if line.strip()]

  def is_header_or_footer(line):
    upper = line.upper()
    if "NBA INJURY REPORT" in upper:
      return True
    if "REPORT UPDATED" in upper:
      return True
    if upper.startswith("PAGE "):
      return True
    if upper in [
        "GAME DATE MATCHUP TEAM PLAYER STATUS REASON",
        "GAME TIME MATCHUP TEAM PLAYER STATUS REASON",
        "GAME DATE/TIME MATCHUP TEAM PLAYER STATUS REASON",
        "GAME DATE GAME TIME MATCHUP TEAM PLAYER NAME CURRENT STATUS REASON"
    ]:
      return True
    if re.match(
        r"^(GAME|DATE|TIME|MATCHUP|TEAM|PLAYER|STATUS|REASON)(\s+\|\s+|\s{3,})"
        r"(GAME|DATE|TIME|MATCHUP|TEAM|PLAYER|STATUS|REASON)",
        upper
    ):
      return True
    return False

  for line_index, line in enumerate(lines):
    if is_header_or_footer(line):
      continue
    
    if line.strip().upper() == "NOT YET SUBMITTED":
      continue

    parts = [part.strip() for part in re.split(r"\s{2,}", line) if part.strip()]
    
    if len(parts) == 1 and parts[0].upper() == "NOT YET SUBMITTED":
      continue
    
    # CORRECTION: Une ligne est une nouvelle entrée de joueur si elle a au moins 4 parties
    # OU si elle commence par un horaire de match (format HH:MM)
    is_new_player = len(parts) >= 4 or (len(parts) > 0 and re.match(r"\d{2}:\d{2}", parts[0]))
    
    if is_new_player:
      if len(parts) >= 6:
        game_time, matchup, team, player, status = parts[:5]
        reason = " ".join(parts[5:])
      elif len(parts) == 5:
        game_time, team, player, status = parts[:4]
        matchup = ""
        reason = parts[4]
      else:
        game_time = parts[0] if len(parts) > 0 else ""
        matchup = ""
        team = parts[1] if len(parts) > 1 else ""
        player = parts[2] if len(parts) > 2 else ""
        status = parts[3] if len(parts) > 3 else ""
        reason = ""
      
      rows.append({
          "gameTime": game_time or "TBD",
          "matchup": matchup,
          "team": team,
          "player": player,
          "status": status,
          "reason": reason,
          "page": page_num,
          "rowIndex": line_index
      })
      
    elif len(rows) > 0 and parts:
      # CORRECTION CRITIQUE: C'est une continuation, l'ajouter à la DERNIÈRE ligne
      continuation_text = " ".join(parts)
      
      if continuation_text.upper() != "NOT YET SUBMITTED":
        # Ajouter à la reason de la dernière ligne (le joueur précédent)
        rows[-1]["reason"] = f"{rows[-1]['reason']} {continuation_text}".strip()
  
  return rows


def normalize_rows(rows: list[Row], game_date: str) -> list[Row]:
  """Version simplifiée - les continuations sont déjà gérées dans parse_rows_per_page"""
  normalized = []
  last_team = None
  last_game_time = None
  last_real_game_time = None
  last_matchup = None
  game_time_by_matchup: dict[str, str] = {}

  status_pattern = re.compile(r"\b(" + "|".join([re.escape(token) for token in STATUS_TOKENS]) + r")\b", re.I)
  reason_pattern = re.compile(
      r"(Injury/Illness|Injury|Illness|G League|GLeague|Personal|Rest|Suspension|Not With Team|Injury Recovery)",
      re.I
  )
  reason_prefixes = [
      "G League",
      "Injury/Illness",
      "NOT YET SUBMITTED",
      "Not With Team",
      "Return to Competition Reconditioning"
  ]
  carryover_reason_by_page = {}
  carryover_team_by_page = {}
  pending_reason_by_page = {}
  pending_team_by_page = {}

  def starts_with_reason_prefix(text):
    if not text:
      return False
    for prefix in reason_prefixes:
      if text.startswith(prefix):
        return True
    return False

  def split_reason_on_prefixes(text):
    if not text:
      return text, ""
    text = text.replace("\u00a0", " ").strip()
    positions = []
    for prefix in reason_prefixes:
      for match in re.finditer(re.escape(prefix), text, flags=re.I):
        positions.append(match.start())
    positions = sorted(set(positions))
    if not positions:
      return text, ""
    if text[:positions[0]].strip() == "":
      positions[0] = 0
    if positions[0] > 0:
      split_at = positions[0]
      return text[:split_at].strip(), text[split_at:].strip()
    if len(positions) < 2:
      return text, ""
    split_at = positions[1]
    return text[:split_at].strip(), text[split_at:].strip()

  reason_keyword_regex = re.compile(
      r"(management|contusion|sprain|strain|soreness|surgery|tear|recovery|reconditioning|tendinitis|"
      r"fracture|tendinopathy|illness|injury|irritation|tightness|bruise|spasms|stress|thrombosis|"
      r"mcl|acl|pcl|lcl|ankle|knee|foot|back|shoulder|hamstring|groin|achilles|toe|hand|wrist|"
      r"finger|elbow|quad|calf|rib|hip)",
      re.I
  )
  player_status_blob_regex = re.compile(
      r"\b[A-Z][A-Za-z'`.\-]+,\s*[A-Z][A-Za-z'`.\-]+\s+"
      r"(Out|Questionable|Doubtful|Probable|Available|Not\s*With\s*Team|NotWithTeam)\b",
      re.I
  )
  page_marker_regex = re.compile(r"\bPage\s*\d+\s*of\s*\d+\b|\bPage\d+of\d+\b", re.I)
  matchup_blob_regex = re.compile(r"\b[A-Z]{2,4}\s*@\s*[A-Z]{2,4}\b")
  date_or_time_blob_regex = re.compile(r"\b\d{2}/\d{2}/\d{4}\b|\b\d{1,2}:\d{2}\s*\(ET\)\b", re.I)
  status_blob_regex = re.compile(
      r"\b(Out|Questionable|Doubtful|Probable|Available|Not\s*With\s*Team|NotWithTeam)\b",
      re.I
  )

  def normalize_spaces(text):
    return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()

  def looks_like_player_blob(text):
    compact = normalize_spaces(text)
    if not compact:
      return False
    if player_status_blob_regex.search(compact):
      return True
    if page_marker_regex.search(compact):
      return True
    if matchup_blob_regex.search(compact):
      return True
    if date_or_time_blob_regex.search(compact):
      return True
    status_hits = len(status_blob_regex.findall(compact))
    comma_name_hits = len(re.findall(r"[A-Z][A-Za-z'`.\-]+,\s*[A-Z][A-Za-z'`.\-]+", compact))
    if status_hits >= 2 and comma_name_hits >= 1:
      return True
    return False

  def trim_reason_noise(text):
    compact = normalize_spaces(text)
    if not compact:
      return ""
    if player_status_blob_regex.match(compact):
      return ""
    cut_positions = []
    for regex in (page_marker_regex, date_or_time_blob_regex, matchup_blob_regex, player_status_blob_regex):
      match = regex.search(compact)
      if match and match.start() > 0:
        cut_positions.append(match.start())
    if cut_positions:
      compact = compact[:min(cut_positions)].strip()
    compact = page_marker_regex.sub("", compact).strip(" ;,-")
    if compact.upper().replace(" ", "") == "NOTYETSUBMITTED":
      return "NOT YET SUBMITTED"
    return compact

  def looks_like_reason_continuation(text):
    if not text:
      return False
    if starts_with_reason_prefix(text):
      return True
    return bool(reason_keyword_regex.search(text))

  def should_append_reason(prev_reason, continuation):
    if not prev_reason or not continuation:
      return False
    if continuation[:1].islower():
      return True
    if prev_reason.endswith(";") or prev_reason.endswith("-"):
      return True
    if re.search(
        r"(Injury|Illness|Recovery|Reconditioning|Management|Surgery|Sprain|Strain|Tear|Contusion|Fracture|Tendinopathy|Bruise|Soreness|Tightness)$",
        prev_reason,
        re.I
    ):
      return True
    return False

  def is_header_row(team_value, player_value):
    team_text = (team_value or "").strip().lower()
    player_text = (player_value or "").strip()
    if team_text.startswith("injury report"):
      return True
    if re.match(r"\\d{2}/\\d{2}/\\d{2}\\s+\\d{2}:\\d{2}\\s*[AP]M", player_text):
      return True
    return False

  for row in rows:
    player = row.get("player", "") or ""
    status = row.get("status", "") or ""
    raw_reason_original = row.get("reason", "") or ""
    reason = raw_reason_original
    raw_team = (row.get("team") or "").strip()
    if reason:
      reason = trim_reason_noise(reason)

    if is_header_row(raw_team, player):
      continue

    # Gérer "NOT YET SUBMITTED"
    nys_marker = "NOT YET SUBMITTED"
    if player.strip().upper() == nys_marker or status.strip().upper() == nys_marker:
      team_value = row.get("team", "").strip() or last_team or ""
      nys_matchup = (row.get("matchup", "") or "").strip() or last_matchup or ""
      matchup_key = nys_matchup.upper()
      resolved_time = (row.get("gameTime", "") or "").strip()
      if not resolved_time or resolved_time.upper() == "TBD":
        resolved_time = game_time_by_matchup.get(matchup_key, "") if matchup_key else ""
      if not resolved_time or resolved_time.upper() == "TBD":
        resolved_time = last_real_game_time or last_game_time or "TBD"
      if resolved_time and resolved_time.upper() != "TBD":
        if matchup_key:
          game_time_by_matchup[matchup_key] = resolved_time
        last_real_game_time = resolved_time
      if team_value and team_value.upper() != nys_marker:
        normalized.append({
            "gameTime": resolved_time,
            "matchup": nys_matchup,
            "team": team_value,
            "player": nys_marker,
            "status": nys_marker,
            "reason": nys_marker,
            "page": row.get("page", 0),
            "rowIndex": row.get("rowIndex", 0),
            "gameDate": game_date
        })
      continue

    # Extraire le statut du nom du joueur si nécessaire
    if not status and player:
      match = status_pattern.search(player)
      if match:
        status = match.group(1)
        player = player.replace(match.group(0), "").strip(" -")

    # Extraire la raison du nom du joueur si nécessaire
    if not reason and player:
      reason_match = reason_pattern.search(player)
      if reason_match:
        reason = player[reason_match.start():].strip(" -")
        player = player[:reason_match.start()].strip(" -")
      else:
        for prefix in reason_prefixes:
          if player.startswith(prefix):
            reason = player
            player = ""
            break

    if not player and not status and not reason:
      possible_reason = trim_reason_noise(" ".join([row.get("team", ""), row.get("matchup", "")]).strip())
      if (
          possible_reason
          and len(possible_reason) <= 120
          and looks_like_reason_continuation(possible_reason)
          and not looks_like_player_blob(possible_reason)
      ):
        prev = normalized[-1] if normalized else None
        page_key = row.get("page", 0)
        if prev and prev.get("page") == page_key:
          prev_reason = prev.get("reason", "")
          if should_append_reason(prev_reason, possible_reason) or looks_like_reason_continuation(possible_reason):
            prev["reason"] = f"{prev_reason} {possible_reason}".strip()
            continue
        pending_reason_by_page[page_key] = possible_reason
        pending_team_by_page[page_key] = raw_team
      continue

    # Gestion du contexte (sans réinitialisation par page)
    if not row.get("team") or row.get("team").strip() == "":
      row["team"] = last_team or ""
    else:
      team_value = row.get("team").strip()
      if team_value and team_value.upper() != nys_marker:
        last_team = team_value

    if not row.get("matchup") or row.get("matchup").strip() == "":
      row["matchup"] = last_matchup or ""
    else:
      matchup_value = row.get("matchup").strip()
      if matchup_value and matchup_value.upper() != nys_marker:
        last_matchup = matchup_value
    matchup_key = (row.get("matchup") or "").strip().upper()

    time_value = (row.get("gameTime") or "").strip()
    if time_value and time_value.upper() not in {"TBD", nys_marker}:
      row["gameTime"] = time_value
      last_game_time = time_value
      last_real_game_time = time_value
      if matchup_key:
        game_time_by_matchup[matchup_key] = time_value
    else:
      inferred_time = ""
      if matchup_key:
        inferred_time = game_time_by_matchup.get(matchup_key, "")
      if not inferred_time:
        inferred_time = last_real_game_time or ""
      if not inferred_time:
        inferred_time = last_game_time or ""
      row["gameTime"] = inferred_time or "TBD"

    current_team = row.get("team") or last_team or ""
    page_key = row.get("page", 0)
    explicit_team = raw_team

    if not player and not status and reason:
      raw_reason = normalize_spaces(raw_reason_original)
      # If the raw continuation already contains player/status blobs, it is
      # almost certainly OCR spillover from next rows, not a valid reason.
      if looks_like_player_blob(raw_reason):
        continue
      reason = trim_reason_noise(reason)
      if not reason or looks_like_player_blob(reason):
        continue
      prev = normalized[-1] if normalized else None
      if prev and prev.get("page") == page_key and (not current_team or prev.get("team") == current_team):
        prev_reason = prev.get("reason", "")
        if should_append_reason(prev_reason, reason):
          prev["reason"] = f"{prev_reason} {reason}".strip()
          continue
      if looks_like_reason_continuation(reason):
        pending_reason_by_page[page_key] = reason
        pending_team_by_page[page_key] = current_team
      continue

    if page_key in pending_reason_by_page:
      pending_reason = pending_reason_by_page.get(page_key)
      pending_team = pending_team_by_page.get(page_key)
      if pending_reason and not looks_like_player_blob(pending_reason) and (not pending_team or pending_team == current_team):
        if (
            not reason
            or not starts_with_reason_prefix(reason)
            or looks_like_reason_continuation(reason)
        ):
          reason = trim_reason_noise(f"{pending_reason} {reason}".strip())
        pending_reason_by_page.pop(page_key, None)
        pending_team_by_page.pop(page_key, None)

    if page_key in carryover_reason_by_page:
      carry_reason = carryover_reason_by_page.get(page_key)
      if carry_reason and not looks_like_player_blob(carry_reason) and (player or status):
        if (
            not reason
            or not starts_with_reason_prefix(reason)
            or looks_like_reason_continuation(reason)
        ):
          reason = trim_reason_noise(f"{carry_reason} {reason}".strip())
          carryover_reason_by_page.pop(page_key, None)
          carryover_team_by_page.pop(page_key, None)

    if reason:
      reason = trim_reason_noise(reason)
      reason, spill = split_reason_on_prefixes(reason)
      reason = trim_reason_noise(reason)
      spill = trim_reason_noise(spill)
      if spill and not looks_like_player_blob(spill):
        carryover_reason_by_page[page_key] = spill
        carryover_team_by_page[page_key] = explicit_team or None

    # Ignorer les lignes complètement vides
    if not player and not status and not reason:
      continue

    row["player"] = player
    row["status"] = status
    row["reason"] = reason
    row["gameDate"] = game_date
    normalized.append(row)

  return normalized


def deduplicate_rows(rows: list[Row]) -> list[Row]:
  """Version améliorée - fusion intelligente des doublons"""
  if not rows:
    return []
  noisy_reason_regex = re.compile(
      r"[A-Z][A-Za-z'`.\-]+,\s*[A-Z][A-Za-z'`.\-]+\s+"
      r"(Out|Questionable|Doubtful|Probable|Available|Not\s*With\s*Team|NotWithTeam)|"
      r"\bPage\s*\d+\s*of\s*\d+\b|\bPage\d+of\d+\b|\b\d{2}/\d{2}/\d{4}\b|\b[A-Z]{2,4}\s*@\s*[A-Z]{2,4}\b",
      re.I
  )
  
  unique = {}
  for row in rows:
    key = (
        row.get("team", "").strip().upper(),
        row.get("player", "").strip().upper(),
        row.get("gameTime", "").strip()
    )
    
    # Ignorer les clés vides ou "NOT YET SUBMITTED"
    if not key[1] or key[1] == "NOT YET SUBMITTED":
      # Garder quand même ces lignes mais avec une clé unique
      unique[id(row)] = row
      continue
    
    if key in unique:
      existing = unique[key]
      
      # Fusionner les raisons intelligemment
      existing_reason = existing.get("reason", "").strip()
      new_reason = row.get("reason", "").strip()
      
      if new_reason and noisy_reason_regex.search(new_reason):
        continue
      if new_reason and new_reason not in existing_reason:
        # Vérifier si c'est vraiment différent ou juste une continuation
        combined = f"{existing_reason} {new_reason}".strip()
        existing["reason"] = combined
      
      # Mettre à jour les champs vides
      for field in ["status", "matchup", "gameTime", "team"]:
        if not existing.get(field) and row.get(field):
          existing[field] = row.get(field)
    else:
      unique[key] = row
  
  result = list(unique.values())
  result.sort(key=lambda x: (x.get("page", 0), x.get("rowIndex", 0)))
  return result


def normalize_header(value: Any) -> str:
  if not value:
    return ""
  return re.sub(r"[^a-z]", "", str(value).lower())


def group_words_by_line(words: list[dict[str, Any]], y_tol: int = 3) -> list[dict[str, Any]]:
  lines = []
  for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
    if not lines or abs(word["top"] - lines[-1]["top"]) > y_tol:
      lines.append({"top": word["top"], "words": [word]})
    else:
      lines[-1]["words"].append(word)
  return lines

def extract_rows_by_columns(pdf: Any) -> list[Row]:
  """Version améliorée avec gestion des continuations"""
  rows = []
  column_boundaries = None
  
  for page_num, page in enumerate(pdf.pages, start=1):
    words = page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False) or []
    if not words:
      continue
    
    lines = group_words_by_line(words)
    header_line = None
    
    for line_index, line in enumerate(lines):
      line_text = " ".join(word["text"] for word in line["words"]).upper()
      if "GAME" in line_text and "MATCHUP" in line_text and "TEAM" in line_text and "PLAYER" in line_text:
        header_line = line
        break
    
    if header_line:
      header_words = {word["text"].upper(): word["x0"] for word in header_line["words"]}
      columns = []
      for key in ["GAME", "MATCHUP", "TEAM", "PLAYER", "STATUS", "REASON"]:
        if key in header_words:
          columns.append((key, header_words[key]))
      columns.sort(key=lambda item: item[1])
      
      if len(columns) >= 4:
        column_boundaries = []
        for index, (label, x0) in enumerate(columns):
          x1 = columns[index + 1][1] if index + 1 < len(columns) else float("inf")
          column_boundaries.append((label, x0, x1))
    
    if not column_boundaries:
      continue

    for line_index, line in enumerate(lines):
      line_text = " ".join(word["text"] for word in line["words"]).upper()
      if "NBA INJURY REPORT" in line_text or "REPORT UPDATED" in line_text or line_text.startswith("PAGE "):
        continue
      if header_line and line is header_line:
        continue

      row_data = {label: "" for label, _, _ in column_boundaries}
      for word in line["words"]:
        for label, x0, x1 in column_boundaries:
          if x0 <= word["x0"] < x1:
            row_data[label] = f"{row_data[label]} {word['text']}".strip()
            break

      # CORRECTION: Vérifier si c'est une ligne de continuation
      # Une continuation n'a pas de PLAYER, TEAM, ou STATUS
      has_player = bool(row_data.get("PLAYER", "").strip())
      has_team = bool(row_data.get("TEAM", "").strip())
      has_status = bool(row_data.get("STATUS", "").strip())
      has_reason = bool(row_data.get("REASON", "").strip())
      
      # Si seulement la raison est présente, c'est une continuation
      if has_reason and not has_player and not has_team and not has_status:
        if len(rows) > 0:
          # Ajouter à la dernière ligne
          rows[-1]["reason"] = f"{rows[-1]['reason']} {row_data['REASON']}".strip()
        continue
      
      # Si la ligne est complètement vide, l'ignorer
      if not any([has_team, has_player, has_status, has_reason]):
        continue

      rows.append({
          "gameTime": row_data.get("GAME") or "TBD",
          "matchup": row_data.get("MATCHUP", ""),
          "team": row_data.get("TEAM", ""),
          "player": row_data.get("PLAYER", ""),
          "status": row_data.get("STATUS", ""),
          "reason": row_data.get("REASON", ""),
          "page": page_num,
          "rowIndex": line_index
      })
  
  return rows


def extract_rows_from_tables_per_page(page: Any, page_num: int = 1) -> list[Row]:
  """Version améliorée avec gestion des continuations"""
  rows = []
  table_settings = {
      "vertical_strategy": "text",
      "horizontal_strategy": "text",
      "intersection_tolerance": 3,
      "snap_tolerance": 3,
      "join_tolerance": 3,
      "min_words_vertical": 1,
      "min_words_horizontal": 1
  }
  tables = page.extract_tables(table_settings) or []
  row_index = 0
  
  for table in tables:
    if not table or len(table) < 2:
      continue
    header = table[0]
    if not header:
      continue
    
    header_map = {}
    for idx, value in enumerate(header):
      if value:
        header_map[normalize_header(value)] = idx

    def find_index(keys):
      for key in keys:
        for header_key, idx in header_map.items():
          if key in header_key:
            return idx
      return None

    game_idx = find_index(["gamedate", "gametime", "game", "date", "time"])
    matchup_idx = find_index(["matchup", "match"])
    team_idx = find_index(["team"])
    player_idx = find_index(["player", "name"])
    status_idx = find_index(["status"])
    reason_idx = find_index(["reason", "injury", "comment"])

    for row in table[1:]:
      if not row:
        continue
      
      def safe_get(index):
        if index is None or index >= len(row):
          return ""
        return (row[index] or "").strip()

      game_time = safe_get(game_idx)
      matchup = safe_get(matchup_idx)
      team = safe_get(team_idx)
      player = safe_get(player_idx)
      status = safe_get(status_idx)
      reason = safe_get(reason_idx)
      
      # CORRECTION: Détecter les continuations dans les tables aussi
      # Une continuation n'a pas de player, team ou status
      is_continuation = (
          not player and 
          not team and 
          not status and 
          reason
      )
      
      if is_continuation and len(rows) > 0:
        # Ajouter à la dernière ligne
        rows[-1]["reason"] = f"{rows[-1]['reason']} {reason}".strip()
        continue
      
      if not any([team, player, status, reason]):
        continue
      if player.upper() in ["PLAYER", "NAME"]:
        continue
      
      rows.append({
          "gameTime": game_time or "TBD",
          "matchup": matchup,
          "team": team,
          "player": player,
          "status": status,
          "reason": reason,
          "page": page_num,
          "rowIndex": row_index
      })
      row_index += 1
  
  return rows



def build_stats(rows: list[Row]) -> dict[str, Any]:
  by_status = {}
  by_team = {}
  for row in rows:
    status = row.get("status") or "Unknown"
    team = row.get("team") or "Unknown"
    by_status[status] = by_status.get(status, 0) + 1
    by_team[team] = by_team.get(team, 0) + 1
  return {
      "totalRows": len(rows),
      "byStatus": by_status,
      "byTeam": by_team
  }


def sort_rows_for_display(rows: list[Row]) -> list[Row]:
  if not rows:
    return []
  status_rank = {status: idx for idx, status in enumerate(STATUS_DISPLAY_ORDER)}
  team_block_order: dict[tuple[str, str], int] = {}
  for idx, row in enumerate(rows):
    team_key = (
        (row.get("matchup") or "").strip().upper(),
        (row.get("team") or "").strip().upper(),
    )
    team_block_order.setdefault(team_key, idx)
  return sorted(
      rows,
      key=lambda row: (
          team_block_order.get(
              (
                  (row.get("matchup") or "").strip().upper(),
                  (row.get("team") or "").strip().upper(),
              ),
              10**9,
          ),
          status_rank.get((row.get("status") or "").strip(), len(STATUS_DISPLAY_ORDER)),
          (row.get("player") or "").strip().upper(),
      ),
  )


def filter_rows(
    rows: list[Row],
    player_query: str | None,
    selected_teams: list[str] | None,
    selected_statuses: list[str] | None,
) -> list[Row]:
  if not rows:
    return []
  query = (player_query or "").strip().lower()
  team_set = {team.strip() for team in (selected_teams or []) if team and team.strip()}
  status_set = {status.strip() for status in (selected_statuses or []) if status and status.strip()}
  filtered: list[Row] = []
  for row in rows:
    player = (row.get("player") or "").strip()
    team = (row.get("team") or "").strip()
    status = (row.get("status") or "").strip()
    if query and query not in player.lower():
      continue
    if team_set and team not in team_set:
      continue
    if status_set and status not in status_set:
      continue
    filtered.append(row)
  return filtered


def status_filter_color(status: str) -> str:
  normalized = (status or "").strip().lower()
  if normalized == "available":
    return "#2f7a1f"
  if normalized == "probable":
    return "#275ea6"
  if normalized == "questionable":
    return "#a57500"
  if normalized == "doubtful":
    return "#8e4c25"
  if normalized == "not with team":
    return "#583689"
  if normalized == "out":
    return "#ad3416"
  return "#857866"


def rows_to_dataframe(rows: list[Row]) -> pd.DataFrame:
  df = pd.DataFrame(rows)
  if df.empty:
    return df
  df["status"] = df["status"].fillna("Unknown")
  df["team"] = df["team"].fillna("Unknown")
  df["player"] = df["player"].fillna("")
  df["reason"] = df["reason"].fillna("")
  df["gameTime"] = df["gameTime"].fillna("TBD")
  df["matchup"] = df["matchup"].fillna("")
  if "page" in df.columns:
    df["page"] = df["page"].fillna(0).astype(int)
  if "rowIndex" in df.columns:
    df = df.drop(columns=["rowIndex"])
  if "gameDate" in df.columns:
    df["gameDate"] = df["gameDate"].fillna("")
  return df





def fetch_injury_report() -> Payload:
  CACHE_LOGGER.info("Fetch injury report using bs4 link extraction.")
  session = requests.Session()
  latest = fetch_latest_pdf_link(session=session)
  if not latest:
    CACHE_LOGGER.warning("No PDF links found.")
    return {
        "ok": False,
        "error": "No PDF found on official page.",
        "step": "parse_links"
    }
  CACHE_LOGGER.info("Latest PDF selected: %s", latest)
  try:
    pdf_response = fetch_pdf_with_retry(latest, session=session, timeout=20, retries=3)
  except Exception as exc:  # noqa: BLE001
    fallback_url = fallback_pdf_url(latest)
    if not fallback_url or fallback_url == latest:
      raise exc
    CACHE_LOGGER.warning("PDF fetch failed, trying fallback URL: %s", fallback_url)
    pdf_response = fetch_pdf_with_retry(fallback_url, session=session, timeout=20, retries=3)
    latest = fallback_url
  os.makedirs(DATA_DIR, exist_ok=True)
  pdf_name = latest.split("/")[-1]
  pdf_path = os.path.join(DATA_DIR, pdf_name)
  with open(pdf_path, "wb") as pdf_file:
    pdf_file.write(pdf_response.content)
  CACHE_LOGGER.info("PDF saved to %s", pdf_path)
  text_rows = []
  table_rows = []
  column_rows = []
  with pdfplumber.open(io.BytesIO(pdf_response.content)) as pdf:
    CACHE_LOGGER.info("PDF pages detected: %s", len(pdf.pages))
    for page_num, page in enumerate(pdf.pages, start=1):
      page_text = page.extract_text() or ""
      if page_text:
        text_rows.extend(parse_rows_per_page(page_text, page_num))
      table_rows.extend(extract_rows_from_tables_per_page(page, page_num))
    column_rows = extract_rows_by_columns(pdf)

  combined_rows = text_rows + table_rows + column_rows
  CACHE_LOGGER.info(
      "Row counts text=%s table=%s column=%s combined=%s",
      len(text_rows),
      len(table_rows),
      len(column_rows),
      len(combined_rows)
  )
  game_date = parse_pdf_date(latest)
  if not game_date:
    timestamp = parse_pdf_datetime(latest)
    if timestamp:
      game_date = datetime.utcfromtimestamp(timestamp).strftime("%m/%d/%Y")
  rows = normalize_rows(deduplicate_rows(combined_rows), game_date)
  if not rows:
    CACHE_LOGGER.warning("PDF parsed but no rows extracted.")
    return {
        "ok": False,
        "error": "PDF loaded but no usable rows.",
        "step": "parse_pdf"
    }
  df = rows_to_dataframe(rows)
  if not df.empty:
    nys_mask = df["reason"].str.contains("NOT YET SUBMITTED", case=False, na=False)
    empty_player_status = (df["player"] == "") & (df["status"] == "")
    df = df[~(empty_player_status & ~nys_mask)]
  csv_columns = ["gameDate", "team", "player", "status", "reason", "page"]
  csv_df = df
  if all(column in df.columns for column in csv_columns):
    csv_df = df[csv_columns]
  csv_name = pdf_name.replace(".pdf", ".csv")
  csv_path = os.path.join(DATA_DIR, csv_name)
  csv_df.to_csv(csv_path, index=False)
  CACHE_LOGGER.info("CSV saved to %s", csv_path)
  published_timestamp = parse_pdf_datetime(latest)
  published_at = ""
  if published_timestamp:
    published_at = datetime.utcfromtimestamp(published_timestamp).isoformat() + "Z"
  return {
      "ok": True,
      "meta": {
          "pdfUrl": latest,
          "pdfName": latest.split("/")[-1],
          "publishedAt": published_at,
          "reportTime": parse_pdf_time_label(latest),
          "pdfPath": pdf_path,
          "csvPath": csv_path
      },
      "stats": build_stats(rows),
      "rows": rows
  }


def get_cached_report(force: bool = False) -> Payload:
  now = time.time()
  cached_payload: Payload | None = None
  cache_fresh = False
  with CACHE_LOCK:
    if not force and CACHE_STATE["data"] and (now - CACHE_STATE["last_updated"] < CACHE_TTL_SECONDS):
      cached_payload = CACHE_STATE["data"]
      cache_fresh = True
  if cached_payload and cache_fresh:
    cached_url = cached_payload.get("meta", {}).get("pdfUrl", "")
    cached_ts = parse_pdf_datetime(cached_url)
    try:
      latest_url = fetch_latest_pdf_link()
      latest_ts = parse_pdf_datetime(latest_url or "")
      if latest_url and latest_ts > cached_ts:
        CACHE_LOGGER.info("Newer PDF detected (%s). Refreshing cache.", latest_url)
      else:
        CACHE_LOGGER.info("Serving cached report.")
        return cached_payload
    except Exception as exc:  # noqa: BLE001
      CACHE_LOGGER.warning("Could not verify newer PDF, serving cached report: %s", exc)
      return cached_payload
  if force:
    CACHE_LOGGER.info("Force refresh requested.")
  else:
    CACHE_LOGGER.info("Cache expired or empty. Refreshing.")
  payload = fetch_injury_report()
  if payload.get("ok"):
    with CACHE_LOCK:
      CACHE_STATE["data"] = payload
      CACHE_STATE["last_updated"] = now
      CACHE_LOGGER.info("Cache updated.")
  return payload


def start_scheduler() -> None:
  def loop():
    while True:
      try:
        CACHE_LOGGER.info("Scheduled refresh triggered.")
        get_cached_report(force=True)
      except Exception:
        CACHE_LOGGER.exception("Scheduled refresh failed.")
        pass
      time.sleep(CACHE_TTL_SECONDS)

  if os.environ.get("ENABLE_SCHEDULER", "1") != "1":
    CACHE_LOGGER.info("Scheduler disabled via ENABLE_SCHEDULER.")
    return
  if os.environ.get("WERKZEUG_RUN_MAIN") not in ("true", "1") and os.environ.get("DASH_DEBUG") == "1":
    CACHE_LOGGER.info("Scheduler skipped in Dash debug reloader.")
    return
  CACHE_LOGGER.info("Scheduler started. Interval=%ss", CACHE_TTL_SECONDS)
  thread = threading.Thread(target=loop, daemon=True)
  thread.start()


def render_status_cards(stats: dict[str, Any]) -> Any:
  by_status = stats.get("byStatus", {})
  entries = list(by_status.items())
  entries.sort(
      key=lambda x: STATUS_DISPLAY_ORDER.index(x[0]) if x[0] in STATUS_DISPLAY_ORDER else len(STATUS_DISPLAY_ORDER)
  )
  if not entries:
    return html.Div("No data available.", className="muted")
  return [
      html.Div(
          [
              html.Span(status, className="status-card-label"),
              html.Strong(count, className="status-card-count"),
          ],
          className=f"status-card status-card-{status.lower().replace(' ', '-')}",
      )
      for status, count in entries
  ]


def team_logo_src(team_name: str) -> str | None:
  code = TEAM_LOGO_CODE_BY_NAME.get((team_name or "").strip())
  if not code:
    return None
  return f"/assets/team_logos/{code}.png"


def player_headshot_src(player_name: str) -> str | None:
  key = normalize_player_name_key(player_name)
  filename = PLAYER_HEADSHOT_FILE_BY_NAME_KEY.get(key)
  if not filename:
    return None
  if filename not in AVAILABLE_PLAYER_HEADSHOT_FILES:
    return None
  return f"/assets/player_headshots/{filename}"


def render_table_rows(rows: list[Row], loading: bool = False) -> Any:
  if loading:
    return html.Div("Loading official PDF...", className="table-row muted")
  if not rows:
    return html.Div("No rows extracted yet.", className="table-row muted")
  row_divs = []
  for index, row in enumerate(rows):
    team_name = row.get("team", "")
    logo_src = team_logo_src(team_name)
    team_children = []
    if logo_src:
      team_children.append(
          html.Img(
              src=logo_src,
              className="team-logo",
              alt=f"{team_name} logo",
          )
      )
    team_children.append(html.Span(team_name, className="team-name"))

    player_name = row.get("player", "")
    player_src = player_headshot_src(player_name)
    player_children = []
    if player_src:
      player_children.append(
          html.Img(
              src=player_src,
              className="player-headshot",
              alt=f"{player_name} headshot",
          )
      )
    player_children.append(html.Span(player_name, className="player-name"))

    status = row.get("status") or "Unknown"
    status_class = f"status-pill status-{status.lower().replace(' ', '-')}"
    row_divs.append(
        html.Div(
            [
                html.Span(
                    row.get("gameTime", ""),
                    className="cell-game-time",
                    title=row.get("gameTime", ""),
                ),
                html.Span(
                    row.get("matchup", ""),
                    className="cell-matchup",
                    title=row.get("matchup", ""),
                ),
                html.Span(team_children, className="team-cell", title=team_name),
                html.Span(player_children, className="player-cell", title=player_name),
                html.Span(status, className=status_class),
                html.Span(
                    row.get("reason", ""),
                    className="cell-reason",
                    title=row.get("reason", ""),
                ),
            ],
            className="table-row",
            key=f"{row.get('player', '')}-{index}",
        )
    )
  return row_divs


app = Dash(__name__)
server = app.server

app.layout = html.Div(
    [
        html.Header(
            [
                html.Div(
                    [
                        html.P("TTFL live data", className="eyebrow"),
                        html.H1("NBA Injury Report"),
                        html.P(
                            "Official NBA data, updated automatically for the TTFL dashboard.",
                            className="subtitle",
                        ),
                        html.Div(
                            [
                                html.Button("Refresh report", className="primary", id="refresh-btn"),
                                html.Div(
                                    [
                                        html.Span(id="pdf-name", children="Official PDF"),
                                        html.Span(id="report-time"),
                                        html.Span(id="local-updated"),
                                    ],
                                    className="meta",
                                ),
                            ],
                            className="hero-actions",
                        ),
                    ]
                ),
                html.Div(
                    [
                        html.Div(
                            [html.P("Players tracked", className="hero-label"), html.H2(id="total-players")],
                        ),
                        html.Div(
                            [html.P("Teams impacted", className="hero-label"), html.H2(id="total-teams")],
                        ),
                    ],
                    className="hero-card",
                ),
            ],
            className="hero",
        ),
        html.Div(id="error-banner"),
        html.Section(
            [
                html.Div(
                    [
                        html.H3("Key statuses"),
                        html.Div(id="status-grid", className="status-grid"),
                    ],
                    className="panel",
                ),
                html.Div(
                    [
                        html.H3("Official source"),
                        html.P(
                            "Report extracted automatically from the NBA official page. No dummy data.",
                            className="muted",
                        ),
                        html.Div(
                            [
                                html.Div(
                                    [
                                        html.Span("PDF link", className="label"),
                                        html.A("Open PDF", href="#", id="pdf-link", target="_blank"),
                                    ]
                                ),
                            ],
                            className="source-meta",
                        ),
                    ],
                    className="panel",
                ),
            ],
            className="grid",
        ),
        html.Section(
            [
                html.Div(
                    [
                        html.H3("Filters"),
                        html.Div(
                            [
                                dcc.Input(
                                    id="player-search",
                                    type="text",
                                    placeholder="Search player...",
                                    className="filter-input",
                                ),
                                dcc.Dropdown(
                                    id="team-filter",
                                    options=[],
                                    multi=True,
                                    placeholder="Filter by team",
                                    className="filter-dropdown",
                                ),
                                dcc.Dropdown(
                                    id="availability-filter",
                                    options=[],
                                    multi=True,
                                    placeholder="Filter by availability",
                                    className="filter-dropdown",
                                ),
                            ],
                            className="filter-grid",
                        ),
                    ],
                    className="panel",
                ),
            ]
        ),
        html.Section(
            [
                html.Div(
                    [
                        html.H3("Injury report live"),
                        html.Span(id="row-count", className="muted"),
                    ],
                    className="table-header",
                ),
                html.Div(
                    [
                        html.Div(
                            [
                                html.Span("Game Time"),
                                html.Span("Matchup"),
                                html.Span("Team"),
                                html.Span("Player"),
                                html.Span("Status"),
                                html.Span("Reason"),
                            ],
                            className="table-row table-head",
                        ),
                        html.Div(id="table-body"),
                    ],
                    className="table",
                ),
            ],
            className="panel table-panel",
        ),
        dcc.Store(id="report-store"),
    ],
    className="app",
)


@app.callback(
    Output("report-store", "data"),
    Output("error-banner", "children"),
    Output("local-updated", "children"),
    Input("refresh-btn", "n_clicks"),
)
def load_report(n_clicks: int | None) -> tuple[Any, Any, str]:
  try:
    force_refresh = n_clicks is not None
    payload = get_cached_report(force=force_refresh)
    if not payload.get("ok"):
      error_msg = payload.get("error", "Unknown error.")
      return None, html.Div(error_msg, className="error"), ""
    updated = datetime.now().strftime("Local update: %H:%M:%S")
    return payload, None, updated
  except Exception as exc:  # noqa: BLE001
    return None, html.Div(str(exc), className="error"), ""


@app.callback(
    Output("team-filter", "options"),
    Output("availability-filter", "options"),
    Input("report-store", "data"),
)
def populate_filter_options(data: Payload | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
  if not data:
    return [], []
  rows = data.get("rows", [])
  teams = sorted({(row.get("team") or "").strip() for row in rows if (row.get("team") or "").strip()})
  statuses = {(row.get("status") or "").strip() for row in rows if (row.get("status") or "").strip()}
  statuses_sorted = sorted(
      statuses,
      key=lambda value: STATUS_DISPLAY_ORDER.index(value) if value in STATUS_DISPLAY_ORDER else len(STATUS_DISPLAY_ORDER),
  )
  team_options = []
  for team in teams:
    logo_src = team_logo_src(team)
    label_children: list[Any] = []
    if logo_src:
      label_children.append(html.Img(src=logo_src, className="dropdown-team-logo", alt=f"{team} logo"))
    label_children.append(html.Span(team, className="dropdown-team-name"))
    team_options.append(
        {
            "label": html.Span(label_children, className="dropdown-team-option"),
            "value": team,
            "search": team,
        }
    )
  status_options = []
  for status in statuses_sorted:
    status_options.append(
        {
            "label": html.Span(
                [
                    html.Span("●", className="dropdown-status-dot", style={"color": status_filter_color(status)}),
                    html.Span(status, className="dropdown-status-name"),
                ],
                className="dropdown-status-option",
            ),
            "value": status,
            "search": status,
        }
    )
  return team_options, status_options


@app.callback(
    Output("pdf-name", "children"),
    Output("report-time", "children"),
    Output("pdf-link", "href"),
    Output("total-players", "children"),
    Output("total-teams", "children"),
    Output("status-grid", "children"),
    Output("table-body", "children"),
    Output("row-count", "children"),
    Input("report-store", "data"),
    Input("player-search", "value"),
    Input("team-filter", "value"),
    Input("availability-filter", "value"),
)
def render_report(
    data: Payload | None,
    player_search: str | None,
    selected_teams: list[str] | None,
    selected_statuses: list[str] | None,
) -> tuple[Any, ...]:
  if not data:
    return (
        "Official PDF",
        "",
        "#",
        "--",
        "--",
        html.Div("No data available.", className="muted"),
        render_table_rows([], loading=True),
        "--",
    )
  raw_rows = data.get("rows", [])
  filtered_rows = filter_rows(raw_rows, player_search, selected_teams, selected_statuses)
  rows = sort_rows_for_display(filtered_rows)
  stats = build_stats(rows)
  total_players = str(len(rows))
  total_teams = str(len(stats.get("byTeam", {})))
  row_count_label = f"{len(rows)} rows"
  if len(rows) != len(raw_rows):
    row_count_label = f"{len(rows)} rows (filtered from {len(raw_rows)})"
  return (
      data.get("meta", {}).get("pdfName", "Official PDF"),
      f"Report time: {data.get('meta', {}).get('reportTime', '--')}" if data.get("meta", {}).get("reportTime") else "Report time: --",
      data.get("meta", {}).get("pdfUrl", "#"),
      total_players,
      total_teams,
      render_status_cards(stats),
      render_table_rows(rows),
      row_count_label,
  )


if __name__ == "__main__":
  port = int(os.environ.get("PORT", "8050"))
  if "--port" in os.sys.argv:
    try:
      port_index = os.sys.argv.index("--port") + 1
      port = int(os.sys.argv[port_index])
    except Exception:
      pass
  debug = os.environ.get("DASH_DEBUG") == "1"
  start_scheduler()
  app.run_server(debug=debug, port=port)
