export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'include',
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) }
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    const detail = Array.isArray(body.detail) ? body.detail.map((item: { msg?: string }) => item.msg).join('；') : body.detail
    throw new ApiError(response.status, detail || `HTTP ${response.status}`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export type SourceMode = 'web' | 'model_prior' | 'hybrid'

export interface AgentConfig {
  name: string
  role: string
  domain: string
  objective: string
  source_mode: SourceMode
  model_override?: string | null
  timeout_seconds: number
  max_retries: number
}

export interface RunInfo {
  id: string
  model_id: string
  status: string
  current_stage: string
  progress: number
  created_at: string
  started_at?: string
  completed_at?: string
  error?: string
  current_round: number
  rounds_completed: number
  continuous: boolean
  publish_changes: boolean
  round_interval_seconds: number
  stop_after_round?: number | null
  agents: Array<{ id: string; name: string; role: string; domain: string; source_mode: SourceMode; status: string; duration_seconds?: number; error?: string }>
}
