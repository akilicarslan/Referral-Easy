import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { C, clamp, Frame, Label, mono, Panel, Pill, Reveal } from "../design";

export const Booking: React.FC = () => {
  const f = useCurrentFrame();
  const resolved = f > 168;
  return (
    <Frame number="04" chapter="Failure path / lost response" duration={270}>
      <div style={{ position: "absolute", left: 112, top: 158 }}>
        <Label>02 / Transaction integrity</Label>
        <h1
          style={{
            fontSize: 100,
            letterSpacing: -4,
            fontWeight: 400,
            margin: "23px 0",
          }}
        >
          The response is lost.
          <br />
          <span
            style={{
              fontFamily: "Georgia, serif",
              fontStyle: "italic",
              color: C.teal,
            }}
          >
            The booking is not repeated.
          </span>
        </h1>
      </div>
      <Reveal
        delay={8}
        style={{ position: "absolute", left: 112, top: 455, width: 520 }}
      >
        <Panel style={{ padding: 33 }}>
          <Label style={{ fontSize: 17 }}>Approval binds to</Label>
          {["Patient + workflow", "State version + payload hash", "Expiry"].map(
            (v) => (
              <div
                key={v}
                style={{
                  fontSize: 26,
                  borderTop: `1px solid ${C.line}`,
                  marginTop: 20,
                  paddingTop: 18,
                }}
              >
                {v}
              </div>
            ),
          )}
          <div style={{ marginTop: 28 }}>
            <Pill>Exact request approved</Pill>
          </div>
        </Panel>
      </Reveal>
      <div style={{ position: "absolute", left: 735, top: 469, width: 1070 }}>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            fontFamily: mono,
            fontSize: 20,
            color: C.muted,
            marginBottom: 28,
          }}
        >
          <span>SCHEDULING</span>
          <span>MOCK HOSPITAL</span>
        </div>
        <div
          style={{
            position: "relative",
            height: 360,
            borderLeft: `1px solid ${C.line}`,
            borderRight: `1px solid ${C.line}`,
          }}
        >
          <Reveal
            delay={20}
            style={{ position: "absolute", left: 32, right: 32, top: 0 }}
          >
            <div style={{ fontFamily: mono, fontSize: 24, color: C.teal }}>
              create_booking(key: referral-7f2)
            </div>
            <div
              style={{
                height: 2,
                background: C.teal,
                marginTop: 15,
                width: `${interpolate(f, [20, 65], [0, 100], clamp)}%`,
              }}
            />
            <span
              style={{
                position: "absolute",
                right: 0,
                top: 46,
                color: C.teal,
                fontSize: 23,
              }}
            >
              ✓ Committed
            </span>
          </Reveal>
          <Reveal
            delay={80}
            style={{ position: "absolute", left: 32, right: 32, top: 112 }}
          >
            <div
              style={{
                borderTop: `2px dashed ${C.amber}`,
                width: "70%",
                marginLeft: "auto",
              }}
            />
            <div
              style={{
                fontFamily: mono,
                fontSize: 22,
                color: C.amber,
                marginTop: 18,
              }}
            >
              × Response lost → outcome_unknown
            </div>
          </Reveal>
          <Reveal
            delay={130}
            style={{ position: "absolute", left: 32, right: 32, top: 226 }}
          >
            <div style={{ fontFamily: mono, fontSize: 24, color: C.teal }}>
              lookup_request(key: referral-7f2)
            </div>
            <div
              style={{
                height: 2,
                background: C.teal,
                marginTop: 15,
                width: `${interpolate(f, [130, 168], [0, 100], clamp)}%`,
              }}
            />
          </Reveal>
        </div>
        <Reveal delay={170}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 24,
              marginTop: 20,
            }}
          >
            <span
              style={{
                fontSize: 74,
                color: C.teal,
                fontFamily: "Georgia, serif",
              }}
            >
              01
            </span>
            <span style={{ fontSize: 28 }}>
              Existing booking resolved
              <br />
              <span style={{ fontSize: 21, color: C.muted }}>
                Same key. No second booking send.
              </span>
            </span>
          </div>
        </Reveal>
      </div>
      <div
        style={{
          position: "absolute",
          left: 112,
          top: 893,
          fontFamily: mono,
          fontSize: 18,
          color: resolved ? C.teal : C.muted,
        }}
      >
        CONSENT → COMMIT → {resolved ? "RECONCILED" : "UNCERTAINTY → LOOKUP"}
      </div>
    </Frame>
  );
};
