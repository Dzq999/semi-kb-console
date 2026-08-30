import { act } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useAppStore } from './store'

describe('global notices', () => {
  afterEach(() => {
    vi.useRealTimers()
    act(() => useAppStore.getState().setNotice(''))
  })

  it('automatically clears a notice after two seconds', () => {
    vi.useFakeTimers()
    act(() => useAppStore.getState().setNotice('操作成功'))
    expect(useAppStore.getState().notice).toBe('操作成功')

    act(() => vi.advanceTimersByTime(1999))
    expect(useAppStore.getState().notice).toBe('操作成功')

    act(() => vi.advanceTimersByTime(1))
    expect(useAppStore.getState().notice).toBe('')
  })
})
