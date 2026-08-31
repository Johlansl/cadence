import { render, type RenderResult } from '@testing-library/react'
import type { ReactElement } from 'react'
import { vi } from 'vitest'
import { ConfirmProvider } from '../components/ConfirmDialog'
import { ToastProvider } from '../components/Toast'

// Components under test call useToast()/useConfirm(); wrap them in the real
// providers so toasts and the confirm dialog actually render.
export function renderWithProviders(ui: ReactElement): RenderResult {
  return render(
    <ToastProvider>
      <ConfirmProvider>{ui}</ConfirmProvider>
    </ToastProvider>,
  )
}

interface MockReply {
  status?: number
  body?: unknown
}

interface MockRequest {
  url: string
  method: string
  headers: Headers
  body: unknown
}

type MockRoute = MockReply | ((req: MockRequest) => MockReply | Promise<MockReply>)

// Replace global fetch with a router keyed by "METHOD /path" (query string
// ignored). "METHOD *" and "*" are catch-alls. Returns the vi.fn so a test can
// assert on the calls it received. Undo with vi.unstubAllGlobals().
export function installFetchMock(routes: Record<string, MockRoute>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    const path = url.split('?')[0]
    const route = routes[`${method} ${path}`] ?? routes[`${method} *`] ?? routes['*']
    if (route == null) throw new Error(`installFetchMock: no route for ${method} ${path}`)

    const req: MockRequest = {
      url,
      method,
      headers: new Headers(init?.headers),
      body: typeof init?.body === 'string' ? safeParse(init.body) : undefined,
    }
    const reply = typeof route === 'function' ? await route(req) : route
    const status = reply.status ?? 200
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: '',
      json: async () => reply.body ?? {},
    } as Response
  })
  vi.stubGlobal('fetch', fn)
  return fn
}

function safeParse(s: string): unknown {
  try {
    return JSON.parse(s)
  } catch {
    return undefined
  }
}
