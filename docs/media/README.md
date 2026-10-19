# ReferralEasy showcase assets

[`referraleasy-showcase-enterprise.mp4`](referraleasy-showcase-enterprise.mp4) is the compressed web export linked at the top of the repository README. It contains the complete 60-second film,
male narration and an original futuristic techno score. `referraleasy-showcase.jpg` is its poster image.
The music stays at one low volume throughout, with no narration ducking or volume swells.

Export: 4.08 MB, 1280 × 720, 30 fps, H.264 video and stereo AAC audio. Fast-start MP4.
Duration: 60 seconds. See `export-info.json` for exact metadata.

For a GitHub Pages HTML page in the parent `docs/` directory:

```html
<video controls playsinline preload="metadata"
       poster="media/referraleasy-showcase.jpg"
       style="width:100%;max-width:1280px;height:auto">
  <source src="media/referraleasy-showcase-enterprise.mp4" type="video/mp4">
  <a href="media/referraleasy-showcase-enterprise.mp4">Watch the ReferralEasy showcase</a>
</video>
```

The repository README embeds the [GitHub-hosted video attachment](https://github.com/user-attachments/assets/24517357-e5ea-45a1-b4d0-4db36b714ea3)
as a native inline player, with a direct relative link to the tracked MP4 as a fallback.

To replace the inline video, upload the new MP4 through a GitHub attachment field, then replace
the attachment URL in the repository README. Keep the URL on its own line with blank lines
around it so GitHub renders a player. Retain the repository MP4 link as a fallback.
See GitHub's [attachment instructions and video limits](https://docs.github.com/en/get-started/writing-on-github/working-with-advanced-formatting/attaching-files).

The film uses synthetic data and animated UI reconstructions. It presents implemented engineering
safeguards without claiming a deployed healthcare service.

Narration: Kyutai Pocket TTS, George voice from CSTR VCTK. Model and voice source: CC BY 4.0.
VCTK: Junichi Yamagishi, Christophe Veaux and Kirsten MacDonald, University of Edinburgh.
Music: “Signal Path,” an original synthesized techno composition at 128 BPM.
See [full credits](../../videos/referraleasy-showcase/CREDITS.md),
[Pocket TTS](https://huggingface.co/kyutai/pocket-tts-without-voice-cloning),
[VCTK](https://datashare.ed.ac.uk/handle/10283/3443), and
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).


## UI screenshots

The repository README uses separate small thumbnails linked to full-size JPEG captures:

- `referral-create.jpg` — the sample-patient creation form.
- `referral-journey.jpg` — a referral rendered through the real gateway and A2UI adapter.
- `admin-dashboard.jpg` — the operations overview using the local telemetry source.
- `component-gallery.jpg` — the static, interactive fixture gallery.

Captured October 19, 2026. All displayed records and traffic are synthetic. These are browser
captures of the implemented UI, not design mockups. Counts reflect the local stack at capture time.
Each `*-thumb.jpg` is resized to 520 pixels wide and displayed at 260 pixels in the README;
clicking it opens its full-size counterpart.
