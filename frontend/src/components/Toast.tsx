import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { TONE_TEXT, type Tone } from '../lib/pill'

type ToastKind = 'success' | 'error' | 'info'

interface Toast {
  id: number
  kind: ToastKind
  message: string
}

const KIND_TONE: Record<ToastKind, Tone> = {
  success: 'ok',
  error: 'danger',
  info: 'info',
}

const TIMEOUT_MS: Record<ToastKind, number> = {
  success: 4000,
  info: 4000,
  error: 8000,
}

interface ToastApi {
  notify: (kind: ToastKind, message: string) => void
}

const ToastContext = createContext<ToastApi | null>(null)

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within <ToastProvider>')
  return ctx
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => {
    setToasts((ts) => ts.filter((t) => t.id !== id))
  }, [])

  const notify = useCallback(
    (kind: ToastKind, message: string) => {
      const id = nextId.current++
      setToasts((ts) => [...ts, { id, kind, message }])
      window.setTimeout(() => dismiss(id), TIMEOUT_MS[kind])
    },
    [dismiss],
  )

  const api = useMemo(() => ({ notify }), [notify])

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div
        className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
        aria-live="polite"
        aria-atomic="false"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            role={t.kind === 'error' ? 'alert' : 'status'}
            className="pointer-events-auto flex items-start gap-2 rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-xs text-zinc-200 shadow-lg shadow-black/40"
          >
            <span className={`mt-0.5 shrink-0 ${TONE_TEXT[KIND_TONE[t.kind]]}`} aria-hidden="true">
              {t.kind === 'success' ? '✓' : t.kind === 'error' ? '✕' : 'ℹ'}
            </span>
            <span className="min-w-0 flex-1 whitespace-pre-wrap break-words">{t.message}</span>
            <button
              type="button"
              onClick={() => dismiss(t.id)}
              className="shrink-0 text-zinc-500 hover:text-zinc-300"
              aria-label="Dismiss notification"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}
