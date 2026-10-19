"""Generate the local male voice, fit each cue, and retain model provenance."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("HF_HOME", str(ROOT / ".cache" / "huggingface"))
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")


def main() -> None:
    # Set the model cache environment before loading the audio libraries.
    import numpy as np
    import soundfile as sf
    import torch
    from pocket_tts import TTSModel

    torch.set_num_threads(4)
    torch.manual_seed(27)
    out = ROOT / "public" / "audio"
    out.mkdir(parents=True, exist_ok=True)
    raw = ROOT / ".cache" / "voice"
    raw.mkdir(parents=True, exist_ok=True)
    model = TTSModel.load_model(language="english_2026-09", temp=0.3)
    voice = model.get_state_for_audio_prompt("george")
    cues = json.loads((ROOT / "script.json").read_text())
    sr = model.sample_rate
    timeline = np.zeros(60 * sr, dtype=np.float32)
    report = []
    for cue in cues:
        source = raw / f"{cue['id']}.wav"
        text_hash = hashlib.sha256(cue["text"].encode()).hexdigest()
        stamp = source.with_suffix(".sha256")
        if not source.exists() or not stamp.exists() or stamp.read_text() != text_hash:
            samples = model.generate_audio(voice, cue["text"]).detach().cpu().numpy()
            sf.write(source, samples, sr)
            stamp.write_text(text_hash)
        samples, rate = sf.read(source, dtype="float32")
        active = np.flatnonzero(np.abs(samples) > 0.008)
        if active.size:
            samples = samples[
                max(0, active[0] - int(0.07 * rate)) : min(
                    len(samples), active[-1] + int(0.15 * rate)
                )
            ]
        sf.write(source.with_name(f"{cue['id']}-trim.wav"), samples, rate)
        available = cue["end"] - cue["start"] - 0.6
        duration = len(samples) / rate
        speed = max(1.0, duration / available)
        if speed > 1.15:
            raise RuntimeError(
                f"Cue {cue['id']} requires excessive speed {speed:.2f}. Shorten the script."
            )
        target = out / f"{cue['id']}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(source.with_name(f"{cue['id']}-trim.wav")),
                "-af",
                f"atempo={speed:.6f},highpass=f=70,lowpass=f=10500,loudnorm=I=-17:TP=-2:LRA=6",
                "-ar",
                str(sr),
                str(target),
            ],
            check=True,
        )
        fitted, _ = sf.read(target, dtype="float32")
        at = int((cue["start"] + 0.3) * sr)
        end = at + len(fitted)
        if end > int(cue["end"] * sr):
            raise RuntimeError(f"Narration crosses the {cue['id']} boundary")
        timeline[at:end] += fitted
        report.append(
            {
                **cue,
                "voice_start": at / sr,
                "voice_end": end / sr,
                "source_duration": duration,
                "tempo": speed,
            }
        )
        print(f"{cue['id']}: {len(fitted) / sr:.2f}s, tempo {speed:.3f}", flush=True)
    sf.write(out / "narration.wav", timeline, sr, subtype="PCM_24")
    (ROOT / "public" / "narration-timing.json").write_text(json.dumps(report, indent=2) + "\n")
    weights = [
        {
            "path": str(p.relative_to(ROOT)),
            "bytes": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in (ROOT / ".cache").rglob("*.safetensors")
        if p.is_file()
    ]
    # Hugging Face snapshot symlinks may point to the same blob. Count unique hashes.
    total = sum({x["sha256"]: x["bytes"] for x in weights}.values())
    if total >= 3_000_000_000:
        raise RuntimeError(f"Model exceeds 3 GB: {total}")
    (ROOT / "voice-provenance.json").write_text(
        json.dumps(
            {
                "engine": "pocket-tts",
                "version": "3.3.0",
                "language": "english_2026-09",
                "voice": "george",
                "sample_rate": sr,
                "unique_weight_bytes": total,
                "weights": weights,
                "timing": report,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
