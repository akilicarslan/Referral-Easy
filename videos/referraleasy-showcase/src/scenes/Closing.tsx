import React from "react";
import { interpolate, Interactive, useCurrentFrame } from "remotion";
import { C, clamp, Frame, Label, Logo, mono, Panel } from "../design";

const TaglineInner: React.FC<{
  children: string;
  style?: React.CSSProperties;
}> = ({ children, style }) => (
  <div
    style={{
      fontFamily: "Georgia, serif",
      fontStyle: "italic",
      fontSize: 79,
      color: C.teal,
      letterSpacing: -2,
      ...style,
    }}
  >
    {children}
  </div>
);
export const Tagline = Interactive.withSchema({
  Component: TaglineInner,
  componentName: "<Tagline>",
  schema: {
    children: {
      type: "text-content",
      default: "A simpler journey. Control built in.",
      description: "Final product line",
    },
  },
  wrapInSequence: true,
});

export const Closing: React.FC = () => {
  const f = useCurrentFrame();
  const traceOpacity = interpolate(f, [0, 18, 77, 100], [0, 1, 1, 0], clamp);
  const endOpacity = interpolate(f, [83, 113], [0, 1], clamp);
  return (
    <Frame number="07" chapter="Observable by design" duration={210}>
      <div
        style={{
          position: "absolute",
          left: 112,
          right: 112,
          top: 163,
          opacity: traceOpacity,
        }}
      >
        <Label>05 / Follow the evidence</Label>
        <h1
          style={{
            fontSize: 101,
            fontWeight: 400,
            letterSpacing: -4,
            margin: "25px 0 38px",
          }}
        >
          One trace.{" "}
          <span
            style={{
              fontFamily: "Georgia, serif",
              fontStyle: "italic",
              color: C.teal,
            }}
          >
            The whole journey.
          </span>
        </h1>
        <Panel style={{ padding: "30px 38px" }}>
          <div
            style={{
              fontFamily: mono,
              fontSize: 18,
              color: C.muted,
              marginBottom: 26,
            }}
          >
            OTEL / GATEWAY → A2A SPECIALISTS / ILLUSTRATIVE WATERFALL
          </div>
          {[
            ["Gateway", 0, 98],
            ["Coordinator", 9, 83],
            ["Intake", 14, 16],
            ["Coverage", 33, 24],
            ["Matching", 33, 35],
            ["Readiness", 69, 13],
            ["Scheduling", 85, 12],
          ].map(([name, left, width], i) => (
            <div
              key={name}
              style={{
                height: 55,
                display: "flex",
                gap: 20,
                alignItems: "center",
              }}
            >
              <span
                style={{
                  width: 205,
                  fontSize: 23,
                  color: i < 2 ? C.ink : C.muted,
                }}
              >
                {name}
              </span>
              <div
                style={{
                  position: "relative",
                  height: 24,
                  flex: 1,
                  background:
                    "linear-gradient(90deg,transparent 99%,#45605d44 99%)",
                  backgroundSize: "20% 100%",
                }}
              >
                <div
                  style={{
                    position: "absolute",
                    left: `${left}%`,
                    width: `${Number(width) * interpolate(f, [i * 5, i * 5 + 23], [0, 1], clamp)}%`,
                    height: 22,
                    borderRadius: 4,
                    background: i === 3 || i === 4 ? C.teal : "#427a71",
                  }}
                />
              </div>
            </div>
          ))}
        </Panel>
      </div>
      <div
        style={{
          position: "absolute",
          inset: 0,
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          alignItems: "center",
          paddingBottom: 0,
          opacity: endOpacity,
          scale: interpolate(f, [85, 209], [0.97, 1], clamp),
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 27 }}>
          <Logo size={117} />
          <span style={{ fontSize: 131, letterSpacing: -7, fontWeight: 400 }}>
            ReferralEasy
          </span>
        </div>
        <div
          style={{
            height: 1,
            width: 160,
            background: C.teal,
            margin: "43px 0",
          }}
        />
        <Tagline name="Final line" premountFor={30}>
          A simpler journey. Control built in.
        </Tagline>
        <div
          style={{
            marginTop: 49,
            fontFamily: mono,
            fontSize: 20,
            letterSpacing: 2,
            color: C.muted,
          }}
        >
          GUIDED EXPERIENCE. GOVERNED AUTONOMY. VISIBLE EVIDENCE.
        </div>
      </div>
    </Frame>
  );
};
