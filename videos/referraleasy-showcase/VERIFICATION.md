# Verification record

2026-10-19. Current enterprise-demo revision.

- ESLint and TypeScript checks passed for the video project.
- Main composition: 1,800 frames at 30 fps, 1920 × 1080. Total duration: 60 seconds.
- Business opening: 0–18 seconds. Engineering section: 18–60 seconds.
- The opening leads with ReferralEasy, identifies the platform, and explains the referral problem.
  The next scene demonstrates hospital, specialist, time selection, review and approval.
- All seven male narration cues were regenerated. Every cue uses natural speed (tempo 1.0), with
  no acceleration or slowing, and ends within its scene.
- The accepted techno bed remains at fixed gain 0.20. Narration stays at 0.90. No music ducking or
  volume automation was added. The current WAV mix measures -16.1 LUFS with -2.1 dBFS true peak.
- Fifteen still frames rendered, including nine scene samples and six label-stability samples.
  The revised opening and patient journey were visually reviewed.
- The old tiny technical label is removed. Its replacement is larger, has an opaque background,
  and stays outside scene/card transforms at fixed integer coordinates.
- The replacement label's text pixels are identical across six uncompressed samples, including
  adjacent frames and different UI phases. See `qa/label-stability.json`.
- All 324 encoded frames checked between 7.0 and 17.8 seconds retain the label text.
  Minimum bright-text pixel count equals the median count. Average label luminance varies by
  only 0.046 on the 0–255 scale. No blinking was detected.
- The complete MP4 decoded with FFmpeg without errors. An audible listening review remains for
  the human viewer; the agent checked timing and signal measurements.

## GitHub export

`docs/media/referraleasy-showcase-enterprise.mp4` is the named download. The canonical showcase
and previous techno-named URL are updated too. File size: 4,081,169 bytes (4.08 MB).
Dimensions: 1280 × 720 at 30 fps, with H.264 video and stereo AAC audio. Container duration:
60.011 seconds, including audio encoder padding. The `moov` atom precedes `mdat`
for progressive loading. The poster now shows the name-first opening.

Earlier scripts and exports remain in the ignored `.cache/before-enterprise-revision/` directory.
The application code was read to ground claims; its test suite was not rerun for this video-only
change. See `PRODUCT-BRIEF.md` for source evidence. ASD-STE100 was waived for this video only.
