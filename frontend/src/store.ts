import { create } from 'zustand'
import type { RunInfo } from './api'

interface AppState {
  latestRun: RunInfo | null
  notice: string
  setLatestRun: (run: RunInfo | null) => void
  setNotice: (notice: string) => void
}

let noticeTimer: ReturnType<typeof setTimeout> | null = null

export const useAppStore = create<AppState>((set) => ({
  latestRun: null,
  notice: '',
  setLatestRun: (latestRun) => set({ latestRun }),
  setNotice: (notice) => {
    if (noticeTimer) {
      clearTimeout(noticeTimer)
      noticeTimer = null
    }
    set({ notice })
    if (notice) {
      noticeTimer = setTimeout(() => {
        set({ notice: '' })
        noticeTimer = null
      }, 2000)
    }
  }
}))
