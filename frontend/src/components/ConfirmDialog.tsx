import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'

interface ConfirmOptions {
  title: string
  body?: string
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>

const ConfirmContext = createContext<ConfirmFn | null>(null)

export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmContext)
  if (!ctx) throw new Error('useConfirm must be used within <ConfirmProvider>')
  return ctx
}

interface Pending extends ConfirmOptions {
  resolve: (ok: boolean) => void
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null)
  const confirmBtn = useRef<HTMLButtonElement>(null)

  const confirm = useCallback<ConfirmFn>(
    (opts) => new Promise<boolean>((resolve) => setPending({ ...opts, resolve })),
    [],
  )

  const settle = useCallback((ok: boolean) => {
    setPending((p) => {
      p?.resolve(ok)
      return null
    })
  }, [])

  useEffect(() => {
    if (!pending) return
    confirmBtn.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') settle(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pending, settle])

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {pending && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => settle(false)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label={pending.title}
            className="w-full max-w-sm rounded-lg border border-zinc-700 bg-zinc-900 p-4 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <p className="text-sm font-medium text-zinc-100">{pending.title}</p>
            {pending.body && <p className="mt-1 text-xs text-zinc-400">{pending.body}</p>}
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => settle(false)}
                className="rounded border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
              >
                {pending.cancelLabel ?? 'Cancel'}
              </button>
              <button
                ref={confirmBtn}
                type="button"
                onClick={() => settle(true)}
                className={`rounded px-3 py-1 text-xs font-medium ${
                  pending.danger
                    ? 'bg-red-500 text-white hover:bg-red-400'
                    : 'bg-zinc-100 text-zinc-900 hover:bg-white'
                }`}
              >
                {pending.confirmLabel ?? 'Confirm'}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  )
}
