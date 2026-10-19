import React from "react";
import { useCurrentFrame } from "remotion";
import { C, Frame, Label, mono, Panel, Pill, Reveal } from "../design";

export const Recovery: React.FC = () => {
  const f = useCurrentFrame();
  return (
    <Frame
      number="06"
      chapter="Failure paths / recovery + handoff"
      duration={300}
    >
      <div style={{ position: "absolute", left: 112, top: 158 }}>
        <Label>04 / Edge cases are part of the journey</Label>
        <h1
          style={{
            fontSize: 103,
            letterSpacing: -4,
            fontWeight: 400,
            margin: "25px 0",
          }}
        >
          A safe next step.
          <br />
          <span
            style={{
              fontFamily: "Georgia, serif",
              fontStyle: "italic",
              color: C.teal,
            }}
          >
            Even when the path breaks.
          </span>
        </h1>
      </div>
      <div
        style={{
          position: "absolute",
          left: 112,
          top: 470,
          width: 1000,
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 20,
        }}
      >
        {[
          ["Missing document", "Pause before booking", 8],
          ["Unknown coverage", "Keep uncertainty visible", 26],
          ["Stale slot", "Refresh available options", 44],
          ["Upstream outage", "Bound retries, then recover", 62],
        ].map(([title, detail, delay]) => (
          <Reveal key={title} delay={Number(delay)}>
            <Panel style={{ padding: "28px 30px", height: 123 }}>
              <div
                style={{
                  display: "flex",
                  gap: 14,
                  alignItems: "center",
                  color: C.amber,
                  fontSize: 28,
                }}
              >
                <span style={{ fontFamily: mono, fontSize: 23 }}>↳</span>
                {title}
              </div>
              <div style={{ color: C.muted, fontSize: 23, marginTop: 16 }}>
                {detail}
              </div>
            </Panel>
          </Reveal>
        ))}
      </div>
      <Reveal
        delay={100}
        style={{ position: "absolute", left: 1185, top: 470, width: 620 }}
      >
        <Panel style={{ padding: 32, height: 385, borderColor: `${C.teal}70` }}>
          <Label style={{ fontSize: 17 }}>Consented handoff</Label>
          <div style={{ fontSize: 37, marginTop: 27, lineHeight: 1.3 }}>
            {f > 205 ? "Summary approved." : "Review the shared summary."}
            <br />
            <span style={{ color: f > 205 ? C.teal : C.muted }}>
              {f > 205 ? "Ticket queued." : "The patient decides."}
            </span>
          </div>
          <div
            style={{
              marginTop: 34,
              borderTop: `1px solid ${C.line}`,
              paddingTop: 26,
              fontSize: 23,
              color: C.muted,
              lineHeight: 1.5,
            }}
          >
            Evidence and context stay attached.
            <br />
            Human ownership pauses automation.
          </div>
          <div style={{ marginTop: 23 }}>
            <Pill>
              {f > 205
                ? "QUEUED · AWAITING OPERATOR"
                : "PATIENT CONSENT REQUIRED"}
            </Pill>
          </div>
        </Panel>
      </Reveal>
      <Reveal delay={148} style={{ position: "absolute", left: 112, top: 891 }}>
        <span style={{ fontFamily: mono, fontSize: 20, color: C.muted }}>
          RETRIEVAL RECOVERY
        </span>
        <span style={{ fontSize: 24, color: C.teal, marginLeft: 24 }}>
          Hybrid → BM25 → Versioned seed → Human
        </span>
      </Reveal>
    </Frame>
  );
};
