"use client";

import { EngineEvent } from "../lib/api";

function eventToString(event: EngineEvent): string {
  switch (event.type) {
    case "engine":
      if (event.status === "started") return "🚀 Engine started";
      if (event.status === "scanning") return "🔍 Scanning Greenhouse boards...";
      if (event.status === "discovered")
        return `📋 Found ${event.count} intern roles`;
      if (event.status === "completed") return "✅ Engine completed";
      if (event.status === "stopped_by_user") return "⏹ Stopped by user";
      if (event.status === "error") return `❌ Engine error: ${event.error}`;
      return `Engine: ${event.status}`;
    case "applying":
      return `⏳ Applying to ${event.title} at ${event.company}`;
    case "applied":
      return `✓ Applied to ${event.title} at ${event.company}`;
    case "captcha":
      return `⚠ CAPTCHA at ${event.company}: ${event.reason}`;
    case "error":
      return `✗ Error at ${event.company}: ${event.error}`;
    default:
      return JSON.stringify(event);
  }
}

function eventColor(event: EngineEvent): string {
  if (event.type === "applied") return "text-green-400";
  if (event.type === "captcha") return "text-amber-400";
  if (event.type === "error" || event.status === "error") return "text-red-400";
  if (event.type === "engine" && event.status === "completed")
    return "text-green-400";
  return "text-zinc-400";
}

export default function ActivityFeed({ events }: { events: EngineEvent[] }) {
  return (
    <div
      className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4"
      style={{ maxHeight: 300, overflowY: "auto" }}
    >
      <h3 className="text-sm font-semibold text-zinc-300 mb-3">
        Live Activity
      </h3>
      {events.length === 0 ? (
        <p className="text-xs text-zinc-500 italic">
          No activity yet. Start the engine to begin.
        </p>
      ) : (
        <div className="space-y-1.5">
          {events.map((event, i) => (
            <div key={i} className={`text-xs font-mono ${eventColor(event)}`}>
              {eventToString(event)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

