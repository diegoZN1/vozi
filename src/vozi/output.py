"""Salida del texto: portapapeles (wl-copy) y pegado automático con un teclado virtual.

En Wayland las apps no pueden inyectar teclas en otras ventanas. El pegado usa un
teclado virtual de /dev/uinput (como ydotool, pero sin daemon extra) que requiere
permiso de escritura en /dev/uinput: lo da `vozi setup-paste` con una regla udev.
"""

from __future__ import annotations

import fcntl
import logging
import os
import struct
import subprocess
import time

log = logging.getLogger(__name__)

UINPUT = "/dev/uinput"
EV_SYN, EV_KEY, SYN_REPORT = 0x00, 0x01, 0x00
KEY_CODES = {"ctrl": 29, "shift": 42, "alt": 56, "super": 125, "insert": 110, "v": 47}

# ioctl de <linux/uinput.h>
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_SETUP = 0x405C5503  # _IOW('U', 3, struct uinput_setup), sizeof = 92
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
BUS_VIRTUAL = 0x06


def parse_keys(spec: str) -> list[int]:
    try:
        return [KEY_CODES[k.strip().lower()] for k in spec.split("+")]
    except KeyError as e:
        raise ValueError(f"Tecla no soportada en paste_keys: {e.args[0]!r} (usa {', '.join(KEY_CODES)})") from None


class VirtualKeyboard:
    def __init__(self):
        self.fd = os.open(UINPUT, os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            for code in KEY_CODES.values():
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
            setup = struct.pack("HHHH80sI", BUS_VIRTUAL, 0x1209, 0x5770, 1, b"Vozi virtual keyboard", 0)
            fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
        except OSError:
            os.close(self.fd)
            raise
        self._created = time.monotonic()

    def _emit(self, code: int, value: int) -> None:
        os.write(self.fd, struct.pack("llHHi", 0, 0, EV_KEY, code, value))
        os.write(self.fd, struct.pack("llHHi", 0, 0, EV_SYN, SYN_REPORT, 0))
        time.sleep(0.008)

    def press(self, codes: list[int]) -> None:
        # El compositor tarda un momento en registrar un dispositivo recién creado.
        wait = 0.5 - (time.monotonic() - self._created)
        if wait > 0:
            time.sleep(wait)
        for code in codes:
            self._emit(code, 1)
        for code in reversed(codes):
            self._emit(code, 0)

    def close(self) -> None:
        try:
            fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        finally:
            os.close(self.fd)


def paste_available() -> bool:
    return os.access(UINPUT, os.W_OK)


class Output:
    def __init__(self, cfg):
        self.cfg = cfg
        self.keys = parse_keys(cfg.paste_keys)
        self.keyboard: VirtualKeyboard | None = None
        self.paste_error = ""
        if cfg.paste:
            try:
                self.keyboard = VirtualKeyboard()
            except OSError as e:
                self.paste_error = f"sin acceso a {UINPUT} ({e.strerror}); ejecuta: vozi setup-paste"
                log.warning("Pegado automático desactivado: %s", self.paste_error)

    def deliver(self, text: str) -> bool:
        """Copia el texto y, si está activado, lo pega. Devuelve True si se pegó."""
        pasting = self.keyboard is not None
        if pasting:
            text += " "  # para que dictados seguidos no queden pegados entre sí
        # Shift+Insert pega la selección PRIMARY en terminales y CLIPBOARD en el resto:
        # se llenan ambas con el mismo texto para que funcione en cualquier ventana.
        copy(text, primary=pasting and KEY_CODES["insert"] in self.keys)
        if pasting:
            self.keyboard.press(self.keys)
        return pasting

    def close(self) -> None:
        if self.keyboard:
            self.keyboard.close()
            self.keyboard = None


def copy(text: str, primary: bool = False) -> None:
    # wl-copy deja un proceso hijo sirviendo el portapapeles: no se le pasan pipes de
    # salida porque el hijo los heredaría y esperar su EOF bloquearía para siempre.
    data = text.encode()
    targets = [[]] + ([["--primary"]] if primary else [])
    procs = [subprocess.Popen(["wl-copy", "--type", "text/plain;charset=utf-8", *extra], stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for extra in targets]
    for p in procs:
        p.stdin.write(data)
        p.stdin.close()
    for p in procs:
        if p.wait(timeout=5):
            raise RuntimeError(f"wl-copy falló (código {p.returncode})")
