import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api } from './api'

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
