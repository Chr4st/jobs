"use client";

import { useEffect, useState, useCallback, useRef } from "react";
import {
  fetchApplications,
  fetchStats,
  fetchEngineStatus,
  connectWebSocket,
  Application,
  Stats,
  EngineEvent,
} from "./lib/api";
import StatsBar from "./components/StatsBar";
import EngineControl from "./components/EngineControl";
import ActivityFeed from "./components/ActivityFeed";
import AppTable from "./components/AppTable";
import MappingsPanel from "./components/MappingsPanel";

export default function Dashboard() {
  const [applications, setApplications] = useState<Application[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [engineRunning, setEngineRunning] = useState(false);
  const [events, setEvents] = useState<EngineEvent[]>([]);
  const [filter, setFilter] = useState("ALL");
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);

  const refreshData = useCallback(async () => {
    try {
      const [appData, statsData, statusData] = await Promise.all([
        fetchApplications(),
        fetchStats(),
        fetchEngineStatus(),
      ]);
      setApplications(appData.applications);
      setStats(statsData);
      setEngineRunning(statusData.running);
    } catch {
      /* API not up yet */
    }
  }, []);

  // Initial load + polling
  useEffect(() => {
    refreshData();
    const interval = setInterval(refreshData, 5000);
    return () => clearInterval(interval);
  }, [refreshData]);

  // WebSocket
  useEffect(() => {
    const ws = connectWebSocket((event) => {
      setEvents((prev) => [event, ...prev].slice(0, 100));
      if (
        event.type === "applied" ||
        event.type === "engine" ||
        event.type === "error"
      ) {
        refreshData();
      }
      if (event.type === "engine") {
        if (event.status === "started") setEngineRunning(true);
        if (
          event.status === "completed" ||
          event.status === "stopped_by_user" ||
          event.status === "error"
        )
          setEngineRunning(false);
      }
    });
    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    wsRef.current = ws;
    return () => ws.close();
  }, [refreshData]);

  return (
    <div className="min-h-screen p-6 max-w-[1400px] mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-zinc-100 tracking-tight">
            JobBot
          </h1>
          <p className="text-xs text-zinc-500 mt-0.5">
            Automated internship pipeline
            {connected ? (
              <span className="ml-2 text-green-500">● connected</span>
            ) : (
              <span className="ml-2 text-red-500">● disconnected</span>
            )}
          </p>
        </div>
        <EngineControl running={engineRunning} onStatusChange={refreshData} />
      </div>

      {/* Stats */}
      <div className="mb-6">
        <StatsBar stats={stats} />
      </div>

      {/* Main grid: Table + Sidebar */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <AppTable
            applications={applications}
            filter={filter}
            onFilterChange={setFilter}
          />
        </div>
        <div className="space-y-6">
          <ActivityFeed events={events} />
          <MappingsPanel />
        </div>
      </div>
    </div>
  );
}
