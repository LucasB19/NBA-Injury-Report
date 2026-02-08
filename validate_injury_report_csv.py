#!/usr/bin/env python3
"""Validate extracted NBA injury report CSV files.

Copyright (c) 2026 Lucas Berry.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Sequence

REQUIRED_COLUMNS: tuple[str, ...] = ("gameDate", "team", "player", "status", "reason", "page")
MAX_REASON_LEN = 180

PLAYER_STATUS_BLOB_RE = re.compile(
    r"\b[A-Z][A-Za-z'`.\-]+,\s*[A-Z][A-Za-z'`.\-]+\s+"
    r"(Out|Questionable|Doubtful|Probable|Available|Not\s*With\s*Team|NotWithTeam)\b",
    re.I,
)
PAGE_MARKER_RE = re.compile(r"\bPage\s*\d+\s*of\s*\d+\b|\bPage\d+of\d+\b", re.I)
MATCHUP_BLOB_RE = re.compile(r"\b[A-Z]{2,4}\s*@\s*[A-Z]{2,4}\b")
DATE_OR_TIME_BLOB_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\b|\b\d{1,2}:\d{2}\s*\(ET\)\b", re.I)


@dataclass(slots=True)
class Issue:
  """Validation issue raised for a specific row or file."""

  level: str
  code: str
  message: str
  row_number: int | None = None


@dataclass(slots=True)
class ValidationResult:
  """Aggregated result of CSV validation."""

  csv_path: str
  row_count: int
  issues: list[Issue] = field(default_factory=list)

  @property
  def errors(self) -> list[Issue]:
    return [issue for issue in self.issues if issue.level == "ERROR"]

  @property
  def warnings(self) -> list[Issue]:
    return [issue for issue in self.issues if issue.level == "WARN"]

  @property
  def ok(self) -> bool:
    return not self.errors


def _normalize_spaces(text: str) -> str:
  return re.sub(r"\s+", " ", (text or "").replace("\u00a0", " ")).strip()


def find_latest_csv(data_dir: str) -> str | None:
  csv_files = glob.glob(os.path.join(data_dir, "Injury-Report_*.csv"))
  if not csv_files:
    return None
  return max(csv_files, key=os.path.getmtime)


def _is_contaminated_reason(reason: str) -> bool:
  text = _normalize_spaces(reason)
  if not text:
    return False
  if PLAYER_STATUS_BLOB_RE.search(text):
    return True
  if PAGE_MARKER_RE.search(text):
    return True
  if MATCHUP_BLOB_RE.search(text):
    return True
  if DATE_OR_TIME_BLOB_RE.search(text):
    return True
  return False


def validate_csv(path: str, strict_warnings: bool = False) -> ValidationResult:
  with open(path, newline="", encoding="utf-8") as handle:
    reader = csv.DictReader(handle)
    columns = reader.fieldnames or []
    rows = list(reader)

  result = ValidationResult(csv_path=path, row_count=len(rows))

  missing_columns = [column for column in REQUIRED_COLUMNS if column not in columns]
  if missing_columns:
    result.issues.append(
        Issue(
            level="ERROR",
            code="MISSING_COLUMNS",
            message=f"Missing required columns: {missing_columns}",
        )
    )
    return result

  if not rows:
    result.issues.append(Issue(level="ERROR", code="EMPTY_FILE", message="CSV has no data rows."))
    return result

  seen_keys: set[tuple[str, str, str, str, str]] = set()

  for row_number, row in enumerate(rows, start=2):
    team = (row.get("team") or "").strip()
    player = (row.get("player") or "").strip()
    status = (row.get("status") or "").strip()
    reason = _normalize_spaces(row.get("reason") or "")
    game_date = (row.get("gameDate") or "").strip()
    page = (row.get("page") or "").strip()

    combined = f"{team} {player} {status} {reason}".upper()
    is_nys = "NOT YET SUBMITTED" in combined or "NOTYETSUBMITTED" in combined

    if not is_nys:
      if not team:
        result.issues.append(Issue("ERROR", "EMPTY_TEAM", "Missing team value.", row_number))
      if not player:
        result.issues.append(Issue("ERROR", "EMPTY_PLAYER", "Missing player value.", row_number))
      if not status:
        result.issues.append(Issue("ERROR", "EMPTY_STATUS", "Missing status value.", row_number))
      if not reason:
        result.issues.append(Issue("ERROR", "EMPTY_REASON", "Missing reason value.", row_number))

    if reason and len(reason) > MAX_REASON_LEN:
      result.issues.append(
          Issue(
              "ERROR",
              "REASON_TOO_LONG",
              f"Reason is suspiciously long ({len(reason)} chars).",
              row_number,
          )
      )

    if reason and _is_contaminated_reason(reason):
      result.issues.append(
          Issue(
              "ERROR",
              "REASON_CONTAMINATED",
              "Reason contains player/page/matchup/date artifacts.",
              row_number,
          )
      )

    if reason.count("Injury/Illness") > 1:
      result.issues.append(
          Issue(
              "WARN",
              "MULTI_INJURY_SEGMENT",
              "Reason contains multiple 'Injury/Illness' segments.",
              row_number,
          )
      )

    key = (game_date, team.upper(), player.upper(), status.upper(), page)
    if key in seen_keys:
      result.issues.append(Issue("WARN", "DUPLICATE_PLAYER_ROW", "Potential duplicate player row.", row_number))
    else:
      seen_keys.add(key)

  if strict_warnings and result.warnings:
    for warning in list(result.warnings):
      result.issues.append(
          Issue(
              level="ERROR",
              code=f"STRICT_{warning.code}",
              message=warning.message,
              row_number=warning.row_number,
          )
      )

  return result


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="Validate extracted NBA injury report CSV files.")
  parser.add_argument("path", nargs="?", help="CSV file path. If omitted, uses latest CSV in --data-dir.")
  parser.add_argument("--data-dir", default=os.environ.get("PDF_STORAGE_DIR", "data"))
  parser.add_argument("--strict-warnings", action="store_true", help="Treat warnings as errors.")
  return parser


def main(argv: Sequence[str] | None = None) -> int:
  args = build_parser().parse_args(argv)

  csv_path = args.path
  if not csv_path:
    csv_path = find_latest_csv(args.data_dir)
    if not csv_path:
      print(f"FAIL: no CSV found in {args.data_dir}")
      return 2

  if not os.path.exists(csv_path):
    print(f"FAIL: file not found: {csv_path}")
    return 2

  result = validate_csv(csv_path, strict_warnings=args.strict_warnings)

  if result.ok:
    print(f"PASS: {result.csv_path} rows={result.row_count} warnings={len(result.warnings)}")
    for warning in result.warnings:
      location = f" row={warning.row_number}" if warning.row_number else ""
      print(f"WARN[{warning.code}]{location}: {warning.message}")
    return 0

  print(f"FAIL: {result.csv_path} rows={result.row_count} errors={len(result.errors)} warnings={len(result.warnings)}")
  for issue in result.issues:
    location = f" row={issue.row_number}" if issue.row_number else ""
    print(f"{issue.level}[{issue.code}]{location}: {issue.message}")
  return 1


if __name__ == "__main__":
  raise SystemExit(main())
