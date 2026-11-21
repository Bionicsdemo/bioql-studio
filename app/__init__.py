"""BioQL Studio's local service and native shell: Agtr Desktop's pattern, with its identity changed.

Everything in this package is a fork of Agtr Desktop
(~/Library/Application Support/AGTR/agtr-os/agtrdesktop/service.py and desktop/macos/Sources/*.swift).
Agtr's own identity is never reused, because reusing it is how the fork would attach to the running Agtr Desktop and
read its token:

    Agtr Desktop                        BioQL Studio
    ------------------------------      ------------------------------------
    port 7790 (service.py:5, 1487)      port 7810
    ping name "agtr-desktop" (:1291)    ping name "bioql-studio"
    ~/…/AGTR/desktop (Service.swift:8)  studio.home() = ~/…/BioQL Studio
    ~/Library/Logs/AgtrDesktop.log      ~/Library/Logs/BioQL Studio.log
    com.agtr.desktop (Info.plist)       com.molevera.bioqlstudio
    hub on 7799, VS Code proxy          neither: Studio has no hub and no editor

Ports Studio must never bind, and does not: 3000 and 5035 (BioQL's frontend and backend), 7790/7799/7797/7788/7791
(Agtr Desktop and its neighbours).
"""
from __future__ import annotations

from pathlib import Path

PORT = 7810                                 # Studio's own port. Never 7790 (Agtr), never 3000/5035 (BioQL).
PING_NAME = "bioql-studio"                  # Agtr answers "agtr-desktop" (service.py:1291); this never does.
BUNDLE_ID = "com.molevera.bioqlstudio"      # a guess: no bundle id was prescribed. Not "com.agtr.*", not "com.bioql.*"
                                            # (BioQL's own LaunchAgents are com.bioql.localstack / .watchdog /
                                            # .llm-watchdog; Studio installs no LaunchAgent at all).
VERSION = "0.1.20"


def log_path() -> Path:
    """Where the service's own output goes. Agtr: ~/Library/Logs/AgtrDesktop.log (Service.swift:9-10)."""
    return Path.home() / "Library" / "Logs" / "BioQL Studio.log"
