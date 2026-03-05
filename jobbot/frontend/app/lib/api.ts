const API_BASE = "http://localhost:8000";

export interface Application {
  app_id: string;
  dedup_key: string;
  status: string;
  stage: string;
  resume_version: string;
  submission_proof: string | null;
  applied_at: string | null;
  created_at: string;
  updated_at: string;
  notes: string;
  job_url: string;
  company: string;
  role_title: string;
  role_family: string;
  location: string;
  match_score: number;
}

export interface Stats {
  stages: Record<string, number>;
  total: number;
}

export interface EngineEvent {
  type: string;
  status?: string;
  company?: string;
  title?: string;
  app_id?: string;
  error?: string;
  count?: number;
  summary?: Record<string, unknown>;
  reason?: string;
  role_family?: string;
}

export async function fetchApplications(): Promise<{
  applications: Application[];
  count: number;
}> {
  const res = await fetch(`${API_BASE}/api/applications`);
  return res.json();
}

export async function fetchStats(): Promise<Stats> {
  const res = await fetch(`${API_BASE}/api/stats`);
  return res.json();
}

export async function fetchEngineStatus(): Promise<{ running: boolean }> {
  const res = await fetch(`${API_BASE}/api/engine/status`);
  return res.json();
}

export async function startEngine(
  maxJobs = 40,
  dryRun = false
): Promise<{ success?: boolean; error?: string }> {
  const res = await fetch(`${API_BASE}/api/engine/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_jobs: maxJobs, dry_run: dryRun }),
  });
  return res.json();
}

export async function stopEngine(): Promise<{
  success?: boolean;
  error?: string;
}> {
  const res = await fetch(`${API_BASE}/api/engine/stop`, {
    method: "POST",
  });
  return res.json();
}

export async function updateStage(
  appId: string,
  stage: string,
  notes = ""
): Promise<{ success?: boolean }> {
  const res = await fetch(`${API_BASE}/api/applications/${appId}/stage`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage, notes }),
  });
  return res.json();
}

// ---------- Schemas ----------
export interface FormField {
  field_key: string;
  field_type: string;
  label: string;
  required: boolean;
  options: string[];
  html_name: string;
  html_id: string;
}

export interface FormSchema {
  schema_id: string;
  schema_hash: string;
  job_url: string;
  company: string;
  fields: FormField[];
  created_at: string;
}

export async function fetchSchemas(): Promise<{
  schemas: FormSchema[];
  count: number;
}> {
  const res = await fetch(`${API_BASE}/api/schemas`);
  return res.json();
}

export async function fetchSchema(
  schemaHash: string
): Promise<{ schema: FormSchema }> {
  const res = await fetch(`${API_BASE}/api/schemas/${schemaHash}`);
  return res.json();
}

// ---------- Mappings ----------
export interface FieldMapping {
  mapping_id: string;
  field_key: string;
  scope: string;
  scope_value: string;
  answer_value: string;
  created_at: string;
  updated_at: string;
}

export async function fetchMappings(
  fieldKey?: string,
  scope?: string
): Promise<{ mappings: FieldMapping[]; count: number }> {
  const params = new URLSearchParams();
  if (fieldKey) params.set("field_key", fieldKey);
  if (scope) params.set("scope", scope);
  const res = await fetch(`${API_BASE}/api/mappings?${params}`);
  return res.json();
}

export async function createMapping(data: {
  field_key: string;
  answer_value: string;
  scope?: string;
  scope_value?: string;
}): Promise<{ success: boolean; mapping_id: string }> {
  const res = await fetch(`${API_BASE}/api/mappings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  return res.json();
}

export async function deleteMapping(
  mappingId: string
): Promise<{ success: boolean }> {
  const res = await fetch(`${API_BASE}/api/mappings/${mappingId}`, {
    method: "DELETE",
  });
  return res.json();
}

export function connectWebSocket(
  onMessage: (event: EngineEvent) => void
): WebSocket {
  const ws = new WebSocket("ws://localhost:8000/ws");
  ws.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      onMessage(data);
    } catch {
      /* ignore */
    }
  };
  ws.onopen = () => {
    // Keep alive ping every 30s
    const interval = setInterval(() => {
      if (ws.readyState === WebSocket.OPEN) ws.send("ping");
      else clearInterval(interval);
    }, 30000);
  };
  return ws;
}

