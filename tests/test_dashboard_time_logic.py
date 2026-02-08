"""Tests for dashboard time parsing and game-time propagation.

Copyright (c) 2026 Lucas Berry.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import unittest

import injury_report_dashboard as dashboard


class DashboardTimeLogicTests(unittest.TestCase):
  def test_parse_pdf_time_label_with_underscore_format(self) -> None:
    url = "https://example.com/Injury-Report_2026-02-08_03_45AM.pdf"
    self.assertEqual(dashboard.parse_pdf_time_label(url), "03:45 AM ET")
    self.assertGreater(dashboard.parse_pdf_datetime(url), 0)

  def test_normalize_rows_reuses_previous_matchup_time_when_tbd(self) -> None:
    rows = [
        {
            "gameTime": "02:00 (ET)",
            "matchup": "MIA@WAS",
            "team": "Miami Heat",
            "player": "A, Player",
            "status": "Out",
            "reason": "Injury/Illness - Test",
            "page": 1,
            "rowIndex": 1,
        },
        {
            "gameTime": "TBD",
            "matchup": "MIA@WAS",
            "team": "Washington Wizards",
            "player": "B, Player",
            "status": "Questionable",
            "reason": "Injury/Illness - Test",
            "page": 1,
            "rowIndex": 2,
        },
    ]
    normalized = dashboard.normalize_rows(rows, "02/08/2026")
    self.assertEqual(normalized[0]["gameTime"], "02:00 (ET)")
    self.assertEqual(normalized[1]["gameTime"], "02:00 (ET)")


if __name__ == "__main__":
  unittest.main()
