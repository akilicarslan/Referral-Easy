"""Mix the techno bed at one fixed low gain for the entire film."""

import json
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

root = Path(__file__).resolve().parents[1]
audio = root / "public" / "audio"
MUSIC_GAIN = 0.20
NARRATION_GAIN = 0.90
score, sr = sf.read(audio / "techno-score.wav", dtype="float32", always_2d=True)
speech, vsr = sf.read(audio / "narration.wav", dtype="float32")
voice = resample_poly(speech, sr, vsr)
voice = voice[: len(score)]
mix = score * MUSIC_GAIN + voice[:, None] * NARRATION_GAIN
peak = float(np.abs(mix).max())
if peak >= 0.99:
    raise RuntimeError(f"Mix clips: peak {peak}")
sf.write(audio / "mix-preview.wav", mix, sr, subtype="PCM_24")
report = {
    "duration_seconds": len(mix) / sr,
    "sample_rate": sr,
    "channels": 2,
    "sample_peak_dbfs": float(20 * np.log10(peak)),
    "background_gain": MUSIC_GAIN,
    "background_gain_db": float(20 * np.log10(MUSIC_GAIN)),
    "gain_automation": False,
    "ducking": False,
    "narration_gain": NARRATION_GAIN,
    "score": "Signal Path — original futuristic techno, 128 BPM",
    "instrument_source": "Original deterministic DSP synthesis",
    "source_recordings_or_melodies_reused": False,
}
(root / "audio-report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
