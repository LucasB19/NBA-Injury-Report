"""Tests for player headshot filename and name lookup behavior.

Copyright (c) 2026 Lucas Berry.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from injury_report_dashboard import load_player_name_map, normalize_player_name_key
from scripts.assets.sync_player_headshots import build_name_keys, headshot_filename


class TestPlayerHeadshotLookup(unittest.TestCase):
  def test_normalize_player_name_key_removes_accents(self) -> None:
    self.assertEqual(normalize_player_name_key("Jović, Nikola"), "jovic, nikola")
    self.assertEqual(normalize_player_name_key("Danté Exum"), "dante exum")

  def test_load_player_name_map_supports_string_and_int_values(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
      map_path = Path(tmp_dir) / "player_name_map.json"
      payload = {
          "Jović, Nikola": "nikola-jovic-1631107.png",
          "Exum, Danté": 203957,
      }
      map_path.write_text(json.dumps(payload), encoding="utf-8")
      loaded = load_player_name_map(str(map_path))
      self.assertEqual(loaded.get("jovic, nikola"), "nikola-jovic-1631107.png")
      self.assertEqual(loaded.get("exum, dante"), "203957.png")

  def test_headshot_filename_uses_player_name_and_id(self) -> None:
    player = {"id": 1631107, "full_name": "Nikola Jović", "first_name": "Nikola", "last_name": "Jović"}
    self.assertEqual(headshot_filename(player), "nikola-jovic-1631107.png")

  def test_build_name_keys_contains_comma_and_plain_variants(self) -> None:
    player = {"id": 1631107, "full_name": "Nikola Jović", "first_name": "Nikola", "last_name": "Jović"}
    keys = build_name_keys(player)
    self.assertIn("nikola jovic", keys)
    self.assertIn("jovic, nikola", keys)


if __name__ == "__main__":
  unittest.main()
