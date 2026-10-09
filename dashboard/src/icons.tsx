import type { ReactNode } from "react";

function Svg({ children }: { children: ReactNode }) {
  return (
    <svg className="icon" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false"
      fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      {children}
    </svg>
  );
}

export const IconCheck = () => <Svg><path d="M3 8.5l3.2 3L13 4.5" /></Svg>;
export const IconAlert = () => <Svg><path d="M8 2l6.5 11.5h-13z" /><path d="M8 6.5v3M8 11.6v.1" /></Svg>;
export const IconQuestion = () => <Svg><circle cx="8" cy="8" r="6" /><path d="M6.3 6.2a1.8 1.8 0 113 1.2c-.7.5-1.3.8-1.3 1.6M8 11.4v.1" /></Svg>;
export const IconDash = () => <Svg><circle cx="8" cy="8" r="6" /><path d="M5.5 8h5" /></Svg>;
export const IconX = () => <Svg><path d="M4 4l8 8M12 4l-8 8" /></Svg>;
export const IconLog = () => <Svg><path d="M2.5 4h11M2.5 8h11M2.5 12h7" /></Svg>;
export const IconPulse = () => <Svg><path d="M1.5 8h3l1.8-5 3.4 10 1.8-5h3" /></Svg>;
export const IconMetric = () => <Svg><path d="M3 13V8M8 13V3M13 13V6" /></Svg>;
export const IconPath = () => <Svg><circle cx="3.5" cy="12.5" r="1.5" /><circle cx="12.5" cy="3.5" r="1.5" /><path d="M5 12.5h3.5a2 2 0 002-2v-5" /></Svg>;
export const IconBolt = () => <Svg><path d="M9 1.5L3.5 9H8l-1 5.5L12.5 7H8z" /></Svg>;
export const IconTrace = () => <Svg><path d="M2 3h8M4 7h8M6 11h8" /></Svg>;

export function kindIcon(kind: string) {
  if (kind === "log") return <IconLog />;
  if (kind === "anomaly") return <IconPulse />;
  if (kind === "metric") return <IconMetric />;
  if (kind === "span" || kind === "trace") return <IconTrace />;
  return <IconBolt />;
}
