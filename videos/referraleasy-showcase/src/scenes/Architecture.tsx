import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { C, clamp, Frame, Label, mono, Panel, Pill, Reveal } from "../design";

const Node: React.FC<{
  x: number;
  y: number;
  title: string;
  sub: string;
  delay: number;
  accent?: boolean;
}> = ({ x, y, title, sub, delay, accent }) => (
  <Reveal
    delay={delay}
    style={{ position: "absolute", left: x, top: y, width: 290 }}
  >
    <Panel
      style={{
        padding: "23px 24px",
        borderColor: accent ? `${C.teal}80` : C.line,
      }}
    >
      <div style={{ fontSize: 27, color: accent ? C.teal : C.ink }}>
        {title}
      </div>
      <div
        style={{
          fontFamily: mono,
          fontSize: 16,
          color: C.muted,
          marginTop: 12,
          whiteSpace: "pre-line",
          lineHeight: 1.4,
        }}
      >
        {sub}
      </div>
    </Panel>
  </Reveal>
);

export const Architecture: React.FC = () => {
  const f = useCurrentFrame();
  const coverage = interpolate(f, [55, 90], [0, 1], clamp);
  return (
    <Frame number="03" chapter="Inside the system" duration={270}>
      <div style={{ position: "absolute", left: 112, top: 155 }}>
        <Label>01 / Architecture</Label>
        <h1
          style={{
            fontSize: 91,
            fontWeight: 400,
            letterSpacing: -4,
            margin: "22px 0",
          }}
        >
          Seven agents.{" "}
          <span
            style={{
              color: C.teal,
              fontFamily: "Georgia, serif",
              fontStyle: "italic",
            }}
          >
            One governed journey.
          </span>
        </h1>
      </div>
      <svg
        width="1920"
        height="1080"
        style={{ position: "absolute", top: 0, left: 0 }}
      >
        {[
          "M 403 590 H 510",
          "M 800 590 H 965 V 385 H 1060",
          "M 800 590 H 1060",
          "M 800 590 H 965 V 795 H 1060",
          "M 965 385 V 308 H 1415 V 795",
          "M 1415 385 H 1450",
          "M 1415 590 H 1450",
          "M 1415 795 H 1450",
        ].map((d, i) => (
          <path
            key={d}
            d={d}
            fill="none"
            stroke={i === 2 || i === 6 ? C.teal : C.line}
            strokeWidth={2}
            pathLength={1}
            strokeDasharray={1}
            strokeDashoffset={interpolate(
              f,
              [25 + i * 5, 55 + i * 5],
              [1, 0],
              clamp,
            )}
          />
        ))}
        <path
          d="M 1040 696 H 1760"
          stroke={C.teal}
          strokeWidth={1}
          opacity={coverage * 0.4}
          strokeDasharray="4 8"
        />
        <path
          d="M 655 660 V 912 H 1570"
          stroke={C.line}
          fill="none"
          strokeDasharray="5 8"
        />
        <circle
          cx={interpolate(f % 85, [0, 85], [805, 1060])}
          cy="590"
          r="4"
          fill={C.teal}
        />
      </svg>
      <Node
        x={112}
        y={534}
        title="Patient UI + Gateway"
        sub={"FastAPI / A2UI / SSE\n3-layer input checks"}
        delay={5}
      />
      <Node
        x={510}
        y={534}
        title="Care Coordinator"
        sub="ADK / A2A · shared budgets"
        delay={18}
        accent
      />
      <Node
        x={1060}
        y={330}
        title="Referral Intake"
        sub="Structure and clarify"
        delay={30}
      />
      <Node
        x={1450}
        y={330}
        title="Recovery & Handoff"
        sub="Evidence and safe exits"
        delay={36}
      />
      <Node
        x={1060}
        y={535}
        title="Coverage & Network"
        sub="Plan + provider evidence"
        delay={55}
        accent
      />
      <Node
        x={1450}
        y={535}
        title="Provider Matching"
        sub="Constraints + hybrid retrieval"
        delay={55}
        accent
      />
      <Node
        x={1060}
        y={740}
        title="Referral Readiness"
        sub="Hospital-specific requirements"
        delay={76}
      />
      <Node
        x={1450}
        y={740}
        title="Scheduling"
        sub="Slots · writes · reconciliation"
        delay={85}
      />
      <Reveal delay={75} style={{ position: "absolute", left: 1190, top: 705 }}>
        <Label style={{ fontSize: 16 }}>
          Independent work runs in parallel
        </Label>
      </Reveal>
      <Reveal
        delay={112}
        style={{ position: "absolute", left: 112, top: 750, width: 730 }}
      >
        <div style={{ display: "flex", gap: 14 }}>
          <Pill>Server-side policy</Pill>
          <Pill>PostgreSQL authority</Pill>
        </div>
        <p
          style={{
            fontSize: 23,
            lineHeight: 1.5,
            color: C.muted,
            maxWidth: 620,
          }}
        >
          Typed results inform the next step.
          <br />
          Policy controls every permitted action.
        </p>
      </Reveal>
      <Reveal
        delay={130}
        style={{ position: "absolute", left: 1070, top: 896 }}
      >
        <span style={{ color: C.muted, fontFamily: mono, fontSize: 17 }}>
          POSTGRESQL / CHECKPOINTS + COMMANDS + APPROVALS
        </span>
      </Reveal>
    </Frame>
  );
};
