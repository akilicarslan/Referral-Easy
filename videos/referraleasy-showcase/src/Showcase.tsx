import React from "react";
import { AbsoluteFill, staticFile, useVideoConfig } from "remotion";
import { Audio } from "@remotion/media";
import { TransitionSeries } from "@remotion/transitions";
import { CutLight } from "./design";
import { Opening } from "./scenes/Opening";
import { Journey } from "./scenes/Journey";
import { Architecture } from "./scenes/Architecture";
import { Booking } from "./scenes/Booking";
import { Continuity } from "./scenes/Continuity";
import { Recovery } from "./scenes/Recovery";
import { Closing } from "./scenes/Closing";

export const Showcase: React.FC = () => {
  const { fps } = useVideoConfig();
  return (
    <AbsoluteFill>
      <TransitionSeries>
        <TransitionSeries.Sequence
          name="Business · Introducing ReferralEasy"
          durationInFrames={180}
          premountFor={fps}
        >
          <Opening />
        </TransitionSeries.Sequence>
        <TransitionSeries.Overlay durationInFrames={18} premountFor={fps}>
          <CutLight />
        </TransitionSeries.Overlay>
        <TransitionSeries.Sequence
          name="Business · Patient-led journey"
          durationInFrames={360}
          premountFor={fps}
        >
          <Journey />
        </TransitionSeries.Sequence>
        <TransitionSeries.Overlay durationInFrames={18} premountFor={fps}>
          <CutLight />
        </TransitionSeries.Overlay>
        <TransitionSeries.Sequence
          name="Engineering · Seven governed agents"
          durationInFrames={270}
          premountFor={fps}
        >
          <Architecture />
        </TransitionSeries.Sequence>
        <TransitionSeries.Overlay durationInFrames={18} premountFor={fps}>
          <CutLight />
        </TransitionSeries.Overlay>
        <TransitionSeries.Sequence
          name="Engineering · Same-key reconciliation"
          durationInFrames={270}
          premountFor={fps}
        >
          <Booking />
        </TransitionSeries.Sequence>
        <TransitionSeries.Overlay durationInFrames={18} premountFor={fps}>
          <CutLight />
        </TransitionSeries.Overlay>
        <TransitionSeries.Sequence
          name="Engineering · Durable restart"
          durationInFrames={210}
          premountFor={fps}
        >
          <Continuity />
        </TransitionSeries.Sequence>
        <TransitionSeries.Overlay durationInFrames={18} premountFor={fps}>
          <CutLight />
        </TransitionSeries.Overlay>
        <TransitionSeries.Sequence
          name="Engineering · Recovery and handoff"
          durationInFrames={300}
          premountFor={fps}
        >
          <Recovery />
        </TransitionSeries.Sequence>
        <TransitionSeries.Overlay durationInFrames={18} premountFor={fps}>
          <CutLight />
        </TransitionSeries.Overlay>
        <TransitionSeries.Sequence
          name="Engineering · Evidence and closing"
          durationInFrames={210}
          premountFor={fps}
        >
          <Closing />
        </TransitionSeries.Sequence>
      </TransitionSeries>
      <Audio
        name="Male narrator · Pocket TTS George"
        src={staticFile("audio/narration.wav")}
        premountFor={fps}
        volume={0.9}
      />
      <Audio
        name="Techno · Signal Path · steady low level"
        src={staticFile("audio/techno-score.wav")}
        premountFor={fps}
        volume={0.2}
      />
    </AbsoluteFill>
  );
};
