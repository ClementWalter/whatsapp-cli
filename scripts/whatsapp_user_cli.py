#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp>=3.10",
#     "click>=8.1",
#     "cryptography>=43",
#     "protobuf>=5",
#     "qrcode[pil]>=7.4",
#     "signal-protocol>=0.2",
#     "pyobjc-framework-Contacts>=10; sys_platform == 'darwin'",
# ]
# ///
"""Standalone launcher for the WhatsApp CLI.

PEP 723 inline metadata makes ``./scripts/whatsapp_user_cli.py`` runnable
directly via ``uv run`` without installing the package — useful for fresh
checkouts and the linked-skill flow. The real CLI lives in ``wa.cli``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add the repo root to sys.path so the sibling `wa/` package is importable
# when this file is executed directly (not installed as a console script).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wa.cli import cli

if __name__ == "__main__":
    cli()
