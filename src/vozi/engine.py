"""Motor de transcripción: faster-whisper con encoder de longitud dinámica.

Whisper siempre rellena el audio hasta una ventana de 30 s y el encoder procesa
esa ventana completa aunque solo hayas hablado 4 s. En CPU el encoder es casi
todo el costo, así que aquí se le pasa solo el audio con voz (tras el VAD) más
un pequeño margen. Para dictados cortos esto es entre 5x y 10x más rápido.
Si el resultado parece una alucinación se reintenta con la ventana completa.
"""

from __future__ import annotations

import gc
import itertools
import logging
import os
import re
import threading
import time
import zlib

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
MAX_FRAMES = 3000  # ventana nativa de Whisper: 30 s a 100 frames/s
PAD_FRAMES = 100  # 1 s de margen después de la voz
MAX_CHUNK_S = 28.0

_HALLUCINATIONS = re.compile(
    r"amara\.org|subt[ií]tulos (realizados )?por|gracias por ver|suscr[ií]bete",
    re.IGNORECASE,
)


def default_threads() -> int:
    # Con más hilos que la mitad de los lógicos el rendimiento empeora en CPUs híbridas
    # (núcleos P+E, hyperthreading) y más aún si el navegador está usando CPU.
    return max(2, (os.cpu_count() or 4) // 2)


class Engine:
    def __init__(self, cfg):
        self.cfg = cfg
        self._model = None
        self._tokenizers = {}
        self._suppress = None
        self._lock = threading.Lock()
        self.last_used = time.monotonic()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> float:
        with self._lock:
            return self._load()

    def _load(self) -> float:
        if self._model is not None:
            return 0.0
        from faster_whisper import WhisperModel

        t = time.perf_counter()
        opts = dict(device="cpu", compute_type=self.cfg.compute_type,
                    cpu_threads=self.cfg.threads or default_threads(), num_workers=1)
        try:  # sin consultar a Hugging Face en cada arranque
            self._model = WhisperModel(self.cfg.model, local_files_only=True, **opts)
        except Exception:
            log.info("Descargando el modelo %s (solo la primera vez)…", self.cfg.model)
            self._model = WhisperModel(self.cfg.model, **opts)
        self._tokenizers.clear()
        self._suppress = None
        # La primera inferencia reserva memoria y es lenta: se hace aquí, no al dictar.
        self._decode(np.zeros(SAMPLE_RATE, dtype=np.float32), allow_fallback=False)
        trim_memory()
        self.last_used = time.monotonic()
        elapsed = time.perf_counter() - t
        log.info("Modelo %s cargado en %.2fs", self.cfg.model, elapsed)
        return elapsed

    def unload(self) -> None:
        with self._lock:
            if self._model is None:
                return
            self._model = None
            self._tokenizers.clear()
            gc.collect()
            trim_memory()
            log.info("Modelo descargado de memoria")

    def transcribe(self, audio: np.ndarray, context: str = "") -> str:
        """Transcribe el audio. `context` es el texto dictado justo antes, si lo hay."""
        with self._lock:
            self._load()
            texts = []
            for chunk in self._speech_chunks(audio):
                if t := self._decode(chunk, " ".join([context, *texts])):
                    texts.append(t)
            self.last_used = time.monotonic()
        text = " ".join(texts).strip()
        if _HALLUCINATIONS.search(text) and len(text) < 80:
            log.info("Descartada alucinación típica: %r", text)
            return ""
        return text

    def find_cut(self, audio: np.ndarray, min_s: float = 10.0) -> int:
        """Busca la última pausa del audio para transcribir hasta ahí mientras se sigue grabando.

        Solo corta entre dos tramos de voz (así la frase anterior está completa) y nunca
        en el último segundo. Devuelve el número de muestras a transcribir, o 0.
        """
        if not self.cfg.vad or len(audio) < min_s * SAMPLE_RATE:
            return 0
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        stamps = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=500))
        limit = len(audio) - SAMPLE_RATE
        cut = 0
        for prev, nxt in itertools.pairwise(stamps):
            mid = (prev["end"] + nxt["start"]) // 2
            if mid <= limit:
                cut = mid
        return cut if cut >= min_s * SAMPLE_RATE else 0

    # --- internos ---------------------------------------------------------

    def _speech_chunks(self, audio: np.ndarray) -> list[np.ndarray]:
        if self.cfg.vad:
            from faster_whisper.vad import VadOptions, collect_chunks, get_speech_timestamps

            stamps = get_speech_timestamps(
                audio,
                VadOptions(min_silence_duration_ms=500, max_speech_duration_s=MAX_CHUNK_S),
            )
            if not stamps:
                return []
            chunks, _ = collect_chunks(audio, stamps, max_duration=MAX_CHUNK_S)
        else:
            step = int(MAX_CHUNK_S * SAMPLE_RATE)
            chunks = [audio[i : i + step] for i in range(0, len(audio), step)]
        return [c for c in chunks if c.size >= SAMPLE_RATE // 10]

    def _tokenizer(self, language: str):
        tok = self._tokenizers.get(language)
        if tok is None:
            from faster_whisper.tokenizer import Tokenizer
            from faster_whisper.transcribe import get_suppressed_tokens

            m = self._model
            tok = Tokenizer(m.hf_tokenizer, m.model.is_multilingual, task="transcribe", language=language)
            prompt = self.cfg.initial_prompt.strip()
            tok.vozi_base = tok.encode(" " + prompt) if prompt else []
            if self._suppress is None:
                self._suppress = get_suppressed_tokens(tok, [-1])
            self._tokenizers[language] = tok
        return tok

    def _decode(self, chunk: np.ndarray, context: str = "", allow_fallback: bool = True) -> str:
        feats = self._model.feature_extractor(chunk)
        n = min(feats.shape[1], MAX_FRAMES)
        frames = min(MAX_FRAMES, n + PAD_FRAMES)
        frames += frames % 2

        text, ok = self._generate(feats, n, frames, self.cfg.beam_size, context)
        if not ok and allow_fallback and frames < MAX_FRAMES:
            log.info("Resultado dudoso (%r); reintentando con ventana completa", text[:60])
            text, ok = self._generate(feats, n, MAX_FRAMES, max(self.cfg.beam_size, 5), context)
        return text

    def _generate(self, feats: np.ndarray, n: int, frames: int, beam: int, context: str) -> tuple[str, bool]:
        import ctranslate2

        m = self._model
        x = np.zeros((1, feats.shape[0], frames), dtype=np.float32)
        x[0, :, :n] = feats[:, :n]
        enc = m.model.encode(ctranslate2.StorageView.from_array(x), to_cpu=False)

        language = self.cfg.language
        if not language:
            language = m.model.detect_language(enc)[0][0][0][2:-2]
        tok = self._tokenizer(language)
        previous = tok.vozi_base
        if context.strip():  # las últimas palabras ya dictadas dan continuidad a la puntuación
            previous = previous + tok.encode(" " + context.strip())[-96:]
        prompt = m.get_prompt(tok, previous, without_timestamps=True)

        r = m.model.generate(
            enc,
            [prompt],
            beam_size=beam,
            patience=1,
            length_penalty=1,
            max_length=m.max_length,
            return_scores=True,
            return_no_speech_prob=True,
            suppress_blank=True,
            suppress_tokens=self._suppress,
        )[0]
        ids = [t for t in r.sequences_ids[0] if t < tok.eot]
        text = tok.decode(ids).strip()
        avg_logprob = r.scores[0] * len(ids) / (len(ids) + 1)

        if r.no_speech_prob > 0.6 and avg_logprob < -1.0:
            return "", True  # silencio o ruido: no hay nada que escribir
        repetitive = bool(text) and len(text) / len(zlib.compress(text.encode())) > 2.4
        return text, not repetitive and avg_logprob >= -1.0


def trim_memory() -> None:
    """Devuelve al sistema la memoria que glibc retiene tras una inferencia (~400 MB)."""
    try:
        import ctypes

        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass
