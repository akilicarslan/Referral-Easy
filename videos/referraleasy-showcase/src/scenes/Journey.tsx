import React from "react";
import { interpolate, useCurrentFrame } from "remotion";
import { C, clamp, Frame, Label, Logo, mono, Reveal } from "../design";

const Row: React.FC<{
  title: string;
  detail: string;
  selected?: boolean;
  badge?: string;
}> = ({ title, detail, selected, badge }) => (
  <div
    style={{
      border: `1.5px solid ${selected ? "#0b6b60" : "#dce4e4"}`,
      background: selected ? "#eaf5f1" : "#fff",
      padding: "24px 26px",
      borderRadius: 12,
      marginTop: 15,
      display: "flex",
      alignItems: "center",
      gap: 22,
    }}
  >
    <div
      style={{
        border: `2px solid ${selected ? "#0b6b60" : "#b8c5c5"}`,
        borderRadius: 30,
        width: 21,
        height: 21,
        padding: 4,
      }}
    >
      {selected && (
        <div
          style={{
            background: "#0b6b60",
            width: "100%",
            height: "100%",
            borderRadius: 30,
          }}
        />
      )}
    </div>
    <div>
      <div style={{ fontSize: 29, fontWeight: 500 }}>{title}</div>
      <div style={{ fontSize: 22, color: "#647573", marginTop: 7 }}>
        {detail}
      </div>
    </div>
    {badge && (
      <div
        style={{
          marginLeft: "auto",
          fontSize: 17,
          color: badge === "Unknown" ? "#4b5560" : "#0b6b60",
          background: badge === "Unknown" ? "#edeff1" : "#e2f0eb",
          borderRadius: 5,
          padding: "8px 11px",
        }}
      >
        {badge}
      </div>
    )}
  </div>
);

export const Journey: React.FC = () => {
  const f = useCurrentFrame();
  const phase = f < 65 ? 0 : f < 123 ? 1 : f < 182 ? 2 : f < 295 ? 3 : 4;
  const phaseStart = [0, 65, 123, 182, 295][phase];
  const click = f - phaseStart > 35;
  return (
    <Frame
      number="02"
      chapter="The business case · patient choice"
      duration={360}
      staticOverlay={
        <div
          data-demo-note="stationary"
          style={{
            position: "absolute",
            left: 855,
            top: 870,
            width: 954,
            height: 48,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 24,
            fontWeight: 500,
            color: "#b4ccca",
            background: "#0c2023",
            borderRadius: 6,
            zIndex: 5,
          }}
        >
          Illustrative demo · Synthetic data
        </div>
      }
    >
      <div style={{ position: "absolute", left: 112, top: 247, width: 660 }}>
        <Reveal>
          <Label>Let’s walk through it.</Label>
        </Reveal>
        <Reveal delay={8}>
          <h1
            style={{
              fontSize: 108,
              letterSpacing: -5,
              lineHeight: 1.05,
              fontWeight: 400,
              margin: "34px 0",
            }}
          >
            Open. Choose.
            <br />
            <span
              style={{
                fontFamily: "Georgia, serif",
                color: C.teal,
                fontStyle: "italic",
              }}
            >
              Approve.
            </span>
          </h1>
        </Reveal>
        <Reveal delay={22}>
          <p
            style={{
              color: C.muted,
              fontSize: 30,
              lineHeight: 1.5,
              width: 580,
            }}
          >
            {
              [
                "First, choose your hospital.",
                "Compare the specialists.",
                "Pick a time that works.",
                "Review the details, then approve.",
                "Your appointment is confirmed.",
              ][phase]
            }
          </p>
        </Reveal>
        <Reveal delay={197}>
          <div
            style={{
              display: "flex",
              gap: 12,
              alignItems: "center",
              marginTop: 38,
              color: C.teal,
              fontSize: 23,
            }}
          >
            <span style={{ fontSize: 29 }}>✓</span> Explicit approval before a
            request is sent
          </div>
        </Reveal>
      </div>
      <div
        style={{
          position: "absolute",
          left: 855,
          top: 185,
          width: 955,
          height: 700,
        }}
      >
        <div
          style={{
            background: "#f4f6f5",
            color: "#142726",
            borderRadius: 19,
            overflow: "hidden",
            boxShadow: "0 50px 110px #0009",
          }}
        >
          <div
            style={{
              display: "flex",
              gap: 11,
              alignItems: "center",
              padding: "23px 32px",
              background: "white",
              borderBottom: "1px solid #dfe6e3",
            }}
          >
            <Logo size={30} color="#0b6b60" />
            <b style={{ fontSize: 23, fontWeight: 500 }}>ReferralEasy</b>
            <span
              style={{
                marginLeft: "auto",
                color: "#637671",
                fontFamily: mono,
                fontSize: 15,
              }}
            >
              SYNTHETIC DEMO
            </span>
          </div>
          <div style={{ padding: "24px 35px 32px" }}>
            <div style={{ display: "flex", gap: 0, marginBottom: 30 }}>
              {["Hospital", "Doctor", "Time", "Review"].map((s, i) => (
                <div
                  key={s}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    flex: 1,
                    gap: 9,
                    color: i <= phase ? "#0b6b60" : "#8c9996",
                    fontSize: 18,
                  }}
                >
                  <span
                    style={{
                      background: i <= phase ? "#0b6b60" : "#d9e1de",
                      color: "white",
                      borderRadius: 20,
                      padding: "5px 9px",
                      fontSize: 15,
                    }}
                  >
                    {i < phase ? "✓" : i + 1}
                  </span>
                  {s}
                  {i < 3 && (
                    <div
                      style={{
                        height: 1,
                        flex: 1,
                        background: "#cad5d1",
                        marginRight: 10,
                      }}
                    />
                  )}
                </div>
              ))}
            </div>
            <div
              style={{
                minHeight: 425,
                opacity: interpolate(f - phaseStart, [0, 8], [0.2, 1], clamp),
                translate: `${interpolate(f - phaseStart, [0, 18], [12, 0], clamp)}px 0px`,
              }}
            >
              {phase === 0 && (
                <>
                  <h2
                    style={{
                      fontSize: 36,
                      letterSpacing: -1,
                      margin: "0 0 10px",
                    }}
                  >
                    Choose a hospital
                  </h2>
                  <p
                    style={{
                      fontSize: 22,
                      color: "#647573",
                      margin: "0 0 24px",
                    }}
                  >
                    Compare providers against your plan and preferences.
                  </p>
                  <Row
                    title="St. Mary's General"
                    detail="Leeds · 2.4 km · Plan checked 3h ago"
                    badge="Plan verified"
                    selected={click}
                  />
                  <Row
                    title="Northgate Royal Infirmary"
                    detail="Leeds · 6.1 km · Status checked 18h ago"
                    badge="Unknown"
                  />
                  <div
                    style={{ marginTop: 24, fontSize: 19, color: "#647573" }}
                  >
                    Coverage freshness stays visible with each option.
                  </div>
                </>
              )}
              {phase === 1 && (
                <>
                  <h2
                    style={{
                      fontSize: 36,
                      letterSpacing: -1,
                      margin: "0 0 10px",
                    }}
                  >
                    Choose your specialist
                  </h2>
                  <p
                    style={{
                      fontSize: 22,
                      color: "#647573",
                      margin: "0 0 24px",
                    }}
                  >
                    St. Mary's General · Cardiology
                  </p>
                  <Row
                    title="Dr. A. Patel"
                    detail="English · Accepting new referrals"
                    selected={click}
                    badge="Matches"
                  />
                  <Row
                    title="Dr. E. Williams"
                    detail="English · Tuesday appointments"
                  />
                  <div
                    style={{ marginTop: 24, fontSize: 19, color: "#647573" }}
                  >
                    Your distance limit and plan remain binding.
                  </div>
                </>
              )}
              {phase === 2 && (
                <>
                  <h2
                    style={{
                      fontSize: 36,
                      letterSpacing: -1,
                      margin: "0 0 10px",
                    }}
                  >
                    Choose an appointment
                  </h2>
                  <p
                    style={{
                      fontSize: 22,
                      color: "#647573",
                      margin: "0 0 24px",
                    }}
                  >
                    Dr. A. Patel · St. Mary's General
                  </p>
                  <Row
                    title="Tuesday, 27 October"
                    detail="09:10 · In person"
                    selected={click}
                  />
                  <Row
                    title="Thursday, 29 October"
                    detail="11:30 · In person"
                  />
                  <div
                    style={{ marginTop: 24, fontSize: 19, color: "#647573" }}
                  >
                    No slot is selected or booked automatically.
                  </div>
                </>
              )}
              {phase === 3 && (
                <>
                  <h2
                    style={{
                      fontSize: 36,
                      letterSpacing: -1,
                      margin: "0 0 23px",
                    }}
                  >
                    Review your referral
                  </h2>
                  {[
                    ["Hospital", "St. Mary's General"],
                    ["Specialist", "Dr. A. Patel · Cardiology"],
                    ["Appointment", "27 October · 09:10"],
                    ["Documents", "Required documents ready"],
                  ].map(([k, v]) => (
                    <div
                      key={k}
                      style={{
                        display: "flex",
                        padding: "12px 0",
                        borderBottom: "1px solid #e0e7e4",
                        fontSize: 23,
                      }}
                    >
                      <span style={{ color: "#73847f", width: 190 }}>{k}</span>
                      <span>{v}</span>
                    </div>
                  ))}
                  <div
                    style={{
                      display: "flex",
                      gap: 13,
                      alignItems: "center",
                      marginTop: 24,
                      fontSize: 22,
                    }}
                  >
                    <span
                      style={{
                        width: 25,
                        height: 25,
                        background: f > 246 ? "#0b6b60" : "#fff",
                        border: "1px solid #78958a",
                        borderRadius: 4,
                        color: "#fff",
                        textAlign: "center",
                      }}
                    >
                      {f > 246 ? "✓" : ""}
                    </span>
                    I agree to this referral being sent
                  </div>
                  <div
                    style={{
                      marginTop: 20,
                      padding: 18,
                      textAlign: "center",
                      background: f > 246 ? "#0b6b60" : "#c6d2ce",
                      borderRadius: 8,
                      color: "white",
                      fontSize: 23,
                    }}
                  >
                    Send referral request
                  </div>
                </>
              )}
              {phase === 4 && (
                <div style={{ padding: "36px 10px", textAlign: "center" }}>
                  <div
                    style={{
                      width: 72,
                      height: 72,
                      borderRadius: 50,
                      background: "#e1f0e6",
                      color: "#0f6b37",
                      fontSize: 44,
                      margin: "0 auto 20px",
                      lineHeight: "72px",
                    }}
                  >
                    ✓
                  </div>
                  <h2 style={{ fontSize: 39, margin: "14px 0" }}>
                    Appointment confirmed
                  </h2>
                  <p style={{ fontSize: 24, color: "#61766a" }}>
                    The hospital confirmed this appointment.
                  </p>
                  <div style={{ fontSize: 27, marginTop: 25 }}>
                    27 October · 09:10
                  </div>
                  <div
                    style={{ fontSize: 22, color: "#647573", marginTop: 13 }}
                  >
                    St. Mary's General · Dr. A. Patel
                  </div>
                </div>
              )}
            </div>
          </div>
          {phase < 4 && (
            <svg
              width="36"
              height="45"
              viewBox="0 0 36 45"
              style={{
                position: "absolute",
                pointerEvents: "none",
                left:
                  phase === 3
                    ? interpolate(
                        f,
                        [220, 242, 266, 280],
                        [350, 45, 45, 440],
                        clamp,
                      )
                    : interpolate(f - phaseStart, [0, 30], [350, 95], clamp),
                top:
                  phase === 3
                    ? interpolate(
                        f,
                        [220, 242, 266, 280],
                        [420, 470, 470, 560],
                        clamp,
                      )
                    : interpolate(f - phaseStart, [0, 30], [380, 300], clamp),
                opacity: interpolate(f - phaseStart, [8, 18], [0, 1], clamp),
                filter: "drop-shadow(1px 4px 3px #0005)",
              }}
            >
              <path
                d="M4 3V34L12 26L19 42L25 39L18 24L31 23Z"
                fill="#fff"
                stroke="#143b34"
                strokeWidth="2"
              />
            </svg>
          )}
        </div>
      </div>
    </Frame>
  );
};
