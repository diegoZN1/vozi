"""Configuración en ~/.config/vozi/config.toml (todas las claves son opcionales)."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "vozi"
CONFIG_FILE = CONFIG_DIR / "config.toml"

DEFAULT_TOML = """\
# Configuración de Vozi. Tras editar: vozi restart

# Modelo: "small" (rápido, ligero) o "large-v3-turbo" (más preciso, ~3x más lento y ~2.5 GB de RAM).
# También: "base", "medium", "large-v3", o una ruta a un modelo CTranslate2.
model = "small"
compute_type = "int8"
# Hilos de CPU para la IA (0 = automático: la mitad de los hilos del procesador).
threads = 0

# Idioma del dictado ("es", "en", ...). Vacío = detección automática.
language = "es"
# 1 = más rápido. 5 = un poco más preciso.
beam_size = 1
# Texto de ejemplo que orienta el estilo (puntuación, ortografía, nombres propios).
initial_prompt = "Hola, amigo. Esto es un dictado profesional con comas, puntos, y excelente ortografía."
# Quita los silencios antes de transcribir (más rápido, evita alucinaciones).
vad = true

# Liberar la RAM del modelo tras N minutos sin uso (0 = nunca; recargarlo tarda ~1 s).
unload_after_min = 0

# Micrófono de PipeWire (nombre o número de `vozi devices`). Vacío = el predeterminado de GNOME.
input_device = ""
# Límite de seguridad de una grabación, en segundos.
max_seconds = 300

# Pegar automáticamente el texto en la ventana activa (requiere `vozi setup-paste`).
paste = false
# Combinación para pegar: "shift+insert" funciona en navegadores, editores y terminales.
# Alternativas: "ctrl+v", "ctrl+shift+v".
paste_keys = "shift+insert"

# Sonidos (al empezar, al terminar y cuando el texto está listo) y notificaciones de escritorio.
sounds = true
# Estilo: "burbuja", "moderno" o "clasico". Escúchalos con: vozi sounds
sound_theme = "burbuja"
notifications = true
"""


@dataclass
class Config:
    model: str = "small"
    compute_type: str = "int8"
    threads: int = 0
    language: str = "es"
    beam_size: int = 1
    initial_prompt: str = "Hola, amigo. Esto es un dictado profesional con comas, puntos, y excelente ortografía."
    vad: bool = True
    unload_after_min: int = 0
    input_device: str = ""
    max_seconds: int = 300
    paste: bool = False
    paste_keys: str = "shift+insert"
    sounds: bool = True
    sound_theme: str = "burbuja"
    notifications: bool = True


def load() -> Config:
    if not CONFIG_FILE.exists():
        return Config()
    with CONFIG_FILE.open("rb") as f:
        data = tomllib.load(f)
    known = {f.name for f in fields(Config)}
    for key in data.keys() - known:
        log.warning("Clave desconocida en %s: %s", CONFIG_FILE, key)
    return Config(**{k: v for k, v in data.items() if k in known})


def write_default(overwrite: bool = False) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if overwrite or not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_TOML)
    return CONFIG_FILE


def set_value(key: str, value: str) -> None:
    """Cambia una línea `key = ...` del archivo de config conservando los comentarios."""
    write_default()
    lines = CONFIG_FILE.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key} = {value}"
            break
    else:
        lines.append(f"{key} = {value}")
    CONFIG_FILE.write_text("\n".join(lines) + "\n")
