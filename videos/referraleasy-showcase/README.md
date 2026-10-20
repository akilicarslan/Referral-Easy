# ReferralEasy cinematic showcase

A 60-second Remotion composition. 1920 × 1080 at 30 fps. The first 18 seconds introduce ReferralEasy, the coordination problem, and how to use the app.
The remaining 42 seconds explain the architecture and safeguards for technical listeners. Includes local male narration and an
original futuristic techno score at a steady low volume.

Read [the timed script](SCRIPT.md), [the product brief and claim evidence](PRODUCT-BRIEF.md), and
[the audio credits and TTS research](CREDITS.md).

## Preview

```sh
cd videos/referraleasy-showcase
npm ci
npm run dev -- --port 3200
```

Select `ReferralEasy`, or open `http://localhost:3200/ReferralEasy`. Press Play. Each scene is also
registered separately under Scenes. The opening leads with the product name. Narration uses a
conversational launch-demo delivery, at the TTS voice’s natural speed. Audio stems are on separate named timeline tracks.

This video project is separate from the application workspace. It does not change application
dependencies or start the backend. The UI shots are animated reconstructions based on implemented
card contracts. The architecture and failure paths are explanatory animations. All data is
synthetic. The video does not claim a production deployment or measured clinical outcomes.

## Regenerate audio

Generated WAV assets are included. The cached model and isolated environment stay local and are
excluded from Git. To install them again:

```sh
uv venv .audio-venv --python 3.12
uv pip install --python .audio-venv/bin/python -r tools/audio-requirements.txt
npm run voice
npm run score
```

Voice generation uses CPU Pocket TTS 3.3.0, English September 2026, male George. It fits each cue
inside the times in `script.json`. Run `npm run score` to synthesize the new techno bed.
The music source is `tools/compose_techno.py`; the constant-gain mix is in `tools/mix_audio.py`.
Music volume stays at 0.20 for the entire film. Narration volume stays at 0.90. There is no
speech ducking, volume automation, musical fade, build or breakdown.
`public/audio/mix-preview.wav` provides a standalone 60-second audio preview.

## Checks

```sh
npm run lint
npm run qa
```

`qa` checks composition metadata and produces fifteen PNGs. It also verifies that the stationary
demo note has identical text pixels across six samples. On macOS it uses installed
Chrome to avoid an extra browser download. Set `REMOTION_BROWSER_EXECUTABLE` for a different path.
See [the verification record](VERIFICATION.md). The main composition is exactly 1,800 frames.

## Compressed GitHub export

The exported [MP4](../../docs/media/referraleasy-showcase.mp4) is 4.08 MB. It contains the complete
60-second video at 1280 × 720, 30 fps, with H.264 video and stereo AAC audio. Playback metadata is
at the front of the file for progressive loading. A [poster image](../../docs/media/referraleasy-showcase.jpg)
and [GitHub Pages embed snippet](../../docs/media/README.md) are included.

For a new full-resolution export from Studio, use Render, or run:

```sh
npx remotion render ReferralEasy out/referraleasy-showcase.mp4 --codec=h264 --crf=18 --browser-executable="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```
