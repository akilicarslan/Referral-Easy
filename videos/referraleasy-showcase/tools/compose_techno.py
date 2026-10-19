"""Compose an original 128 BPM techno bed with steady instrumentation and level."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfilt

ROOT = Path(__file__).resolve().parents[1]
SR = 48_000
SECONDS = 60
BPM = 128
BEAT = 60 / BPM
BAR = BEAT * 4
N = SR * SECONDS
RNG = np.random.default_rng(12837)


def filt(samples: np.ndarray, cutoff: float | tuple[float, float], kind: str) -> np.ndarray:
    return sosfilt(butter(3, cutoff, btype=kind, fs=SR, output="sos"), samples)


def midi(note: int) -> float:
    return 440 * 2 ** ((note - 69) / 12)


def finish(samples: np.ndarray, attack: float = 0.002, release: float = 0.008) -> np.ndarray:
    samples = samples.copy()
    a, r = min(len(samples), int(attack * SR)), min(len(samples), int(release * SR))
    samples[:a] *= np.linspace(0, 1, a)
    samples[-r:] *= np.linspace(1, 0, r)
    return samples


def add(mix: np.ndarray, sound: np.ndarray, at: float, gain: float, pan: float = 0) -> None:
    start = round(at * SR)
    if start >= len(mix):
        return
    length = min(len(sound), len(mix) - start)
    # Equal-power pan. No speech input or gain automation enters the music bus.
    mix[start : start + length, 0] += sound[:length] * gain * np.sqrt((1 - pan) / 2)
    mix[start : start + length, 1] += sound[:length] * gain * np.sqrt((1 + pan) / 2)


def kick() -> np.ndarray:
    t = np.arange(round(0.34 * SR)) / SR
    pitch = 49 + 112 * np.exp(-t * 40)
    body = np.sin(2 * np.pi * np.cumsum(pitch) / SR) * np.exp(-t * 14)
    transient = filt(RNG.normal(size=len(t)), 3600, "lowpass") * np.exp(-t * 180) * 0.13
    return finish(np.tanh(body * 1.45) * 0.72 + transient, 0.0007, 0.012)


def hat(opened: bool) -> np.ndarray:
    length = 0.15 if opened else 0.045
    t = np.arange(round(length * SR)) / SR
    noise = filt(RNG.normal(size=len(t)), (6500, 15000), "bandpass")
    metal = sum(np.sin(2 * np.pi * freq * t) for freq in (7319, 9163, 11261)) / 6
    return finish((noise * 0.75 + metal * 0.17) * np.exp(-t * (26 if opened else 105)))


def clap() -> np.ndarray:
    t = np.arange(round(0.13 * SR)) / SR
    noise = filt(RNG.normal(size=len(t)), (1200, 6500), "bandpass")
    envelope = np.exp(-t * 32) * 0.25
    for offset in (0, 0.011, 0.023):
        envelope += np.where(t >= offset, np.exp(-np.maximum(t - offset, 0) * 200), 0) * 0.24
    return finish(noise * envelope)


def bass(note: int, duration: float) -> np.ndarray:
    t = np.arange(round(duration * SR)) / SR
    frequency = midi(note)
    phase = 2 * np.pi * frequency * t
    tone = np.sin(phase + 0.8 * np.sin(phase * 2) * np.exp(-t * 20)) * 0.65
    tone += np.sin(phase / 2) * 0.22
    tone += np.sin(phase * 2) * 0.13 + np.sin(phase * 3) * 0.07
    return finish(filt(np.tanh(tone * 1.3), 1150, "lowpass") * np.exp(-t * 5), 0.006, 0.014)


def pluck(note: int) -> np.ndarray:
    t = np.arange(round(0.26 * SR)) / SR
    phase = 2 * np.pi * midi(note) * t
    # Glass-like FM transient and a short tail, kept behind the narrator's range.
    tone = np.sin(phase + 0.65 * np.sin(phase * 2.003) * np.exp(-t * 32))
    tone += 0.19 * np.sin(phase * 1.5)
    return finish(filt(tone * np.exp(-t * 24), 5400, "lowpass"), 0.003, 0.012)


def main() -> None:
    mix = np.zeros((N, 2), dtype=np.float32)
    drums = {"kick": kick(), "hat": hat(False), "open": hat(True), "clap": clap()}
    pattern = [0, None, 0, 12, None, 0, 7, None, 0, None, 3, 0, None, 7, 10, 0]
    arpeggio = [73, 80, 75, 83, 76, 80, 75, 87]
    roots = [37, 37, 37, 37, 40, 40, 35, 35]
    bass_cache: dict[int, np.ndarray] = {}
    pluck_cache = {p: pluck(p) for p in arpeggio}

    # Thirty-two complete bars fill sixty seconds. Every bar has the same groove.
    for bar in range(32):
        start = bar * BAR
        root = roots[bar % len(roots)]
        for beat in range(4):
            add(mix, drums["kick"], start + beat * BEAT, 0.78)
            if beat in (1, 3):
                add(mix, drums["clap"], start + beat * BEAT, 0.18, 0.08)
            add(mix, drums["open"], start + (beat + 0.5) * BEAT, 0.085, 0.22)
        for step, interval in enumerate(pattern):
            at = start + step * BEAT / 4
            add(mix, drums["hat"], at, 0.046 if step % 2 else 0.061, -0.22 if step % 2 else 0.12)
            if interval is not None:
                pitch = root + interval
                if pitch not in bass_cache:
                    bass_cache[pitch] = bass(pitch, BEAT * 0.43)
                add(mix, bass_cache[pitch], at, 0.49)
            if step % 2 == 0:
                p = arpeggio[(step // 2 + (bar % 4) * 2) % len(arpeggio)]
                pan = -0.48 if step % 4 == 0 else 0.48
                add(mix, pluck_cache[p], at + BEAT / 8, 0.076, pan)
                add(mix, pluck_cache[p], at + BEAT * 0.875, 0.022, -pan)

    # A continuous, quiet wide synth halo. No build, breakdown, swell or sidechain.
    t = np.arange(N, dtype=np.float64) / SR
    for i, p in enumerate((49, 56, 59, 63)):
        frequency = midi(p)
        for channel, detune in ((0, -0.0009), (1, 0.0009)):
            phase = 2 * np.pi * frequency * (1 + detune) * t + i
            pad = (np.sin(phase) + 0.14 * np.sin(phase * 2)) * 0.016
            mix[:, channel] += pad.astype(np.float32)
    mix = np.tanh(mix * 0.85)
    # Only a 6 ms de-click at file boundaries, not a musical fade.
    edge = round(0.006 * SR)
    mix[:edge] *= np.linspace(0, 1, edge)[:, None]
    mix[-edge:] *= np.linspace(1, 0, edge)[:, None]
    cache = ROOT / ".cache"
    out = ROOT / "public" / "audio"
    cache.mkdir(exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    raw = cache / "techno-raw.wav"
    sf.write(raw, mix, SR, subtype="FLOAT")
    # Measure once, then apply one constant gain to the whole track.
    measure = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(raw),
            "-af",
            "loudnorm=I=-18:TP=-4:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    stats, _ = json.JSONDecoder().raw_decode(measure.stderr[measure.stderr.rfind("{") :])
    gain_db = -18 - float(stats["input_i"])
    gain_db = min(gain_db, -4 - float(stats["input_tp"]))
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(raw),
            "-af",
            f"volume={gain_db}dB",
            "-c:a",
            "pcm_s24le",
            str(out / "techno-score.wav"),
        ],
        check=True,
    )
    print(
        json.dumps(
            {
                "score": "Signal Path",
                "bpm": BPM,
                "duration_seconds": SECONDS,
                "music_gain_normalization_db": gain_db,
                "dynamic_processing": False,
                "ducking": False,
                "instrumentation": "four-on-the-floor kick, sub bass, FM bass, glass arpeggio, synth halo",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
