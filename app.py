#!/usr/bin/env python3
"""Compatibility entrypoint for the Injury Report dashboard.

Copyright (c) 2026 Lucas Berry.
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import os

from injury_report_dashboard import app, start_scheduler


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
