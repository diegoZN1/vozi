"""Línea de órdenes de Vozi.

Las órdenes que ejecuta el atajo de teclado (toggle, stop…) solo importan socket y sys
para responder en milisegundos; el resto de módulos se carga cuando hace falta.
"""

import socket
import sys

from . import SOCKET_PATH, __version__

CLIENT_CMDS = {
    "toggle": "Empieza a grabar o, si ya graba, termina y transcribe",
    "start": "Empieza a grabar",
    "stop": "Termina de grabar y transcribe",
    "cancel": "Descarta la grabación en curso",
    "status": "Muestra el estado del servicio",
    "last": "Imprime el último texto transcrito",
    "quit": "Detiene el servicio",
}


def send(cmd: str) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10)
        s.connect(SOCKET_PATH)
        s.sendall(cmd.encode())
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while data := s.recv(4096):
            chunks.append(data)
    return b"".join(chunks).decode().rstrip("\n")


def client(cmd: str) -> int:
    try:
        print(send(cmd))
        return 0
    except (FileNotFoundError, ConnectionRefusedError):
        msg = "El servicio no está corriendo. Inícialo con: systemctl --user start vozi"
        print(msg, file=sys.stderr)
        if not sys.stdout.isatty():  # lanzado desde el atajo: avisar en pantalla
            import subprocess

            subprocess.run(["notify-send", "--app-name=Vozi", "--icon=dialog-error", "Vozi", msg])
        return 1


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] in CLIENT_CMDS:
        sys.exit(client(sys.argv[1]))
    sys.exit(_full_cli())


def _full_cli() -> int:
    import argparse

    p = argparse.ArgumentParser(prog="vozi", description="Dictado por voz local para Linux.")
    p.add_argument("--version", action="version", version=f"vozi {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="ORDEN")
    for name, help_ in CLIENT_CMDS.items():
        sub.add_parser(name, help=help_)

    d = sub.add_parser("daemon", help="Ejecuta el servicio en primer plano (lo usa systemd)")
    d.add_argument("-v", "--verbose", action="store_true")

    i = sub.add_parser("install", help="Instala el servicio de usuario y los atajos de GNOME")
    i.add_argument("--key", default="F8", help="Atajo para grabar/terminar (por defecto F8; '' = ninguno)")
    i.add_argument("--stop-key", default="F9", help="Atajo solo para terminar (por defecto F9; '' = ninguno)")
    i.add_argument("--paste", action="store_true", help="Activa además el pegado automático (pide contraseña)")

    sub.add_parser("uninstall", help="Quita el servicio, los atajos y el permiso de pegado")

    sp = sub.add_parser("setup-paste", help="Da permiso para pegar automáticamente (pide contraseña)")
    sp.add_argument("--off", action="store_true", help="Desactiva el pegado y quita el permiso")

    sub.add_parser("devices", help="Lista los micrófonos disponibles")
    so = sub.add_parser("sounds", help="Reproduce los sonidos de cada estilo")
    so.add_argument("theme", nargs="?", help="burbuja, moderno o clasico (por defecto, todos)")
    c = sub.add_parser("config", help="Muestra la configuración, o cambia una opción: config <clave> <valor>")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="?")
    sub.add_parser("restart", help="Reinicia el servicio (aplica cambios de configuración)")
    sub.add_parser("logs", help="Muestra el registro del servicio en vivo")

    t = sub.add_parser("transcribe", help="Transcribe un archivo de audio (para probar)")
    t.add_argument("file")

    args = p.parse_args()
    if args.cmd is None:
        p.print_help()
        return 0
    if args.cmd in CLIENT_CMDS:
        return client(args.cmd)
    return globals()[f"cmd_{args.cmd.replace('-', '_')}"](args)


def _setup_logging(verbose: bool = False) -> None:
    import logging
    import os

    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    fmt = "%(levelname)s %(name)s: %(message)s"
    if not os.environ.get("JOURNAL_STREAM"):  # journald ya pone la hora
        fmt = "%(asctime)s " + fmt
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt)
    for noisy in ("httpx", "httpcore", "huggingface_hub", "faster_whisper", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_daemon(args) -> int:
    _setup_logging(args.verbose)
    from .daemon import run

    run()
    return 0


def cmd_install(args) -> int:
    from . import config, install

    path = config.write_default()
    cfg = config.load()
    print(f"Configuración: {path}")

    if "/" not in cfg.model:
        print(f"Comprobando el modelo '{cfg.model}' (la primera vez se descarga)…")
        from faster_whisper.utils import download_model

        download_model(cfg.model)

    install.install_service()
    print(f"Servicio instalado y en marcha ({install.UNIT_FILE})")

    warnings = install.install_shortcuts({"toggle": args.key, "stop": args.stop_key})
    if args.key:
        print(f"Atajo {args.key}: grabar / terminar")
    if args.stop_key:
        print(f"Atajo {args.stop_key}: terminar")
    for w in warnings:
        print(f"Aviso: {w}")

    if args.paste:
        import argparse

        return cmd_setup_paste(argparse.Namespace(off=False))
    from .output import paste_available

    if not paste_available():
        print("\nEl texto se copiará al portapapeles. Para que además se pegue solo en la ventana activa:\n"
              "  vozi setup-paste")
    return 0


def cmd_uninstall(args) -> int:
    from . import install

    install.remove_shortcuts()
    install.uninstall_service()
    print("Servicio y atajos eliminados.")
    if install.UDEV_RULE.exists():
        print("Quitando el permiso de pegado (pide contraseña)…")
        install.remove_paste()
    from .config import CONFIG_DIR

    print(f"Se conservan la configuración ({CONFIG_DIR}) y los modelos (~/.cache/huggingface).")
    return 0


def cmd_setup_paste(args) -> int:
    import subprocess

    from . import install
    from .output import paste_available

    if args.off:
        install.enable_paste_in_config(False)
        install.remove_paste()
        print("Pegado automático desactivado.")
    else:
        if not paste_available():
            print("Se instalará una regla udev para que tu sesión pueda crear un teclado virtual\n"
                  f"({install.UDEV_RULE}). Se pedirá tu contraseña.")
            try:
                install.setup_paste()
            except subprocess.CalledProcessError:
                print("No se pudo instalar el permiso.", file=sys.stderr)
                return 1
        if not paste_available():
            print("La regla está instalada pero aún no hay acceso a /dev/uinput. Cierra sesión y vuelve a entrar.")
        install.enable_paste_in_config(True)
        print("Pegado automático activado.")
    install.systemctl("try-restart", install.SERVICE, check=False)
    return 0


def cmd_devices(args) -> int:
    from .recorder import list_sources

    print("Micrófonos (pon el nombre en input_device de la configuración):\n")
    for src in list_sources():
        mark = "*" if src["default"] else " "
        print(f" {mark} {src['description']}\n     {src['name']}")
    print("\n* = predeterminado del sistema (lo que usa Vozi si input_device está vacío)")
    return 0


def cmd_sounds(args) -> int:
    import time

    from .feedback import EVENTS, THEMES, play, sounds

    labels = {"start": "empezar", "stop": "terminar", "done": "texto listo", "error": "error"}
    for theme in [args.theme] if args.theme else THEMES:
        if theme not in THEMES:
            print(f"Estilo desconocido: {theme} (opciones: {', '.join(THEMES)})", file=sys.stderr)
            return 1
        print(f"{theme}:")
        for event in EVENTS:
            print(f"  {labels[event]}")
            play(sounds(theme)[event], wait=True)
            time.sleep(0.35)
        time.sleep(0.5)
    print("\nPara elegir uno: vozi config sound_theme <estilo>")
    return 0


def cmd_config(args) -> int:
    import dataclasses
    import json
    import subprocess

    from . import config

    config.write_default()
    if not args.key:
        print(f"# {config.CONFIG_FILE}\n")
        print(config.CONFIG_FILE.read_text())
        return 0
    types = {f.name: f.type for f in dataclasses.fields(config.Config)}
    if args.key not in types or args.value is None:
        print(f"Uso: vozi config <clave> <valor>. Claves: {', '.join(types)}", file=sys.stderr)
        return 1
    kind, value = types[args.key], args.value
    if kind == "bool":
        if value.lower() not in ("true", "false", "sí", "si", "no"):
            print("Valor esperado: true o false", file=sys.stderr)
            return 1
        value = "true" if value.lower() in ("true", "sí", "si") else "false"
    elif kind == "int":
        if not value.lstrip("-").isdigit():
            print("Valor esperado: un número entero", file=sys.stderr)
            return 1
    else:
        value = json.dumps(value, ensure_ascii=False)
    config.set_value(args.key, value)
    print(f"{args.key} = {value}")
    subprocess.run(["systemctl", "--user", "try-restart", "vozi"])
    return 0


def cmd_restart(args) -> int:
    import subprocess

    return subprocess.run(["systemctl", "--user", "restart", "vozi"]).returncode


def cmd_logs(args) -> int:
    import os

    os.execvp("journalctl", ["journalctl", "--user", "-u", "vozi", "-n", "50", "-f"])


def cmd_transcribe(args) -> int:
    import time

    _setup_logging()
    from . import config
    from .engine import Engine

    audio = _read_audio(args.file)
    engine = Engine(config.load())
    print(f"Modelo cargado en {engine.load():.2f}s")
    t = time.perf_counter()
    text = engine.transcribe(audio)
    print(f"{len(audio) / 16000:.1f}s de audio transcritos en {time.perf_counter() - t:.2f}s:\n{text}")
    return 0


def _read_audio(path: str):
    """Audio a float32 16 kHz mono: con ffmpeg si está, o WAV con la librería estándar."""
    import shutil
    import subprocess
    import wave

    import numpy as np

    if shutil.which("ffmpeg"):
        raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-ac", "1", "-ar", "16000",
                              "-f", "s16le", "-"], capture_output=True, check=True).stdout
    else:
        with wave.open(path) as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                raise SystemExit("Sin ffmpeg solo se aceptan WAV de 16 kHz, mono, 16 bits.")
            raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
