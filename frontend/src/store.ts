import { create } from 'zustand'
import type { RunInfo } from './api'

interface AppState {
  latestRun: RunInfo | null
  notice: string
  setLatestRun: (run: RunInfo | null) => void
  setNotice: (notice: string) => void
}

export const useAppStore = create<AppState>((set) => ({
  latestRun: null,
  notice: '',
  setLatestRun: (latestRun) => set({ latestRun }),
  setNotice: (notice) => set({ notice })
}))

