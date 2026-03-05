"use client";

import { Stats } from "../lib/api";

const STAGE_CONFIG: Record<string, { label: string; color: string; icon: string }> = {
  DISCOVERED: { label: "Discovered", color: "#6366f1", icon: "🔍" },
  NEEDS_USER_DATA: { label: "Needs Data", color: "#f97316", icon: "✏️" },
  APPLYING: { label: "Applying", color: "#f59e0b", icon: "⏳" },
  APPLIED: { label: "Applied", color: "#22c55e", icon: "✓" },
  OA_RECEIVED: { label: "OA Received", color: "#06b6d4", icon: "📧" },
  OA_COMPLETED: { label: "OA Done", color: "#8b5cf6", icon: "✅" },
  INTERVIEW_SCHEDULED: { label: "Interview", color: "#ec4899", icon: "📅" },
  INTERVIEW_DONE: { label: "Interviewed", color: "#14b8a6", icon: "🎤" },
  OFFER: { label: "Offer", color: "#22c55e", icon: "🎉" },
  REJECTED: { label: "Rejected", color: "#ef4444", icon: "✗" },
  ERROR: { label: "Error", color: "#ef4444", icon: "⚠" },
};

export default function StatsBar({ stats }: { stats: Stats | null }) {
  if (!stats) return null;

  const stages = stats.stages || {};

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-5 lg:grid-cols-10 gap-3">
      {Object.entries(STAGE_CONFIG).map(([key, cfg]) => (
        <div
          key={key}
          className="rounded-xl p-3 text-center transition-all hover:scale-105"
          style={{
            background: `${cfg.color}15`,
            border: `1px solid ${cfg.color}30`,
          }}
        >
          <div className="text-lg">{cfg.icon}</div>
          <div
            className="text-2xl font-bold tabular-nums"
            style={{ color: cfg.color }}
          >
            {stages[key] || 0}
          </div>
          <div className="text-xs text-zinc-400 mt-1">{cfg.label}</div>
        </div>
      ))}
    </div>
  );
}

