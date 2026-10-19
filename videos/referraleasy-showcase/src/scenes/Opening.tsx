import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { C, clamp, ease, Frame, Label, mono, Reveal } from "../design";

export const Opening: React.FC = () => {
  const f = useCurrentFrame();
  const path =
    "M 1065 320 H 1220 Q 1280 320 1280 380 V 460 Q 1280 520 1340 520 H 1510 Q 1570 520 1570 580 V 655 Q 1570 715 1630 715 H 1770";
  return (
    <Frame number="01" chapter="The business case" duration={180}>
      <div style={{ position: "absolute", left: 112, top: 235 }}>
        <Reveal delay={7}>
          <Label>Introducing</Label>
        </Reveal>
        <Reveal delay={18}>
          <h1
            style={{
              fontSize: 132,
              lineHeight: 0.99,
              fontWeight: 400,
              letterSpacing: -6,
              margin: "42px 0 34px",
            }}
          >
            ReferralEasy
            <br />
            <span
              style={{
                fontFamily: "Georgia, serif",
                fontStyle: "italic",
                color: C.teal,
                fontSize: 78,
                lineHeight: 1.12,
              }}
            >
              Specialist referrals,
              <br />
              made simple.
            </span>
          </h1>
        </Reveal>
        <Reveal delay={50}>
          <p
            style={{
              fontSize: 31,
              color: C.muted,
              lineHeight: 1.5,
              maxWidth: 790,
            }}
          >
            Providers. Coverage. Paperwork. Appointments.
            <br />
            One guided experience.
          </p>
        </Reveal>
      </div>
      <svg width="1920" height="1080" style={{ position: "absolute" }}>
        <defs>
          <filter id="glow">
            <feGaussianBlur stdDeviation="9" />
          </filter>
        </defs>
        <path
          d={path}
          stroke={C.teal}
          strokeWidth="8"
          fill="none"
          opacity=".25"
          filter="url(#glow)"
          pathLength="1"
          strokeDasharray="1"
          strokeDashoffset={interpolate(f, [22, 145], [1, 0], clamp)}
        />
        <path
          d={path}
          stroke={C.teal}
          strokeWidth="2"
          fill="none"
          pathLength="1"
          strokeDasharray="1"
          strokeDashoffset={interpolate(f, [22, 145], [1, 0], {
            ...clamp,
            easing: ease,
          })}
        />
      </svg>
      {[
        { x: 1070, y: 276, t: "REFERRAL", n: "01", d: 25 },
        { x: 1325, y: 477, t: "COORDINATE", n: "02", d: 65 },
        { x: 1610, y: 673, t: "APPOINTMENT", n: "03", d: 110 },
      ].map((item) => (
        <Reveal
          key={item.t}
          delay={item.d}
          style={{ position: "absolute", left: item.x, top: item.y }}
        >
          <div
            style={{
              padding: "24px 28px",
              border: `1px solid ${C.line}`,
              background: "#0d2225",
              borderRadius: 10,
              boxShadow: "0 20px 50px #0006",
            }}
          >
            <span style={{ fontFamily: mono, color: C.teal, fontSize: 16 }}>
              {item.n}
            </span>
            <div style={{ fontSize: 22, letterSpacing: 2, marginTop: 8 }}>
              {item.t}
            </div>
          </div>
        </Reveal>
      ))}
    </Frame>
  );
};
