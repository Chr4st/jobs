"use client";

import { Application } from "../lib/api";
import { useState } from "react";

const STAGE_BADGES: Record<string, { bg: string; text: string }> = {
  DISCOVERED: { bg: "bg-indigo-500/20", text: "text-indigo-400" },
  NEEDS_USER_DATA: { bg: "bg-orange-500/20", text: "text-orange-400" },
  APPLYING: { bg: "bg-amber-500/20", text: "text-amber-400" },
  APPLIED: { bg: "bg-green-500/20", text: "text-green-400" },
  OA_RECEIVED: { bg: "bg-cyan-500/20", text: "text-cyan-400" },
  OA_COMPLETED: { bg: "bg-violet-500/20", text: "text-violet-400" },
  INTERVIEW_SCHEDULED: { bg: "bg-pink-500/20", text: "text-pink-400" },
  INTERVIEW_DONE: { bg: "bg-teal-500/20", text: "text-teal-400" },
  OFFER: { bg: "bg-emerald-500/20", text: "text-emerald-400" },
  REJECTED: { bg: "bg-red-500/20", text: "text-red-400" },
  ERROR: { bg: "bg-red-500/20", text: "text-red-400" },
  NEEDS_HUMAN: { bg: "bg-orange-500/20", text: "text-orange-400" },
  SUBMITTED: { bg: "bg-green-500/20", text: "text-green-400" },
};

const ROLE_BADGES: Record<string, string> = {
  fullstack: "🖥",
  ml: "🤖",
  quant: "📊",
  founding: "🚀",
};

interface Props {
  applications: Application[];
  filter: string;
  onFilterChange: (f: string) => void;
}

export default function AppTable({ applications, filter, onFilterChange }: Props) {
  const [sortCol, setSortCol] = useState<string>("created_at");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const filtered = applications.filter((app) => {
    if (filter === "ALL") return true;
    return app.stage === filter || app.status === filter;
  });

  const sorted = [...filtered].sort((a, b) => {
    const aVal = (a as Record<string, unknown>)[sortCol] ?? "";
    const bVal = (b as Record<string, unknown>)[sortCol] ?? "";
    const cmp = String(aVal).localeCompare(String(bVal));
    return sortDir === "asc" ? cmp : -cmp;
  });

  const toggleSort = (col: string) => {
    if (sortCol === col) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortCol(col); setSortDir("desc"); }
  };

  const badge = (stage: string) => {
    const cfg = STAGE_BADGES[stage] || { bg: "bg-zinc-700/50", text: "text-zinc-400" };
    return (
      <span className={`${cfg.bg} ${cfg.text} px-2 py-0.5 rounded-md text-xs font-medium`}>
        {stage}
      </span>
    );
  };

  const SortHeader = ({ col, label }: { col: string; label: string }) => (
    <th
      className="px-3 py-2.5 text-left text-xs font-medium text-zinc-400 cursor-pointer hover:text-zinc-200 select-none"
      onClick={() => toggleSort(col)}
    >
      {label} {sortCol === col ? (sortDir === "asc" ? "↑" : "↓") : ""}
    </th>
  );

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/50 overflow-hidden">
      {/* Filter tabs */}
      <div className="flex items-center gap-1 px-4 py-3 border-b border-zinc-800 overflow-x-auto">
        {["ALL", "DISCOVERED", "NEEDS_USER_DATA", "APPLYING", "APPLIED", "OA_RECEIVED", "INTERVIEW_SCHEDULED", "OFFER", "REJECTED", "ERROR"].map((f) => (
          <button
            key={f}
            onClick={() => onFilterChange(f)}
            className={`px-3 py-1 rounded-md text-xs font-medium transition-all cursor-pointer whitespace-nowrap ${
              filter === f
                ? "bg-indigo-500/20 text-indigo-400 border border-indigo-500/30"
                : "text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800"
            }`}
          >
            {f === "ALL" ? `All (${applications.length})` : `${f} (${applications.filter(a => a.stage === f || a.status === f).length})`}
          </button>
        ))}
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-800">
              <SortHeader col="company" label="Company" />
              <SortHeader col="role_title" label="Role" />
              <th className="px-3 py-2.5 text-left text-xs font-medium text-zinc-400">Type</th>
              <SortHeader col="stage" label="Stage" />
              <SortHeader col="location" label="Location" />
              <SortHeader col="created_at" label="Date" />
              <th className="px-3 py-2.5 text-left text-xs font-medium text-zinc-400">Link</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((app) => (
              <tr
                key={app.app_id}
                className="border-b border-zinc-800/50 hover:bg-zinc-800/30 transition-colors"
              >
                <td className="px-3 py-2.5 font-medium text-zinc-200">{app.company}</td>
                <td className="px-3 py-2.5 text-zinc-300 max-w-[300px] truncate">{app.role_title}</td>
                <td className="px-3 py-2.5">{ROLE_BADGES[app.role_family] || "💼"} <span className="text-xs text-zinc-500">{app.role_family}</span></td>
                <td className="px-3 py-2.5">{badge(app.stage || app.status)}</td>
                <td className="px-3 py-2.5 text-zinc-400 text-xs">{app.location || "—"}</td>
                <td className="px-3 py-2.5 text-zinc-500 text-xs tabular-nums">
                  {new Date(app.created_at).toLocaleDateString()}
                </td>
                <td className="px-3 py-2.5">
                  <a href={app.job_url} target="_blank" rel="noopener noreferrer"
                     className="text-indigo-400 hover:text-indigo-300 text-xs">
                    Open ↗
                  </a>
                </td>
              </tr>
            ))}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={7} className="px-3 py-8 text-center text-zinc-500 text-sm">
                  No applications found{filter !== "ALL" ? ` for stage "${filter}"` : ""}.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

