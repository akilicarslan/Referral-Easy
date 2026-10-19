import React from "react";
import {
  AbsoluteFill,
  Easing,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export const C = {
  bg: "#071315",
  panel: "#0c2023",
  ink: "#f4f1e8",
  muted: "#8da6a6",
  teal: "#8ce8d0",
  line: "#254044",
  amber: "#edc382",
  red: "#e4978e",
};
export const ease = Easing.bezier(0.16, 1, 0.3, 1);
export const clamp = {
  extrapolateLeft: "clamp",
  extrapolateRight: "clamp",
} as const;
export const mono = '"SFMono-Regular", Menlo, monospace';
export const sans = '"Helvetica Neue", Arial, sans-serif';

export const Logo: React.FC<{ size?: number; color?: string }> = ({
  size = 44,
  color = C.teal,
}) => (
  <svg width={size} height={size} viewBox="0 0 48 48" fill="none">
    <path
      d="M9 36V18a8 8 0 0 1 8-8h14a8 8 0 0 1 0 16H18m0 0 9 10m-9-10 9-10"
      stroke={color}
      strokeWidth="3.5"
      strokeLinecap="round"
      strokeLinejoin="round"
    />
    <circle cx="9" cy="37" r="3" fill={color} />
  </svg>
);

export const Background: React.FC = () => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: C.bg, overflow: "hidden" }}>
      <AbsoluteFill
        style={{
          background:
            "radial-gradient(ellipse at 78% 44%, #17413d66, transparent 58%), radial-gradient(ellipse at 8% 100%, #533e1e33, transparent 50%)",
          translate: `${Math.sin(f / 170) * 18}px 0px`,
        }}
      />
      <AbsoluteFill
        style={{
          backgroundImage: "radial-gradient(#94b2aa25 .8px, transparent .8px)",
          backgroundSize: "37px 37px",
          opacity: 0.37,
        }}
      />
      <svg
        width="1920"
        height="1080"
        style={{ position: "absolute", opacity: 0.25 }}
      >
        <defs>
          <linearGradient id="orbit">
            <stop stopColor="#8ce8d0" stopOpacity="0" />
            <stop offset=".6" stopColor="#8ce8d0" stopOpacity=".6" />
            <stop offset="1" stopColor="#8ce8d0" stopOpacity="0" />
          </linearGradient>
        </defs>
        <ellipse
          cx="1460"
          cy="390"
          rx="710"
          ry="650"
          fill="none"
          stroke="url(#orbit)"
          transform={`rotate(${f / 30} 1460 390)`}
        />
        <ellipse
          cx="1460"
          cy="390"
          rx="760"
          ry="685"
          fill="none"
          stroke="url(#orbit)"
        />
      </svg>
      <AbsoluteFill style={{ boxShadow: "inset 0 0 200px 55px #02070766" }} />
    </AbsoluteFill>
  );
};

export const Label: React.FC<{
  children: React.ReactNode;
  color?: string;
  style?: React.CSSProperties;
}> = ({ children, color = C.teal, style }) => (
  <div
    style={{
      fontFamily: mono,
      fontSize: 21,
      letterSpacing: 3,
      textTransform: "uppercase",
      color,
      ...style,
    }}
  >
    {children}
  </div>
);

export const Frame: React.FC<{
  children: React.ReactNode;
  chapter: string;
  number: string;
  duration: number;
  staticOverlay?: React.ReactNode;
}> = ({ children, chapter, number, duration, staticOverlay }) => {
  const f = useCurrentFrame();
  return (
    <AbsoluteFill
      style={{ fontFamily: sans, color: C.ink, overflow: "hidden" }}
    >
      <Background />
      <div
        style={{
          position: "absolute",
          top: 49,
          left: 105,
          display: "flex",
          alignItems: "center",
          gap: 14,
        }}
      >
        <Logo size={38} />
        <span style={{ fontSize: 26, fontWeight: 500, letterSpacing: -0.8 }}>
          ReferralEasy
        </span>
      </div>
      <div style={{ position: "absolute", right: 110, top: 60 }}>
        <Label color={C.muted} style={{ fontSize: 17, letterSpacing: 2 }}>
          Synthetic portfolio demo
        </Label>
      </div>
      <AbsoluteFill
        style={{
          opacity: interpolate(
            f,
            [0, 12, duration - 9, duration - 1],
            [0, 1, 1, 0],
            clamp,
          ),
          translate: `0px ${interpolate(f, [0, 26], [18, 0], { ...clamp, easing: ease })}px`,
        }}
      >
        {children}
      </AbsoluteFill>
      {staticOverlay}
      <div
        style={{
          position: "absolute",
          bottom: 55,
          left: 110,
          right: 110,
          display: "flex",
          alignItems: "center",
          gap: 24,
        }}
      >
        <span style={{ fontFamily: mono, color: C.teal, fontSize: 18 }}>
          {number}
        </span>
        <span style={{ width: 70, height: 1, background: C.line }} />
        <Label color={C.muted} style={{ fontSize: 17, letterSpacing: 2 }}>
          {chapter}
        </Label>
        <span
          style={{
            marginLeft: "auto",
            fontFamily: mono,
            color: C.muted,
            fontSize: 16,
          }}
        >
          REFERRAL COORDINATION / ENGINEERED WITH CARE
        </span>
      </div>
    </AbsoluteFill>
  );
};

export const Reveal: React.FC<{
  children: React.ReactNode;
  delay?: number;
  style?: React.CSSProperties;
}> = ({ children, delay = 0, style }) => {
  const f = useCurrentFrame();
  return (
    <div
      style={{
        ...style,
        opacity: interpolate(f, [delay, delay + 22], [0, 1], clamp),
        translate: `0px ${interpolate(f, [delay, delay + 30], [28, 0], { ...clamp, easing: ease })}px`,
      }}
    >
      {children}
    </div>
  );
};

export const Pill: React.FC<{ children: React.ReactNode; color?: string }> = ({
  children,
  color = C.teal,
}) => (
  <span
    style={{
      fontFamily: mono,
      color,
      border: `1px solid ${color}55`,
      background: `${color}0d`,
      borderRadius: 6,
      padding: "10px 17px",
      fontSize: 19,
      letterSpacing: 0.4,
    }}
  >
    {children}
  </span>
);

export const Panel: React.FC<{
  children: React.ReactNode;
  style?: React.CSSProperties;
}> = ({ children, style }) => (
  <div
    style={{
      background: "linear-gradient(135deg, #112a2de8, #0a1a1de8)",
      border: `1px solid ${C.line}`,
      borderRadius: 18,
      boxShadow: "0 35px 90px #0004",
      ...style,
    }}
  >
    {children}
  </div>
);

export const CutLight: React.FC = () => {
  const f = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  return (
    <AbsoluteFill
      style={{
        pointerEvents: "none",
        background:
          "linear-gradient(100deg, transparent 35%, #8ce8d040 49%, #fff9e630 50%, transparent 60%)",
        opacity: interpolate(
          f,
          [0, durationInFrames * 0.4, durationInFrames - 1],
          [0, 0.8, 0],
        ),
        translate: `${interpolate(f, [0, durationInFrames - 1], [-1100, 1100])}px 0px`,
        mixBlendMode: "screen",
      }}
    />
  );
};
