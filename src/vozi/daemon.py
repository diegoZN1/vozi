"""Servicio en segundo plano: mantiene el modelo cargado y atiende órdenes por un socket Unix.

Sin uso no consume CPU: el hilo principal queda bloqueado en accept() y el micrófono
cerrado. El atajo de teclado de GNOME ejecuta `vozi toggle`, que manda la orden.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import socket
import threading
import time

import numpy as np

from . import SOCKET_PATH
from .config import Config
from .engine import SAMPLE_RATE, Engine, trim_memory
from .feedback import Feedback
from .output import Output
from .recorder import Recorder, to_float

log = logging.getLogger(__name__)

MIN_AUDIO_S = 0.3
STREAM_POLL_S = 1.0
QUIET_DBFS = -32  # por debajo, el micrófono casi no captó nada (la voz suele estar entre -25 y -5)


def level_dbfs(audio: np.ndarray) -> float:
    """Nivel de los sonidos más fuertes (percentil 99, para ignorar chasquidos sueltos)."""
    return 20 * np.log10(max(float(np.percentile(np.abs(audio), 99)), 1e-6))


class Session:
    """Una grabación. Mientras dura, lo ya dictado se transcribe por adelantado en cada pausa."""

    def __init__(self, buffer: bytearray):
        self.buffer = buffer
        self.ended = threading.Event()
        self.texts: list[str] = []
        self.done = 0  # muestras ya transcritas
        self.thread: threading.Thread | None = None


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = Engine(cfg)
        self.recorder = Recorder(cfg.input_device)
        self.output = Output(cfg)
        self.fb = Feedback(cfg)
        self.jobs: queue.Queue = queue.Queue()
        self.lock = threading.Lock()
        self.pending = 0
        self.rec_started = 0.0
        self.rec_timer: threading.Timer | None = None
        self.session: Session | None = None
        self.last_text = ""
        self.last_stats = ""
        self.running = True

    # --- ciclo principal --------------------------------------------------

    def serve(self) -> None:
        if os.path.exists(SOCKET_PATH):
            try:
                with socket.socket(socket.AF_UNIX) as probe:
                    probe.connect(SOCKET_PATH)
                raise SystemExit("Vozi ya está corriendo.")
            except ConnectionRefusedError:
                os.unlink(SOCKET_PATH)

        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(8)

        def shutdown(*_):
            self.running = False
            srv.close()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)

        threading.Thread(target=self._load_model, daemon=True).start()
        threading.Thread(target=self._worker, daemon=True).start()
        if self.cfg.unload_after_min > 0:
            threading.Thread(target=self._idle_unloader, daemon=True).start()
        log.info("Escuchando en %s", SOCKET_PATH)

        try:
            while self.running:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                with conn:
                    conn.settimeout(2)
                    try:
                        cmd = conn.recv(256).decode().strip()
                        conn.sendall((self.handle(cmd) + "\n").encode())
                    except Exception as e:
                        log.exception("Error atendiendo orden")
                        try:
                            conn.sendall(f"error: {e}\n".encode())
                        except OSError:
                            pass
        finally:
            with self.lock:
                if self.recorder.recording:
                    self.recorder.cancel()
            self.output.close()
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
            log.info("Detenido")

    def handle(self, cmd: str) -> str:
        if cmd == "toggle":
            with self.lock:
                return self._stop() if self.recorder.recording else self._start()
        if cmd == "start":
            with self.lock:
                return self._start()
        if cmd == "stop":
            with self.lock:
                return self._stop()
        if cmd == "cancel":
            with self.lock:
                return self._cancel()
        if cmd == "status":
            return self._status()
        if cmd == "last":
            return self.last_text
        if cmd == "quit":
            os.kill(os.getpid(), signal.SIGTERM)
            return "saliendo"
        return f"orden desconocida: {cmd!r}"

    # --- estados ----------------------------------------------------------

    def _start(self) -> str:
        if self.recorder.recording:
            return "ya grabando"
        try:
            self.recorder.start()
        except OSError as e:
            self._error(f"No se pudo abrir el micrófono: {e}")
            return "error"
        self.rec_started = time.monotonic()
        self.session = Session(self.recorder.buffer)
        self.session.thread = threading.Thread(target=self._stream, args=(self.session,), daemon=True)
        self.session.thread.start()
        self.rec_timer = threading.Timer(self.cfg.max_seconds, self._timeout, args=(self.session,))
        self.rec_timer.daemon = True
        self.rec_timer.start()
        if not self.engine.loaded:  # recargar mientras hablas, así no se nota
            threading.Thread(target=self._load_model, daemon=True).start()
        self.fb.sound("start")
        self.fb.notify("Grabando…", "Pulsa el atajo otra vez para terminar")
        return "grabando"

    def _stop(self) -> str:
        if not self.recorder.recording:
            return "no estaba grabando"
        self._cancel_timer()
        session, self.session = self.session, None
        session.ended.set()
        try:
            audio = self.recorder.stop()
        except RuntimeError as e:
            self._error(str(e))
            return "error"
        self.fb.sound("stop")
        seconds = len(audio) / SAMPLE_RATE
        if seconds < MIN_AUDIO_S:
            self.fb.notify("Grabación demasiado corta", icon="microphone-sensitivity-muted")
            return "muy corto"
        self.pending += 1
        self.jobs.put((session, audio))
        return f"transcribiendo {seconds:.1f}s"

    def _cancel(self) -> str:
        if not self.recorder.recording:
            return "no estaba grabando"
        self._cancel_timer()
        self.session.ended.set()
        self.session = None
        self.recorder.cancel()
        self.fb.notify("Dictado cancelado", icon="microphone-sensitivity-muted")
        return "cancelado"

    def _timeout(self, session: Session) -> None:
        with self.lock:
            if self.session is session:  # que no detenga una grabación posterior
                log.warning("Límite de %ss alcanzado, deteniendo", self.cfg.max_seconds)
                self._stop()

    def _cancel_timer(self) -> None:
        if self.rec_timer:
            self.rec_timer.cancel()
            self.rec_timer = None

    def _status(self) -> str:
        if self.recorder.recording:
            state = f"grabando ({time.monotonic() - self.rec_started:.0f}s)"
        elif self.pending:
            state = "transcribiendo"
        else:
            state = "listo"
        model = f"{self.cfg.model} ({'cargado' if self.engine.loaded else 'no cargado'})"
        if self.output.keyboard:
            paste = "sí"
        else:
            paste = f"no — {self.output.paste_error}" if self.output.paste_error else "no"
        lines = [f"estado: {state}", f"modelo: {model}", f"pegado automático: {paste}"]
        if self.last_stats:
            lines.append(f"último: {self.last_stats}")
        return "\n".join(lines)

    # --- trabajo pesado ---------------------------------------------------

    def _load_model(self) -> None:
        try:
            self.engine.load()
        except Exception as e:
            log.exception("No se pudo cargar el modelo")
            self._error(f"No se pudo cargar el modelo {self.cfg.model!r}: {e}")

    def _stream(self, session: Session) -> None:
        """Mientras se graba, transcribe hasta la última pausa para que al terminar quede poco."""
        try:
            while not session.ended.wait(STREAM_POLL_S):
                if not self.engine.loaded:
                    continue
                audio = to_float(bytes(session.buffer))[session.done :]
                cut = self.engine.find_cut(audio)
                if cut and not session.ended.is_set():
                    t = time.perf_counter()
                    text = self.engine.transcribe(audio[:cut], context=" ".join(session.texts))
                    log.info("Adelantado %.1fs de audio en %.2fs", cut / SAMPLE_RATE, time.perf_counter() - t)
                    if text:
                        session.texts.append(text)
                    session.done += cut
        except Exception:
            log.exception("Fallo en la transcripción anticipada; se hará al terminar")
            session.texts.clear()
            session.done = 0

    def _worker(self) -> None:
        while True:
            session, audio = self.jobs.get()
            try:
                self._process(session, audio)
            except Exception as e:
                log.exception("Fallo al transcribir")
                self._error(f"Fallo al transcribir: {e}")
            finally:
                trim_memory()
                with self.lock:
                    self.pending -= 1

    def _process(self, session: Session, audio) -> None:
        t = time.perf_counter()
        session.thread.join()  # si estaba adelantando un tramo, esperar a que acabe
        rest = audio[session.done :]
        texts = list(session.texts)
        if len(rest) >= MIN_AUDIO_S * SAMPLE_RATE:
            texts.append(self.engine.transcribe(rest, context=" ".join(texts)))
        text = " ".join(t for t in texts if t)
        elapsed = time.perf_counter() - t
        level = level_dbfs(audio)
        self.last_stats = f"{len(audio) / SAMPLE_RATE:.1f}s de audio en {elapsed:.2f}s, nivel {level:.0f} dBFS"
        log.info("Transcrito %s: %r", self.last_stats, text[:80])
        if not text:
            hint = ""
            if level < QUIET_DBFS:
                hint = "El micrófono casi no captó sonido. Revisa cuál está en uso: vozi devices"
            self.fb.notify("No se detectó voz", hint, icon="microphone-sensitivity-muted")
            return
        self.last_text = text
        pasted = self.output.deliver(text)
        self.fb.sound("done")
        if pasted:
            self.fb.notify("Pegado ✔", text, icon="edit-paste")
        elif self.output.paste_error:
            note = f"\n\n(Pegado automático: {self.output.paste_error})"
            self.fb.notify("Copiado al portapapeles ✔", text + note, icon="edit-copy")
        else:
            self.fb.notify("Copiado al portapapeles ✔", text, icon="edit-copy")

    def _idle_unloader(self) -> None:
        limit = self.cfg.unload_after_min * 60
        while True:
            time.sleep(30)
            idle = time.monotonic() - self.engine.last_used
            if self.engine.loaded and idle > limit and not self.recorder.recording and not self.pending:
                self.engine.unload()

    def _error(self, msg: str) -> None:
        log.error(msg)
        self.fb.sound("error")
        self.fb.notify("Vozi: error", msg, icon="dialog-error", error=True)


def run() -> None:
    from . import config

    cfg = config.load()
    Daemon(cfg).serve()
