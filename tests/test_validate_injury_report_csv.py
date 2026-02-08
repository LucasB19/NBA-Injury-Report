"""Tests for validate_injury_report_csv.

Copyright (c) 2026 Lucas Berry.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import validate_injury_report_csv as validator


class ValidateInjuryReportCsvTests(unittest.TestCase):
  def _write_csv(self, header: list[str], rows: list[list[str]]) -> str:
    temp_dir = tempfile.mkdtemp(prefix="injury_csv_test_")
    path = Path(temp_dir) / "Injury-Report_2026-02-07_06_00AM.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
      writer = csv.writer(handle)
      writer.writerow(header)
      writer.writerows(rows)
    return str(path)

  def test_valid_csv_passes(self) -> None:
    path = self._write_csv(
        ["gameDate", "team", "player", "status", "reason", "page"],
        [["02/07/2026", "Chicago Bulls", "Smith, Jalen", "Questionable", "Injury/Illness - Right Calf; Strain", "5"]],
    )

    result = validator.validate_csv(path)

    self.assertTrue(result.ok)
    self.assertEqual(result.row_count, 1)
    self.assertEqual(result.errors, [])

  def test_missing_required_column_fails(self) -> None:
    path = self._write_csv(
        ["gameDate", "team", "player", "status", "page"],
        [["02/07/2026", "Chicago Bulls", "Smith, Jalen", "Questionable", "5"]],
    )

    result = validator.validate_csv(path)

    self.assertFalse(result.ok)
    self.assertTrue(any(issue.code == "MISSING_COLUMNS" for issue in result.errors))

  def test_contaminated_reason_fails(self) -> None:
    path = self._write_csv(
        ["gameDate", "team", "player", "status", "reason", "page"],
        [["02/07/2026", "Chicago Bulls", "Smith, Jalen", "Questionable", "Injury/Illness - Right Calf; Strain Curry, Seth Out", "5"]],
    )

    result = validator.validate_csv(path)

    self.assertFalse(result.ok)
    self.assertTrue(any(issue.code == "REASON_CONTAMINATED" for issue in result.errors))

  def test_empty_reason_fails(self) -> None:
    path = self._write_csv(
        ["gameDate", "team", "player", "status", "reason", "page"],
        [["02/07/2026", "Chicago Bulls", "Smith, Jalen", "Questionable", "", "5"]],
    )

    result = validator.validate_csv(path)

    self.assertFalse(result.ok)
    self.assertTrue(any(issue.code == "EMPTY_REASON" for issue in result.errors))


if __name__ == "__main__":
  unittest.main()
