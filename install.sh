#!/usr/bin/env bash
# Instala Vozi para el usuario actual.
# Uso: ./install.sh [--paste] [--key F8] [--stop-key F9]
set -euo pipefail
cd "$(dirname "$0")"

missing=()
need() { command -v "$1" >/dev/null || missing+=("$2"); }
need pw-record pipewire-utils
need wl-copy wl-clipboard
need notify-send libnotify
need gsettings glib2
need uv uv
if ((${#missing[@]})); then
    echo "Instalando dependencias del sistema: ${missing[*]}"
    sudo dnf install -y "${missing[@]}"
fi

echo "Instalando vozi en ~/.local/bin…"
uv tool install --quiet --force --reinstall-package vozi .

exec "$HOME/.local/bin/vozi" install "$@"
