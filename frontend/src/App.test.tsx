import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from './App'


function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' }
  })
}


describe('App preferences', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('shows a newly saved default model on the settings page without re-login', async () => {
    let defaultModel: string | null = null
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/auth/status') return json({ setup_required: false })
      if (path === '/api/users/me') return json({ id: 1, username: 'tester', preferences: { default_model_id: defaultModel, default_agent_count: 6, timezone: 'Asia/Shanghai' } })
      if (path === '/api/dashboard') return json({ metrics: { totals: {}, today_added: {}, source_distribution: {}, uncovered: [] }, latest_run: null })
      if (path.startsWith('/api/models')) return json({ items: [{ id: 'gpt-5.6-sol' }], total: 1, default_model_id: defaultModel, fetched_at: new Date().toISOString() })
      if (path === '/api/loop') return json({ enabled: false, interval_minutes: 1440 })
      if (path === '/api/users/me/preferences/default-model' && init?.method === 'PATCH') {
        defaultModel = JSON.parse(String(init.body)).model_id
        return json({ default_model_id: defaultModel })
      }
      if (path === '/api/credentials') return json({ items: [] })
      return json({ detail: `unexpected request: ${path}` }, 500)
    })
    vi.stubGlobal('fetch', fetchMock)

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter><App /></MemoryRouter>
      </QueryClientProvider>
    )

    await screen.findByText('持续协作任务空闲')
    fireEvent.click(screen.getByRole('link', { name: '任务编排' }))
    const modelSelect = (await screen.findByRole('option', { name: 'gpt-5.6-sol' })).parentElement as HTMLSelectElement
    fireEvent.change(modelSelect, { target: { value: 'gpt-5.6-sol' } })
    const saveDefault = screen.getByRole('button', { name: /设为默认模型/ })
    await waitFor(() => expect(saveDefault).toBeEnabled())
    fireEvent.click(saveDefault)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      '/api/users/me/preferences/default-model',
      expect.objectContaining({ method: 'PATCH' })
    ))

    fireEvent.click(screen.getByRole('link', { name: '系统设置' }))
    await waitFor(() => expect(screen.getByText('当前默认模型').parentElement).toHaveTextContent('gpt-5.6-sol'))
  })
})
