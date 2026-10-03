"""Grabación con pw-record (PipeWire): el micrófono solo se abre mientras grabas."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SKIP_BYTES = SAMPLE_RATE // 100 * 2  # 10 ms: algunos micrófonos USB dan un chasquido al abrirse
_PROPS = '{ application.name = "Vozi" node.name = "vozi" media.role = "Communication" }'


class Recorder:
    def __init__(self, device: str = ""):
        self.device = device
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._buf = bytearray()

    @property
    def recording(self) -> bool:
        return self._proc is not None

    @property
    def buffer(self) -> bytearray:
        """Búfer de la grabación actual; cada grabación usa uno nuevo."""
        return self._buf

    def start(self) -> None:
        if self._proc:
            return
        cmd = ["pw-record", "--rate", str(SAMPLE_RATE), "--channels", "1", "--format", "s16",
               "--latency", "30ms", "-P", _PROPS]
        if self.device:
            cmd += ["--target", self.device]
        cmd.append("-")
        self._buf = bytearray()
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._reader = threading.Thread(target=self._read, args=(self._proc,), daemon=True)
        self._reader.start()

    def _read(self, proc: subprocess.Popen) -> None:
        buf, fd, skip = self._buf, proc.stdout.fileno(), SKIP_BYTES
        while chunk := os.read(fd, 65536):
            if skip:
                chunk, skip = chunk[skip:], max(0, skip - len(chunk))
            buf += chunk

    def stop(self) -> np.ndarray:
        """Detiene la grabación y devuelve el audio como float32 a 16 kHz mono."""
        proc, self._proc = self._proc, None
        if proc is None:
            return np.zeros(0, dtype=np.float32)
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self._reader.join(timeout=2)
        err = proc.stderr.read().decode(errors="replace").strip()
        proc.stderr.close()
        proc.stdout.close()

        data = bytes(self._buf)
        self._buf = bytearray()
        if not data and err:
            raise RuntimeError(f"pw-record falló: {err}")
        return to_float(data)

    def cancel(self) -> None:
        self.stop()


def to_float(data: bytes) -> np.ndarray:
    """PCM s16le de pw-record a float32 en [-1, 1]."""
    if data[:4] == b"RIFF":  # por si alguna versión escribe cabecera WAV
        data = data[44:]
    data = data[: len(data) // 2 * 2]
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def list_sources() -> list[dict]:
    """Micrófonos de PipeWire: [{'id', 'name', 'description', 'default'}]."""
    out = subprocess.run(["pw-dump"], capture_output=True, text=True, check=True).stdout
    objects = json.loads(out)
    default = ""
    for obj in objects:
        if obj.get("type") == "PipeWire:Interface:Metadata" and obj.get("props", {}).get("metadata.name") == "default":
            for item in obj.get("metadata", []):
                if item.get("key") == "default.audio.source":
                    value = item.get("value") or {}
                    default = value.get("name", "") if isinstance(value, dict) else ""
    sources = []
    for obj in objects:
        props = obj.get("info", {}).get("props", {})
        if obj.get("type") == "PipeWire:Interface:Node" and props.get("media.class") == "Audio/Source":
            name = props.get("node.name", "")
            sources.append({
                "id": obj["id"],
                "name": name,
                "description": props.get("node.description") or props.get("node.nick") or name,
                "default": name == default,
            })
    return sources
