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

export async function streamNdjson(
  path: string,
  body: unknown,
  onEvent: (event: Record<string, unknown>) => void,
): Promise<void> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }))
    const msg = Array.isArray(detail.detail) ? detail.detail.map((i: { msg?: string }) => i.msg).join('；') : detail.detail
    throw new ApiError(response.status, msg || `HTTP ${response.status}`)
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  // NDJSON：按换行切帧，最后一段可能是半行，留到下一次拼接。
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let index = buffer.indexOf('\n')
    while (index >= 0) {
      const line = buffer.slice(0, index).trim()
      buffer = buffer.slice(index + 1)
      if (line) {
        try {
          onEvent(JSON.parse(line))
        } catch {
          // 半帧或噪声行，忽略。
        }
      }
      index = buffer.indexOf('\n')
    }
  }
  const tail = buffer.trim()
  if (tail) {
    try {
      onEvent(JSON.parse(tail))
    } catch {
      // 收尾无有效 JSON，忽略。
    }
  }
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
  max_consecutive_round_failures?: number
  auto_repair?: boolean
  max_auto_repair_attempts?: number
  repair_follow_failure_threshold?: boolean
  stop_after_round?: number | null
  orchestrator_engine: 'legacy' | 'langgraph' | string
  checkpoint_backend: 'memory' | 'postgres' | string
  checkpoint_thread_id?: string | null
  heartbeat_at?: string | null
  recovery_count: number
  pause_requested: boolean
  cancel_requested: boolean
  agents: Array<{ id: string; name: string; role: string; domain: string; source_mode: SourceMode; status: string; duration_seconds?: number; error?: string }>
}

export interface RunReference {
  title: string
  url: string
  source_type: string
  fetch_status: string
  excerpt: string
  retrieved_at?: string | null
  provenance: Array<{ round: number; agent_id: string; agent_name: string }>
}
