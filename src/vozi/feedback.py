"""Avisos: sonidos sintetizados en memoria (pw-play) y notificaciones de escritorio.

Los sonidos se generan con numpy al primer uso, sin archivos: notas tipo vidrio (síntesis FM),
burbujas que deslizan el tono y una reverb estéreo corta que les da espacio.
"""

from __future__ import annotations

import functools
import logging
import queue
import subprocess
import threading

import numpy as np

log = logging.getLogger(__name__)

RATE = 48000
EVENTS = ("start", "stop", "done", "error")


# --- síntesis -------------------------------------------------------------


def _t(dur: float) -> np.ndarray:
    return np.arange(int(RATE * dur)) / RATE


def _pluck(f: float, dur: float = 0.35, decay: float = 10.0, brightness: float = 1.5, ratio: float = 2.0):
    """Nota tipo vidrio o piano eléctrico (FM): brillante al golpe y se apaga suave."""
    t = _t(dur)
    index = brightness * np.exp(-t * 20)
    envelope = np.minimum(1.0, t / 0.003) * np.exp(-t * decay)
    return np.sin(2 * np.pi * f * t + index * np.sin(2 * np.pi * f * ratio * t)) * envelope


def _bloop(f0: float, f1: float, dur: float = 0.16, sweep: float = 0.07, decay: float = 22.0):
    """Burbuja: tono redondo que sube (o baja) de f0 a f1 en `sweep` segundos."""
    t = _t(dur)
    freq = f0 * (f1 / f0) ** np.minimum(t / sweep, 1.0)
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    envelope = np.minimum(1.0, t / 0.004) * np.exp(-t * decay)
    return (np.sin(phase) + 0.2 * np.sin(2 * phase)) * envelope


def _tone(freqs: list[float], dur: float = 0.07):
    """Pitidos simples (estilo clásico)."""
    t = _t(dur)
    envelope = np.minimum(1.0, np.minimum(t / 0.005, (dur - t) / 0.015))
    return np.concatenate([np.sin(2 * np.pi * f * t) * envelope for f in freqs])


def _chime(f: float, ring: float = 0.3):
    """Campanita simple (estilo clásico)."""
    t = _t(ring)
    envelope = np.minimum(1.0, t / 0.004) * np.exp(-t * 14)
    return (np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t)) * envelope


def _seq(*notes):
    """Mezcla notas (inicio_en_segundos, señal[, ganancia]) en una sola señal."""
    out = np.zeros(max(int(RATE * start) + len(x) for start, x, *_ in notes))
    for start, x, *gain in notes:
        i = int(RATE * start)
        out[i : i + len(x)] += x * (gain[0] if gain else 1.0)
    return out


def _space(x: np.ndarray, wet: float = 0.18, length: float = 0.4) -> np.ndarray:
    """Reverb estéreo corta: convolución con ruido que decae, distinto en cada canal."""
    rng = np.random.default_rng(7)
    t = _t(length)
    n = len(x) + len(t) - 1
    size = 1 << (n - 1).bit_length()
    spectrum = np.fft.rfft(x, size)
    channels = []
    for _ in range(2):
        ir = rng.standard_normal(len(t)) * np.exp(-t * 9)
        ir /= np.sqrt(np.sum(ir**2))
        tail = np.fft.irfft(spectrum * np.fft.rfft(ir, size), size)[:n]
        channels.append(np.pad(x, (0, n - len(x))) + wet * tail)
    out = np.stack(channels, axis=1)
    fade = int(RATE * 0.03)
    out[-fade:] *= np.linspace(1, 0, fade)[:, None]
    return out


# --- estilos --------------------------------------------------------------

E5, B5, C6, E6, G6, C7 = 659.3, 987.8, 1046.5, 1318.5, 1568.0, 2093.0


def _moderno() -> dict:
    """Notas de vidrio en quintas, como los sonidos de sistema de un teléfono actual."""
    return {
        "start": (_space(_seq((0, _pluck(E5)), (0.075, _pluck(B5)))), 0.28),
        "stop": (_space(_seq((0, _pluck(B5)), (0.075, _pluck(E5)))), 0.28),
        "done": (_space(_seq((0, _pluck(C6, 0.5, 7)), (0.06, _pluck(E6, 0.5, 7)), (0.12, _pluck(G6, 0.6, 6)),
                              (0.18, _pluck(C7, 0.7, 6), 0.5)), wet=0.25), 0.3),
        "error": (_space(_seq((0, _pluck(311, 0.25, 14, 3.0, 3.5)), (0.12, _pluck(233, 0.35, 12, 3.0, 3.5)))), 0.3),
    }


def _burbuja() -> dict:
    """Burbujas suaves que suben o bajan, como en una app de mensajería."""
    return {
        "start": (_space(_bloop(420, 880), wet=0.12), 0.25),
        "stop": (_space(_bloop(880, 420), wet=0.12), 0.25),
        "done": (_space(_seq((0, _bloop(520, 1040)), (0.09, _bloop(780, 1560))), wet=0.15), 0.25),
        "error": (_space(_seq((0, _bloop(330, 220, 0.2, 0.1, 14)), (0.16, _bloop(330, 220, 0.2, 0.1, 14)))), 0.28),
    }


def _clasico() -> dict:
    """Los pitidos originales."""
    return {
        "start": (_tone([660, 990]), 0.18),
        "stop": (_tone([990, 660]), 0.18),
        "done": (_seq((0, _chime(C6)), (0.08, _chime(E6)), (0.16, _chime(G6))), 0.16),
        "error": (_tone([220, 0, 220], dur=0.09), 0.25),
    }


THEMES = {"burbuja": _burbuja, "moderno": _moderno, "clasico": _clasico}


@functools.cache
def sounds(theme: str) -> dict[str, bytes]:
    """PCM estéreo s16 a 48 kHz de cada evento del estilo (se genera una sola vez)."""
    build = THEMES.get(theme)
    if build is None:
        log.warning("Estilo de sonido desconocido %r; usando 'burbuja'", theme)
        build = _burbuja
    pcm = {}
    for event, (x, peak) in build().items():
        if x.ndim == 1:
            x = np.stack([x, x], axis=1)
        pcm[event] = (x / np.abs(x).max() * peak * 32767).astype("<i2").tobytes()
    return pcm


def play(pcm: bytes, wait: bool = False) -> None:
    p = subprocess.Popen(["pw-play", "--raw", "--rate", str(RATE), "--channels", "2", "--format", "s16",
                          "--media-role", "Notification", "-"], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wait:
        p.communicate(pcm)
    else:  # sin esperar a que termine de sonar, para no retrasar la notificación que sigue
        threading.Thread(target=p.communicate, args=(pcm,), daemon=True).start()


# --- avisos del servicio --------------------------------------------------


class Feedback:
    def __init__(self, cfg):
        self.cfg = cfg
        self._nid = 0
        self._jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()
        if cfg.sounds:
            self._jobs.put((sounds, (cfg.sound_theme,)))  # sintetizar ya, no en el primer dictado

    def sound(self, event: str) -> None:
        if self.cfg.sounds:
            self._jobs.put((lambda: play(sounds(self.cfg.sound_theme)[event]), ()))

    def notify(self, title: str, body: str = "", icon: str = "audio-input-microphone", error: bool = False) -> None:
        if self.cfg.notifications or error:
            self._jobs.put((self._notify, (title, body, icon, error)))

    # Un solo hilo ejecuta los avisos en orden, sin bloquear la grabación ni la IA.
    def _worker(self) -> None:
        while True:
            fn, args = self._jobs.get()
            try:
                fn(*args)
            except Exception as e:
                log.debug("Aviso fallido: %s", e)

    def _notify(self, title: str, body: str, icon: str, error: bool) -> None:
        cmd = ["notify-send", "--app-name=Vozi", f"--icon={icon}", "--print-id",
               "--transient", f"--urgency={'normal' if error else 'low'}"]
        if self._nid:
            cmd.append(f"--replace-id={self._nid}")
        out = subprocess.run([*cmd, title, body[:300]], capture_output=True, text=True, timeout=5).stdout
        if out.strip().isdigit():
            self._nid = int(out.strip())
