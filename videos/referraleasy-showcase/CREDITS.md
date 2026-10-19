# Audio sources and model research

Research date: 2026-10-19.

Narration uses **Kyutai Pocket TTS 3.3.0**, released 2026-09-24, with the September 2026 English
checkpoint and the built-in male voice **George**. Inference ran on this arm64 Mac CPU.
The actual selected weight files total **225,273,348 bytes (225.3 MB / 214.8 MiB)**, including the
voice embedding. This is a model storage measurement, not a peak RAM claim. The isolated Python
environment occupies about 783 MB; it is excluded from Git. Model hashes and pinned snapshot paths
are in `voice-provenance.json`.

The software is MIT-licensed. The released model weights are CC BY 4.0. The ungated
`pocket-tts-without-voice-cloning` checkpoint was used with a released voice embedding.
No account agreement or custom voice cloning was needed.

- [Current release, v3.3.0](https://github.com/kyutai-labs/pocket-tts/releases/tag/v3.3.0)
- [Software and CPU workflow](https://github.com/kyutai-labs/pocket-tts)
- [Model and CC BY 4.0 terms](https://huggingface.co/kyutai/pocket-tts-without-voice-cloning)
- [Voice sources and licenses](https://huggingface.co/kyutai/tts-voices)
- [George source: VCTK p315, sentence 23](https://huggingface.co/kyutai/tts-voices/blob/main/vctk/p315_023_enhanced.wav)

Model attribution: Manu Orsini, Simon Rouard, Gabriel De Marmiesse, Václav Volhejn, Neil Zeghidour
and Alexandre Défossez / Kyutai. Speech is synthesized from the supplied showcase script. Cues were
trimmed, time-fitted, filtered and normalized. They are not recordings of a person reading this text.

George is based on the CSTR VCTK Corpus, CC BY 4.0, by Junichi Yamagishi, Christophe Veaux and
Kirsten MacDonald, University of Edinburgh. The source recording was enhanced in Kyutai's voice
collection. See the [VCTK corpus](https://datashare.ed.ac.uk/handle/10283/3443) and
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Keep this attribution with shared assets.

Other recent small candidates checked:

| Model | Finding |
| --- | --- |
| [KittenTTS Mini 0.8](https://huggingface.co/KittenML/kitten-tts-mini-0.8) | 80M parameters, about 79 MB, Apache 2.0; compact alternative. |
| [KittenTTS 2](https://github.com/KittenML/KittenTTS) | Newer family, 506–947 MiB weights, but the model uses a separate community license. |
| [MOSS-TTS-Nano](https://github.com/OpenMOSS/MOSS-TTS-Nano) | Recent 100M CPU model; requires a different voice-conditioning workflow. |

Pocket TTS was selected as a current, maintained, small model with a verified September English
checkpoint and ready-to-use male voice. This is the latest verified Pocket TTS release on the
research date. It is not a claim that no newer TTS model exists anywhere.

**Music: “Signal Path”** — original 60-second futuristic techno composition authored for this
showcase. 128 BPM, 32 bars. Synthesized locally from deterministic DSP: a four-on-the-floor kick,
sub bass, FM bass, metallic arpeggios, hats, claps and a wide synth halo. No external music samples,
recordings or melodies are reused. Source: `tools/compose_techno.py`.

The track has one constant normalization gain, then a fixed 0.20 music gain throughout the film.
Narration stays at 0.90. There is no speech ducking, sidechain, volume automation, build or breakdown.
Only a 6 ms de-click at the file boundaries prevents clicks. Both stems remain editable in Remotion.
`audio-report.json` records the current mix measurements. The earlier orchestral source remains in
`tools/compose_score.swift`; the composition no longer uses it.
