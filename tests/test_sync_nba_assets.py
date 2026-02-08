"""Tests for combined NBA assets sync helpers.

Copyright (c) 2026 Lucas Berry.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import unittest

from scripts.assets.sync_nba_assets import TEAM_LOGO_CODES, team_logo_url


class TestSyncNbaAssets(unittest.TestCase):
  def test_team_logo_codes_cover_30_teams(self) -> None:
    self.assertEqual(len(TEAM_LOGO_CODES), 30)
    self.assertEqual(len(set(TEAM_LOGO_CODES)), 30)

  def test_team_logo_url_template(self) -> None:
    self.assertEqual(
        team_logo_url("mia"),
        "https://a.espncdn.com/i/teamlogos/nba/500/mia.png",
    )


if __name__ == "__main__":
  unittest.main()
