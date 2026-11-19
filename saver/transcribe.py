"""Whisper transcription with faster-whisper (CTranslate2).

Writes the sidecar files out.srt, out.vtt, out.txt and out.json
(insanely-fast-whisper's {"speakers", "chunks", "text"} shape), which
subtitle embedding, the watch page and the offload rely on.

Only one transcription runs at a time (GPU semaphore), and the model is
dropped after WHISPER_UNLOAD_AFTER idle seconds so other GPU users (e.g.
Ollama) get the memory back.
"""
import ctypes
import gc
import glob
import importlib
import json
import os
import threading
import time
from pathlib import Path

from .config import settings
from .jobctx import check_cancelled, log, progress

_gpu = threading.Semaphore(1)
_state_lock = threading.Lock()
_model = None          # (WhisperModel, BatchedInferencePipeline)
_model_key = None
_last_used = 0.0
_reaper_started = False


def format_timestamp(seconds: float, separator: str = ",") -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def write_outputs(out_dir: Path, segments, language=None) -> Path:
    """segments: iterable of (start, end, text). Returns the .srt path."""
    segments = [(s, e, t.strip()) for s, e, t in segments if t and t.strip()]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    srt = "\n".join(f"{i}\n{format_timestamp(s)} --> {format_timestamp(e)}\n{t}\n"
                    for i, (s, e, t) in enumerate(segments, 1))
    vtt = "WEBVTT\n\n" + "\n".join(f"{format_timestamp(s, '.')} --> {format_timestamp(e, '.')}\n{t}\n"
                                   for s, e, t in segments)
    (out_dir / "out.srt").write_text(srt, encoding="utf-8")
    (out_dir / "out.vtt").write_text(vtt, encoding="utf-8")
    (out_dir / "out.txt").write_text("\n".join(t for _, _, t in segments) + "\n", encoding="utf-8")
    (out_dir / "out.json").write_text(json.dumps({
        "speakers": [],
        "chunks": [{"timestamp": [round(s, 2), round(e, 2)], "text": t} for s, e, t in segments],
        "text": " ".join(t for _, _, t in segments),
        "language": language,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_dir / "out.srt"


_cuda_preloaded = False


def _preload_cuda_libraries():
    """CTranslate2 dlopen()s libcublas.so.12 / libcudnn*.so.9 by name, but
    the nvidia-cublas-cu12 / nvidia-cudnn-cu12 pip wheels install them under
    site-packages where the loader doesn't look. Loading them RTLD_GLOBAL
    first makes those by-name lookups resolve, so neither LD_LIBRARY_PATH
    nor a CUDA base image is required. Several passes, because the cuDNN
    sub-libraries depend on each other in an order glob doesn't know."""
    global _cuda_preloaded
    if _cuda_preloaded:
        return
    _cuda_preloaded = True
    pending = []
    for package in ("nvidia.cuda_runtime", "nvidia.cuda_nvrtc", "nvidia.cublas", "nvidia.cudnn"):
        try:
            module = importlib.import_module(package)
        except ImportError:
            continue
        for base in module.__path__:
            pending += sorted(glob.glob(os.path.join(base, "lib", "*.so*")))
    for _ in range(4):
        failed = []
        for lib in pending:
            try:
                ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
            except OSError:
                failed.append(lib)
        if not failed or len(failed) == len(pending):
            break
        pending = failed


def _resolve_device() -> str:
    if settings.whisper_device != "auto":
        return settings.whisper_device
    _preload_cuda_libraries()
    try:
        import ctranslate2
        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def _compute_type(device: str) -> str:
    if settings.whisper_compute_type != "auto":
        return settings.whisper_compute_type
    return "float16" if device == "cuda" else "int8"


def _load_model():
    global _model, _model_key, _reaper_started
    _preload_cuda_libraries()
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    device = _resolve_device()
    compute_type = _compute_type(device)
    key = (settings.whisper_model, device, compute_type)
    if _model is None or _model_key != key:
        _model = None
        gc.collect()
        log(f"Loading Whisper {settings.whisper_model} on {device} ({compute_type}) ...")
        model = WhisperModel(settings.whisper_model, device=device, compute_type=compute_type,
                             download_root=str(settings.whisper_model_dir))
        _model = (model, BatchedInferencePipeline(model=model))
        _model_key = key
    if not _reaper_started and settings.whisper_unload_after > 0:
        threading.Thread(target=_reaper, name="whisper-reaper", daemon=True).start()
        _reaper_started = True
    return _model


def _reaper():
    global _model, _model_key
    while True:
        time.sleep(30)
        if _model is None or time.monotonic() - _last_used < settings.whisper_unload_after:
            continue
        if not _gpu.acquire(blocking=False):
            continue  # a transcription is running
        try:
            with _state_lock:
                if _model is not None and time.monotonic() - _last_used >= settings.whisper_unload_after:
                    _model, _model_key = None, None
                    gc.collect()
                    log("Unloaded idle Whisper model to free GPU memory")
        finally:
            _gpu.release()


def unload():
    global _model, _model_key
    with _gpu, _state_lock:
        _model, _model_key = None, None
        gc.collect()


def status() -> dict:
    return {
        "enabled": settings.whisper_enabled,
        "model": settings.whisper_model,
        "loaded": _model is not None,
        "device": _model_key[1] if _model_key else None,
        "busy": _gpu._value == 0,  # noqa: SLF001 - Semaphore has no public "locked" for this
    }


SAMPLE_RATE = 16000


def load_audio(media_path: Path):
    """Decode to 16 kHz mono float32 with ffmpeg. faster-whisper's own
    decoder goes through PyAV, whose API it does not pin (av>=11): PyAV 19
    dropped the `metadata_errors` argument it passes, and every
    transcription failed. ffmpeg is needed for subtitle muxing anyway."""
    import numpy as np

    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(media_path),
           "-vn", "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    from . import proc

    decoded = proc.run(cmd, text=False)
    if decoded.returncode != 0 or not decoded.stdout:
        raise RuntimeError("ffmpeg could not decode audio: "
                           + decoded.stderr.decode(errors="replace").strip()[-500:])
    return np.frombuffer(decoded.stdout, np.int16).astype(np.float32) / 32768.0


def transcribe(media_path: Path, out_dir: Path) -> Path:
    """Transcribe media_path, writing out.* into out_dir. Returns out.srt."""
    global _last_used
    progress(None, "waiting for GPU")
    with _gpu:
        with _state_lock:
            model, batched = _load_model()
        try:
            progress(None, "decoding audio")
            audio = load_audio(media_path)
            if settings.whisper_batch_size > 1:
                # without_timestamps=False: real segment boundaries instead of
                # one cue per ~30s VAD chunk - readable subtitles, and finer
                # timestamps for deeplinks into the transcript.
                segments_iter, info = batched.transcribe(audio, batch_size=settings.whisper_batch_size,
                                                         without_timestamps=False)
            else:
                segments_iter, info = model.transcribe(audio, vad_filter=True)
            duration = info.duration or 0
            log(f"Detected language: {info.language} ({info.language_probability:.0%}), "
                f"duration {_clock(duration)}")
            segments = []
            for segment in segments_iter:
                check_cancelled()
                segments.append((segment.start, segment.end, segment.text))
                if duration:
                    progress(min(segment.end / duration, 1.0), f"{_clock(segment.end)} / {_clock(duration)}")
        finally:
            _last_used = time.monotonic()
    return write_outputs(out_dir, segments, language=info.language)
