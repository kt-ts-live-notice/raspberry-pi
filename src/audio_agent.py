#!/usr/bin/env python3
"""Raspberry Pi audio capture/upload agent for the subway announcement PoC.

Contract implemented by this client:
- PCM 16 kHz, mono, 16-bit
- normal chunks: exactly 2 seconds (64,000 PCM bytes)
- final chunk: > 0 and <= 2 seconds
- POST multipart/form-data to /api/v1/audio-chunks
- parts: audio, session_id, chunk_index, is_final, device_id, recorded_at
- Bearer device authentication
- delete a queued chunk only after HTTP 202
- retry identical bytes/metadata after 0.5 s, 1 s, 2 s
- never skip an unacknowledged chunk
- manual session start/stop for PoC phase 1
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SERVER_URL = os.environ["SERVER_URL"]
DEVICE_ID = os.environ["DEVICE_ID"]
DEVICE_TOKEN = os.environ["DEVICE_TOKEN"]
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "default")
BASE_DIR = Path(os.getenv("BASE_DIR", "/home/pi/subway-audio"))

QUEUE_DIR = BASE_DIR / "queue"
CONTROL_DIR = BASE_DIR / "control"
RUNTIME_DIR = BASE_DIR / "runtime"
STATUS_FILE = RUNTIME_DIR / "status.json"

SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2
CHUNK_SECONDS = 2
PCM_BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH_BYTES
NORMAL_CHUNK_PCM_BYTES = PCM_BYTES_PER_SECOND * CHUNK_SECONDS  # 64,000
READ_BLOCK_BYTES = PCM_BYTES_PER_SECOND // 10  # 100 ms = 3,200 bytes
MAX_AUDIO_BYTES = 128 * 1024
RETRY_DELAYS = (0.5, 1.0, 2.0)
RETRY_CYCLE_PAUSE = 5.0

stop_event = threading.Event()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------
def utc_rfc3339_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def set_status(
    state: str,
    session_id: str | None = None,
    next_chunk_index: int | None = None,
    detail: str | None = None,
) -> None:
    data: dict[str, object] = {
        "state": state,
        "device_id": DEVICE_ID,
        "updated_at": utc_rfc3339_now(),
    }
    if session_id is not None:
        data["session_id"] = session_id
    if next_chunk_index is not None:
        data["next_chunk_index"] = next_chunk_index
    if detail is not None:
        data["detail"] = detail
    atomic_write_json(STATUS_FILE, data)


def consume_control_flag(name: str) -> bool:
    path = CONTROL_DIR / name
    if not path.exists():
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def ensure_directories() -> None:
    for directory in (QUEUE_DIR, CONTROL_DIR, RUNTIME_DIR):
        directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# WAV / queue persistence
# ---------------------------------------------------------------------------
def write_wav(path: Path, pcm_bytes: bytes) -> None:
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(pcm_bytes)


def validate_wav(path: Path, *, is_final: bool) -> tuple[int, float]:
    with wave.open(str(path), "rb") as wav_file:
        if wav_file.getnchannels() != CHANNELS:
            raise ValueError("WAV must be mono")
        if wav_file.getsampwidth() != SAMPLE_WIDTH_BYTES:
            raise ValueError("WAV must be 16-bit PCM")
        if wav_file.getframerate() != SAMPLE_RATE:
            raise ValueError("WAV must be 16 kHz")
        if wav_file.getcomptype() != "NONE":
            raise ValueError("WAV must be uncompressed PCM")
        frame_count = wav_file.getnframes()

    pcm_bytes = frame_count * CHANNELS * SAMPLE_WIDTH_BYTES
    duration = frame_count / SAMPLE_RATE if frame_count else 0.0

    if is_final:
        if not (0 < pcm_bytes <= NORMAL_CHUNK_PCM_BYTES):
            raise ValueError("final chunk must be >0 and <=2 seconds")
    else:
        if pcm_bytes != NORMAL_CHUNK_PCM_BYTES:
            raise ValueError(
                f"normal chunk must be exactly {NORMAL_CHUNK_PCM_BYTES} PCM bytes"
            )

    if path.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError("WAV exceeds 128 KiB")

    return pcm_bytes, duration


def persist_chunk(
    *,
    session_id: str,
    chunk_index: int,
    pcm_bytes: bytes,
    is_final: bool,
    recorded_at: str,
) -> None:
    basename = f"{session_id}__{chunk_index:06d}"
    wav_path = QUEUE_DIR / f"{basename}.wav"
    json_path = QUEUE_DIR / f"{basename}.json"
    wav_tmp = QUEUE_DIR / f".{basename}.wav.tmp"

    write_wav(wav_tmp, pcm_bytes)
    os.replace(wav_tmp, wav_path)
    _, duration = validate_wav(wav_path, is_final=is_final)

    metadata = {
        "session_id": session_id,
        "chunk_index": chunk_index,
        "is_final": is_final,
        "device_id": DEVICE_ID,
        "recorded_at": recorded_at,
        "wav_filename": wav_path.name,
        "queued_at": utc_rfc3339_now(),
    }
    atomic_write_json(json_path, metadata)

    logging.info(
        "queued session=%s index=%d final=%s duration=%.3fs",
        session_id,
        chunk_index,
        is_final,
        duration,
    )


# ---------------------------------------------------------------------------
# Upload logic
# ---------------------------------------------------------------------------
def pending_metadata_files() -> list[Path]:
    records: list[tuple[str, int, Path]] = []
    for json_path in QUEUE_DIR.glob("*.json"):
        try:
            with json_path.open("r", encoding="utf-8") as fp:
                metadata = json.load(fp)
            records.append(
                (
                    str(metadata.get("queued_at", "")),
                    int(metadata["chunk_index"]),
                    json_path,
                )
            )
        except Exception as exc:  # keep broken evidence; do not delete it automatically
            logging.error("failed to read queue metadata %s: %s", json_path, exc)

    records.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in records]


def send_once(wav_path: Path, metadata: dict) -> requests.Response:
    with wav_path.open("rb") as audio_file:
        return requests.post(
            SERVER_URL,
            headers={"Authorization": f"Bearer {DEVICE_TOKEN}"},
            files={
                "audio": (
                    wav_path.name,
                    audio_file,
                    "audio/wav",
                )
            },
            data={
                "session_id": metadata["session_id"],
                "chunk_index": str(metadata["chunk_index"]),
                "is_final": "true" if metadata["is_final"] else "false",
                "device_id": metadata["device_id"],
                "recorded_at": metadata["recorded_at"],
            },
            timeout=(5, 15),
        )


def upload_chunk(json_path: Path) -> bool:
    with json_path.open("r", encoding="utf-8") as fp:
        metadata = json.load(fp)

    wav_path = QUEUE_DIR / metadata["wav_filename"]
    if not wav_path.exists():
        logging.error("missing WAV for %s", json_path.name)
        return False

    # Initial request + three retries at 0.5 / 1 / 2 seconds.
    schedule = (0.0, *RETRY_DELAYS)
    for attempt, delay in enumerate(schedule, start=1):
        if delay and stop_event.wait(delay):
            return False

        logging.info(
            "upload session=%s index=%d attempt=%d",
            metadata["session_id"],
            metadata["chunk_index"],
            attempt,
        )

        try:
            response = send_once(wav_path, metadata)
        except requests.RequestException as exc:
            logging.warning("upload connection error: %s", exc)
            continue

        if response.status_code == 202:
            try:
                body = response.json()
            except ValueError:
                body = {}

            logging.info(
                "202 accepted session=%s index=%d duplicate=%s finalized=%s",
                metadata["session_id"],
                metadata["chunk_index"],
                body.get("is_duplicate", "?"),
                body.get("finalized", "?"),
            )

            # Delete only after the server ACKs the exact chunk.
            wav_path.unlink(missing_ok=True)
            json_path.unlink(missing_ok=True)
            return True

        if response.status_code == 409:
            try:
                problem = response.json()
            except ValueError:
                problem = {}
            logging.error(
                "409 sequence conflict: sent=%d expected=%s body=%s",
                metadata["chunk_index"],
                problem.get("expected_chunk_index", "unknown"),
                response.text[:500],
            )
            # Do not regenerate, skip or delete the chunk.
            return False

        if response.status_code == 429 or response.status_code >= 500:
            logging.warning(
                "temporary server error HTTP %d: %s",
                response.status_code,
                response.text[:300],
            )
            continue

        # 400/401/413/415/422 etc. normally require configuration/data fixes.
        logging.error(
            "upload rejected HTTP %d: %s",
            response.status_code,
            response.text[:500],
        )
        return False

    logging.warning(
        "retry burst exhausted; keeping session=%s index=%d in queue",
        metadata["session_id"],
        metadata["chunk_index"],
    )
    return False


def uploader_loop() -> None:
    while not stop_event.is_set():
        pending = pending_metadata_files()
        if not pending:
            stop_event.wait(0.5)
            continue

        # Strict queue order: if one unacknowledged chunk fails, do not skip it.
        for json_path in pending:
            if stop_event.is_set():
                return
            if not upload_chunk(json_path):
                stop_event.wait(RETRY_CYCLE_PAUSE)
                break


# ---------------------------------------------------------------------------
# Recording session
# ---------------------------------------------------------------------------
class RecordingSession:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.next_chunk_index = 0
        self.process: subprocess.Popen[bytes] | None = None
        self.reader_thread: threading.Thread | None = None
        self.session_stop = threading.Event()
        self.session_done = threading.Event()
        self.error: str | None = None

    @property
    def active(self) -> bool:
        return self.process is not None and not self.session_done.is_set()

    def _arecord_command(self) -> list[str]:
        return [
            "arecord",
            "-q",
            "-D",
            AUDIO_DEVICE,
            "-t",
            "raw",
            "-f",
            "S16_LE",
            "-r",
            str(SAMPLE_RATE),
            "-c",
            str(CHANNELS),
        ]

    def start(self) -> None:
        if self.active:
            logging.warning("recording session already active")
            return

        self.session_id = str(uuid.uuid4())
        self.next_chunk_index = 0
        self.session_stop.clear()
        self.session_done.clear()
        self.error = None

        self.process = subprocess.Popen(
            self._arecord_command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        if self.process.stdout is None:
            raise RuntimeError("arecord stdout pipe was not created")

        self.reader_thread = threading.Thread(
            target=self._capture_loop,
            args=(self.process.stdout,),
            name="audio-capture",
            daemon=True,
        )
        self.reader_thread.start()

        set_status("recording", self.session_id, self.next_chunk_index)
        logging.info("session started: %s", self.session_id)

    def _capture_loop(self, audio_stream: BinaryIO) -> None:
        assert self.session_id is not None
        session_id = self.session_id

        # Keep the newest complete 2-second chunk in memory until we know the
        # session continues. This lets an operator stop exactly on a 2-second
        # boundary and still mark that complete chunk is_final=true.
        pending_full_chunk: tuple[bytes, str] | None = None
        chunk_buffer = bytearray()
        chunk_recorded_at: str | None = None

        try:
            while not self.session_stop.is_set() and not stop_event.is_set():
                block = audio_stream.read(READ_BLOCK_BYTES)
                if not block:
                    break

                # Receiving bytes after a held full chunk proves the previous
                # chunk was not final, so it can now be queued as non-final.
                if pending_full_chunk is not None:
                    pcm, recorded_at = pending_full_chunk
                    persist_chunk(
                        session_id=session_id,
                        chunk_index=self.next_chunk_index,
                        pcm_bytes=pcm,
                        is_final=False,
                        recorded_at=recorded_at,
                    )
                    self.next_chunk_index += 1
                    set_status("recording", session_id, self.next_chunk_index)
                    pending_full_chunk = None

                if chunk_recorded_at is None:
                    chunk_recorded_at = utc_rfc3339_now()

                chunk_buffer.extend(block)

                if len(chunk_buffer) >= NORMAL_CHUNK_PCM_BYTES:
                    full_chunk = bytes(chunk_buffer[:NORMAL_CHUNK_PCM_BYTES])
                    del chunk_buffer[:NORMAL_CHUNK_PCM_BYTES]
                    pending_full_chunk = (
                        full_chunk,
                        chunk_recorded_at or utc_rfc3339_now(),
                    )
                    chunk_recorded_at = utc_rfc3339_now() if chunk_buffer else None

            if self.session_stop.is_set():
                # If there is a partial tail, the held complete chunk is
                # definitely non-final and the tail is final.
                if chunk_buffer:
                    if pending_full_chunk is not None:
                        pcm, recorded_at = pending_full_chunk
                        persist_chunk(
                            session_id=session_id,
                            chunk_index=self.next_chunk_index,
                            pcm_bytes=pcm,
                            is_final=False,
                            recorded_at=recorded_at,
                        )
                        self.next_chunk_index += 1
                        pending_full_chunk = None

                    persist_chunk(
                        session_id=session_id,
                        chunk_index=self.next_chunk_index,
                        pcm_bytes=bytes(chunk_buffer),
                        is_final=True,
                        recorded_at=chunk_recorded_at or utc_rfc3339_now(),
                    )
                    self.next_chunk_index += 1

                # Exact 2-second boundary: the held full chunk itself is final.
                elif pending_full_chunk is not None:
                    pcm, recorded_at = pending_full_chunk
                    persist_chunk(
                        session_id=session_id,
                        chunk_index=self.next_chunk_index,
                        pcm_bytes=pcm,
                        is_final=True,
                        recorded_at=recorded_at,
                    )
                    self.next_chunk_index += 1

        except Exception as exc:
            self.error = str(exc)
            logging.exception("capture loop failed")
        finally:
            self.session_done.set()

    def stop(self) -> None:
        if not self.active:
            logging.warning("no active recording session")
            return

        assert self.process is not None
        assert self.reader_thread is not None
        assert self.session_id is not None

        session_id = self.session_id
        logging.info("manual stop requested: %s", session_id)

        self.session_stop.set()
        self.reader_thread.join(timeout=1.0)

        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

        self.reader_thread.join(timeout=2.0)
        self.session_done.set()

        if self.error:
            set_status("error", session_id, self.next_chunk_index, self.error)
        else:
            set_status("idle")

        logging.info("session stopped: %s", session_id)
        self.process = None
        self.reader_thread = None
        self.session_id = None

    def abort_for_service_shutdown(self) -> None:
        if self.process is None:
            return

        logging.warning(
            "service shutdown while recording; stopping without declaring is_final"
        )
        self.session_stop.clear()  # service shutdown is not an operator-finalized session
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Main service loop
# ---------------------------------------------------------------------------
def handle_signal(signum, frame) -> None:  # noqa: ARG001
    logging.info("service termination signal received")
    stop_event.set()


def main() -> None:
    ensure_directories()
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    set_status("idle")

    uploader = threading.Thread(
        target=uploader_loop,
        name="audio-uploader",
        daemon=True,
    )
    uploader.start()

    recorder = RecordingSession()

    try:
        while not stop_event.is_set():
            if consume_control_flag("start"):
                recorder.start()

            if consume_control_flag("stop"):
                recorder.stop()

            # Detect an unexpected arecord exit.
            if recorder.process is not None and not recorder.session_stop.is_set():
                return_code = recorder.process.poll()
                if return_code is not None:
                    stderr = b""
                    if recorder.process.stderr is not None:
                        stderr = recorder.process.stderr.read()
                    raise RuntimeError(
                        f"arecord exited unexpectedly code={return_code}: "
                        f"{stderr.decode(errors='replace').strip()}"
                    )

            stop_event.wait(0.1)
    finally:
        recorder.abort_for_service_shutdown()
        stop_event.set()
        uploader.join(timeout=5)
        set_status("stopped")


if __name__ == "__main__":
    main()
