"""Integración con el sistema: servicio systemd de usuario, atajos de GNOME y permiso de pegado."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config

SERVICE = "vozi.service"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
UNIT_FILE = UNIT_DIR / SERVICE
UDEV_RULE = Path("/etc/udev/rules.d/70-vozi-uinput.rules")

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
KEYBINDING_DIR = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
SHORTCUTS = {  # id: (nombre, orden)
    "toggle": ("Vozi: grabar / terminar", "toggle"),
    "stop": ("Vozi: terminar", "stop"),
}


def executable() -> str:
    exe = Path(sys.executable).parent / "vozi"
    if exe.exists():
        return str(exe)
    return shutil.which("vozi") or f"{sys.executable} -m vozi"


def systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], check=check, capture_output=True, text=True)


# --- servicio -------------------------------------------------------------


def install_service() -> None:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    UNIT_FILE.write_text(f"""\
[Unit]
Description=Vozi: dictado por voz
PartOf=graphical-session.target
After=graphical-session.target pipewire.service

[Service]
Type=simple
ExecStart={executable()} daemon
Restart=on-failure
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=graphical-session.target
""")
    systemctl("daemon-reload")
    systemctl("enable", SERVICE)
    systemctl("restart", SERVICE)


def uninstall_service() -> None:
    systemctl("disable", "--now", SERVICE, check=False)
    UNIT_FILE.unlink(missing_ok=True)
    systemctl("daemon-reload", check=False)


# --- atajos de GNOME ------------------------------------------------------


def _gsettings(*args: str) -> str:
    return subprocess.run(["gsettings", *args], check=True, capture_output=True, text=True).stdout.strip()


def _custom_list() -> list[str]:
    raw = _gsettings("get", MEDIA_KEYS, "custom-keybindings")
    return ast.literal_eval(raw.removeprefix("@as "))


def _set_custom_list(paths: list[str]) -> None:
    _gsettings("set", MEDIA_KEYS, "custom-keybindings", str(paths))


def _schema(path: str) -> str:
    return f"{MEDIA_KEYS}.custom-keybinding:{path}"


def install_shortcuts(keys: dict[str, str]) -> list[str]:
    """keys: {'toggle': 'F8', 'stop': 'F9'}; una tecla vacía omite ese atajo. Devuelve avisos."""
    remove_shortcuts()
    paths = _custom_list()
    warnings = []
    taken = {}
    for p in paths:
        try:
            binding = _gsettings("get", _schema(p), "binding").strip("'")
            taken[binding] = _gsettings("get", _schema(p), "name").strip("'")
        except subprocess.CalledProcessError:
            pass
    exe = executable()
    for sid, key in keys.items():
        if not key:
            continue
        name, cmd = SHORTCUTS[sid]
        if key in taken:
            warnings.append(f"{key} ya lo usa el atajo '{taken[key]}'; revisa Configuración > Teclado.")
        path = f"{KEYBINDING_DIR}vozi-{sid}/"
        _gsettings("set", _schema(path), "name", name)
        _gsettings("set", _schema(path), "command", f"{exe} {cmd}")
        _gsettings("set", _schema(path), "binding", key)
        paths.append(path)
    _set_custom_list(paths)
    return warnings


def remove_shortcuts() -> None:
    paths = _custom_list()
    ours = [p for p in paths if p.startswith(f"{KEYBINDING_DIR}vozi-")]
    for p in ours:
        for key in ("name", "command", "binding"):
            subprocess.run(["gsettings", "reset", _schema(p), key], capture_output=True)
    if ours:
        _set_custom_list([p for p in paths if p not in ours])


# --- permiso para pegar ---------------------------------------------------


def _run_as_root(script: str) -> None:
    runner = ["sudo"] if sys.stdin.isatty() else ["pkexec"]
    subprocess.run([*runner, "sh", "-c", script], check=True)


def setup_paste() -> None:
    rule = 'KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", OPTIONS+="static_node=uinput"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".rules", delete=False) as f:
        f.write("# Vozi: permite al usuario de la sesión activa crear un teclado virtual para pegar.\n")
        f.write(rule)
    try:
        _run_as_root(
            f"modprobe uinput; install -m 0644 {f.name} {UDEV_RULE} && udevadm control --reload-rules"
            " && udevadm trigger --action=change --name-match=uinput --settle"
        )
    finally:
        os.unlink(f.name)


def remove_paste() -> None:
    if UDEV_RULE.exists():
        _run_as_root(f"rm -f {UDEV_RULE} && udevadm control --reload-rules"
                     " && udevadm trigger --action=change --name-match=uinput --settle")


def enable_paste_in_config(enabled: bool) -> None:
    config.set_value("paste", "true" if enabled else "false")
