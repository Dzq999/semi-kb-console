import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, streamNdjson } from './api'

function ndjsonResponse(lines: string[]): Response {
  // 把若干整行/半行 chunk 拼成一个流式 body，模拟分帧到达。
  const encoder = new TextEncoder()
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const line of lines) controller.enqueue(encoder.encode(line))
      controller.close()
    },
  })
  return new Response(stream, { status: 200 })
}

describe('streamNdjson', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('parses events across chunk boundaries', async () => {
    // 第二个事件被拆到两个 chunk 中间，验证半帧缓冲。
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse([
      '{"type":"stage","key":"grounding"}\n{"type":"tok',
      'en","text":"你好"}\n',
      '{"type":"done","message":{"id":1}}\n',
    ])))
    const events: Record<string, unknown>[] = []
    await streamNdjson('/x', { q: 1 }, (e) => events.push(e))
    expect(events.map((e) => e.type)).toEqual(['stage', 'token', 'done'])
    expect(events[1].text).toBe('你好')
  })

  it('handles a final line without trailing newline', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(ndjsonResponse(['{"type":"done"}'])))
    const events: Record<string, unknown>[] = []
    await streamNdjson('/x', {}, (e) => events.push(e))
    expect(events).toEqual([{ type: 'done' }])
  })

  it('throws ApiError on non-ok response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '坏了' }), { status: 502 })))
    await expect(streamNdjson('/x', {}, () => {})).rejects.toEqual(
      expect.objectContaining({ status: 502, message: '坏了' } satisfies Partial<ApiError>),
    )
  })
})

describe('api client', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('parses JSON and sends credentials', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(api('/api/health')).resolves.toEqual({ ok: true })
    expect(fetchMock).toHaveBeenCalledWith('/api/health', expect.objectContaining({ credentials: 'include' }))
  })

  it('raises a useful API error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: '配置缺失' }), { status: 424 })))
    await expect(api('/api/models')).rejects.toEqual(expect.objectContaining({ status: 424, message: '配置缺失' } satisfies Partial<ApiError>))
  })
})
