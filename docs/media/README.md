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

The repository README uses a clickable poster and a direct link to the tracked MP4. Both use
relative paths, so they keep working when the repository is renamed or transferred.

For a native inline GitHub video attachment, upload this MP4 through a GitHub attachment field,
then place the returned `https://github.com/user-attachments/assets/...` URL on its own line below
the title. Retain the repository MP4 link as a fallback. A local file path cannot supply that
GitHub-hosted attachment URL; the current README therefore uses the linked poster.
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
