import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { C, clamp, Frame, Label, mono, Panel, Pill, Reveal } from "../design";

export const Continuity: React.FC = () => {
  const f = useCurrentFrame();
  return (
    <Frame number="05" chapter="Failure path / worker restart" duration={210}>
      <div style={{ position: "absolute", left: 112, top: 167 }}>
        <Label>03 / Durable execution</Label>
        <h1
          style={{
            fontSize: 107,
            letterSpacing: -5,
            fontWeight: 400,
            margin: "24px 0",
          }}
        >
          The process stops.
          <br />
          <span
            style={{
              fontFamily: "Georgia, serif",
              fontStyle: "italic",
              color: C.teal,
            }}
          >
            Progress stays.
          </span>
        </h1>
      </div>
      <div
        style={{
          position: "absolute",
          left: 112,
          top: 525,
          width: 1695,
          height: 283,
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <Panel
          style={{
            width: 370,
            padding: 33,
            opacity: interpolate(f, [42, 78], [1, 0.38], clamp),
          }}
        >
          <Label color={C.muted} style={{ fontSize: 17 }}>
            Worker A / generation 12
          </Label>
          <div style={{ fontSize: 37, marginTop: 23 }}>Answer stored</div>
          <div
            style={{
              fontSize: 24,
              color: f > 65 ? C.amber : C.teal,
              marginTop: 21,
            }}
          >
            {f > 65 ? "Lease expired" : "Processing patient choice"}
          </div>
        </Panel>
        <div style={{ width: 110, height: 2, background: C.line }} />
        <Reveal delay={20}>
          <Panel
            style={{
              width: 440,
              padding: 36,
              borderColor: `${C.teal}99`,
              boxShadow: "0 0 90px #8ce8d012",
            }}
          >
            <Label style={{ fontSize: 17 }}>PostgreSQL checkpoint</Label>
            <div style={{ fontSize: 45, marginTop: 24 }}>Patient choice</div>
            <div
              style={{
                fontFamily: mono,
                fontSize: 20,
                color: C.muted,
                marginTop: 20,
              }}
            >
              committed · durable · claimable
            </div>
          </Panel>
        </Reveal>
        <div
          style={{
            width: 110,
            height: 2,
            background: f > 92 ? C.teal : C.line,
          }}
        />
        <Reveal delay={90}>
          <Panel
            style={{ width: 370, padding: 33, borderColor: `${C.teal}66` }}
          >
            <Label style={{ fontSize: 17 }}>Worker B / generation 13</Label>
            <div style={{ fontSize: 37, marginTop: 23 }}>Resume safely</div>
            <div style={{ fontSize: 24, color: C.teal, marginTop: 21 }}>
              Continue the stored answer
            </div>
          </Panel>
        </Reveal>
      </div>
      <Reveal
        delay={146}
        style={{
          position: "absolute",
          left: 112,
          top: 850,
          display: "flex",
          gap: 24,
          alignItems: "center",
        }}
      >
        <Pill color={C.amber}>Late write from generation 12 → rejected</Pill>
        <span style={{ fontSize: 24, color: C.muted }}>
          Newer state keeps authority.
        </span>
      </Reveal>
    </Frame>
  );
};
