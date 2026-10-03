"""Vozi: dictado por voz local y rápido para Linux."""

import os

__version__ = "1.0.0"

SOCKET_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}", "vozi.sock")
