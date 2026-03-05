"use client";

import { useEffect, useState, useCallback } from "react";
import {
  fetchMappings,
  fetchSchemas,
  createMapping,
  deleteMapping,
  FieldMapping,
  FormSchema,
} from "../lib/api";

export default function MappingsPanel() {
  const [mappings, setMappings] = useState<FieldMapping[]>([]);
  const [schemas, setSchemas] = useState<FormSchema[]>([]);
  const [tab, setTab] = useState<"mappings" | "schemas">("mappings");
  const [newKey, setNewKey] = useState("");
  const [newVal, setNewVal] = useState("");
  const [expandedSchema, setExpandedSchema] = useState<string | null>(null);

  const loadData = useCallback(async () => {
    try {
      const [m, s] = await Promise.all([fetchMappings(), fetchSchemas()]);
      setMappings(m.mappings);
      setSchemas(s.schemas);
    } catch {
      /* api not up */
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleAdd = async () => {
    if (!newKey.trim() || !newVal.trim()) return;
    await createMapping({ field_key: newKey.trim(), answer_value: newVal.trim() });
    setNewKey("");
    setNewVal("");
    loadData();
  };

  const handleDelete = async (id: string) => {
    await deleteMapping(id);
    loadData();
  };

  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/50 overflow-hidden">
      {/* Tabs */}
      <div className="flex border-b border-zinc-800">
        {(["mappings", "schemas"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`px-4 py-2.5 text-xs font-medium transition-all cursor-pointer ${
              tab === t
                ? "text-indigo-400 border-b-2 border-indigo-400 bg-indigo-500/5"
                : "text-zinc-500 hover:text-zinc-300"
            }`}
          >
            {t === "mappings" ? `📋 Mappings (${mappings.length})` : `🔧 Schemas (${schemas.length})`}
          </button>
        ))}
      </div>

      {tab === "mappings" && (
        <div className="p-4">
          {/* Add new mapping */}
          <div className="flex gap-2 mb-4">
            <input
              value={newKey}
              onChange={(e) => setNewKey(e.target.value)}
              placeholder="field_key"
              className="flex-1 px-3 py-1.5 bg-zinc-800 border border-zinc-700 rounded-lg text-xs text-zinc-200 placeholder:text-zinc-600 focus:border-indigo-500 outline-none"
            />
            <input
              value={newVal}
              onChange={(e) => setNewVal(e.target.value)}
              placeholder="answer_value"
              className="flex-1 px-3 py-1.5 bg-zinc-800 border border-zinc-700 rounded-lg text-xs text-zinc-200 placeholder:text-zinc-600 focus:border-indigo-500 outline-none"
            />
            <button
              onClick={handleAdd}
              className="px-3 py-1.5 bg-indigo-500/20 text-indigo-400 border border-indigo-500/30 rounded-lg text-xs font-medium hover:bg-indigo-500/30 cursor-pointer"
            >
              + Add
            </button>
          </div>
          {/* Mapping list */}
          <div className="space-y-1.5 max-h-[300px] overflow-y-auto">
            {mappings.map((m) => (
              <div key={m.mapping_id} className="flex items-center justify-between px-3 py-2 bg-zinc-800/50 rounded-lg">
                <div className="flex-1 min-w-0">
                  <span className="text-xs font-mono text-indigo-400">{m.field_key}</span>
                  <span className="text-zinc-600 mx-2">→</span>
                  <span className="text-xs text-zinc-300">{m.answer_value}</span>
                  {m.scope !== "global" && (
                    <span className="ml-2 text-[10px] px-1.5 py-0.5 bg-amber-500/20 text-amber-400 rounded">
                      {m.scope}: {m.scope_value}
                    </span>
                  )}
                </div>
                <button onClick={() => handleDelete(m.mapping_id)} className="text-zinc-600 hover:text-red-400 text-xs ml-2 cursor-pointer">✕</button>
              </div>
            ))}
            {mappings.length === 0 && (
              <p className="text-xs text-zinc-500 italic text-center py-4">No mappings yet. Add answers for form fields above.</p>
            )}
          </div>
        </div>
      )}

      {tab === "schemas" && (
        <div className="p-4 space-y-2 max-h-[400px] overflow-y-auto">
          {schemas.map((s) => (
            <div key={s.schema_hash} className="bg-zinc-800/50 rounded-lg overflow-hidden">
              <button
                onClick={() => setExpandedSchema(expandedSchema === s.schema_hash ? null : s.schema_hash)}
                className="w-full flex items-center justify-between px-3 py-2 text-left cursor-pointer hover:bg-zinc-800"
              >
                <div>
                  <span className="text-xs font-medium text-zinc-200">{s.company}</span>
                  <span className="ml-2 text-[10px] text-zinc-500 font-mono">{s.schema_hash.slice(0, 8)}…</span>
                  <span className="ml-2 text-[10px] text-zinc-500">{s.fields.length} fields</span>
                </div>
                <span className="text-zinc-500 text-xs">{expandedSchema === s.schema_hash ? "▼" : "▶"}</span>
              </button>
              {expandedSchema === s.schema_hash && (
                <div className="px-3 pb-3 space-y-1">
                  {s.fields.map((f, i) => (
                    <div key={i} className="flex items-center gap-2 text-[11px]">
                      <span className="text-indigo-400 font-mono w-40 truncate">{f.field_key}</span>
                      <span className="text-zinc-500 w-16">{f.field_type}</span>
                      <span className="text-zinc-400 flex-1 truncate">{f.label}</span>
                      {f.required && <span className="text-red-400 text-[10px]">req</span>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
          {schemas.length === 0 && (
            <p className="text-xs text-zinc-500 italic text-center py-4">No schemas extracted yet. Run the engine to discover form schemas.</p>
          )}
        </div>
      )}
    </div>
  );
}

