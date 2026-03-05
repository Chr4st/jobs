"use client";

import { startEngine, stopEngine } from "../lib/api";

interface Props {
  running: boolean;
  onStatusChange: () => void;
}

export default function EngineControl({ running, onStatusChange }: Props) {
  const handleStart = async () => {
    await startEngine(40, false);
    onStatusChange();
  };

  const handleStop = async () => {
    await stopEngine();
    onStatusChange();
  };

  return (
    <div className="flex items-center gap-4">
      <div className="flex items-center gap-2">
        <div
          className={`w-2.5 h-2.5 rounded-full ${
            running
              ? "bg-green-500 animate-pulse shadow-lg shadow-green-500/50"
              : "bg-zinc-600"
          }`}
        />
        <span className="text-sm font-medium text-zinc-300">
          {running ? "Engine Running" : "Engine Idle"}
        </span>
      </div>

      {running ? (
        <button
          onClick={handleStop}
          className="px-5 py-2 bg-red-500/20 text-red-400 border border-red-500/30 
                     rounded-lg text-sm font-medium hover:bg-red-500/30 transition-all
                     hover:shadow-lg hover:shadow-red-500/10 cursor-pointer"
        >
          ■ Stop
        </button>
      ) : (
        <button
          onClick={handleStart}
          className="px-5 py-2 bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 
                     rounded-lg text-sm font-medium hover:bg-indigo-500/30 transition-all
                     hover:shadow-lg hover:shadow-indigo-500/10 cursor-pointer"
        >
          ▶ Start
        </button>
      )}
    </div>
  );
}

