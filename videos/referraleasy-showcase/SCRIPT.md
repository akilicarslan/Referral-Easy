# ReferralEasy — A simpler journey. Control built in.

60 seconds. 1920 × 1080. 30 fps. Business: 00:00–00:18 (30%). Engineering: 00:18–01:00 (70%).

The opening introduces the name and purpose, then explains the problem and demonstrates how to use the app. The delivery is a conversational enterprise product launch, with technical detail introduced through the demo. ASD-STE100 is not applied to this video.

| Time | Male narration | Picture |
| --- | --- | --- |
| 00:00–00:06 | Meet ReferralEasy, a simpler way to coordinate specialist referrals. | Lead with the ReferralEasy name and the definition: specialist referrals, made simple. The path connects referral, coordination and appointment. |
| 00:06–00:18 | Referrals can mean chasing coverage, paperwork, and appointments. Here, you open your referral, compare providers, choose a time, and approve. | Demonstrate the patient flow: choose a hospital, compare specialists, select an appointment, review the details, then approve. Keep the demo note large and stationary. |
| 00:18–00:27 | Seven agents work together behind the scenes. Coverage and provider matching run in parallel, while the gateway enforces permissions. | Show the gateway, coordinator and six specialists. Highlight coverage and matching in parallel. The gateway enforces policy; ADK, A2A and PostgreSQL appear on screen. |
| 00:27–00:36 | Your approval covers the exact request. If a booking response is lost, we check the same request key to prevent duplicates. | Bind approval to patient, workflow, payload hash and expiry. Show a committed booking, lost response and same-key lookup resolving the original booking. |
| 00:36–00:43 | After a restart, Postgres restores progress. Expired workers can't overwrite newer decisions. | Worker A expires. The persisted PostgreSQL checkpoint stays. Worker B resumes with a new generation. Refuse the late write. |
| 00:43–00:53 | When a document is missing or a slot disappears, you get a clear next step. Human handoff includes your summary and permission. | Show missing documents, unknown coverage, stale slots and outages. Keep a clear next action. End with summary approval and a queued human handoff. |
| 00:53–01:00 | OpenTelemetry connects every step. ReferralEasy. A simpler experience, with control built in. | Show the OpenTelemetry waterfall, then the product name and final line: A simpler journey. Control built in. |

Pocket TTS pronounces “Referral Easy,” “Open Telemetry,” and “Postgres” naturally. The displayed names are ReferralEasy, OpenTelemetry and PostgreSQL. Every cue uses the model’s natural speed; no cue is accelerated or slowed.

Keep the accepted obsidian, ivory and teal visual direction. The soundtrack remains Signal Path: original futuristic techno at 128 BPM, fixed at 0.20 gain throughout. No narration ducking or volume swells.

The product uses synthetic data. Interface shots are animated reconstructions based on implemented card contracts. The patient scene has a fixed, readable “Illustrative demo · Synthetic data” note outside all scene and card transforms. The architecture and failure paths are explanatory animations.
