import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AdminWriteResult } from '../api/client'
import { installFetchMock } from '../test/harness'
import { useAdminKeyAction } from './AdminKeyPrompt'
import { SessionButton, SessionProvider, useSession } from './SessionAuth'

const ME_URL = 'GET /api/v1/auth/me'

const realLocation = window.location

afterEach(() => {
  Object.defineProperty(window, 'location', {
    value: realLocation,
    writable: true,
    configurable: true,
  })
  vi.unstubAllGlobals()
})

function stubAssign() {
  const assignMock = vi.fn()
  Object.defineProperty(window, 'location', {
    value: { assign: assignMock },
    writable: true,
    configurable: true,
  })
  return assignMock
}

describe('SessionAuth', () => {
  it('hides SSO UI entirely when the backend has OIDC disabled', async () => {
    installFetchMock({ [ME_URL]: { status: 404 } })
    render(
      <SessionProvider>
        <SessionButton />
      </SessionProvider>,
    )
    await waitFor(() => expect(screen.queryByRole('button')).not.toBeInTheDocument())
  })

  it('offers sign-in when logged out and navigates to the login endpoint', async () => {
    installFetchMock({ [ME_URL]: { body: { authenticated: false } } })
    const assignMock = stubAssign()
    render(
      <SessionProvider>
        <SessionButton />
      </SessionProvider>,
    )
    const button = await screen.findByRole('button', { name: 'Sign in with SSO' })
    await userEvent.click(button)
    expect(assignMock).toHaveBeenCalledWith(expect.stringContaining('/api/v1/auth/oidc/login'))
  })

  it('shows the verified actor and signs out', async () => {
    let signedIn = true
    const fetchMock = installFetchMock({
      [ME_URL]: () =>
        signedIn
          ? { body: { authenticated: true, actor: 'johlan@example.com' } }
          : { body: { authenticated: false } },
      ['POST /api/v1/auth/logout']: () => {
        signedIn = false
        return { body: { ok: true } }
      },
    })
    render(
      <SessionProvider>
        <SessionButton />
      </SessionProvider>,
    )
    expect(await screen.findByText('johlan@example.com')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    await screen.findByRole('button', { name: 'Sign in with SSO' })
    const methods = fetchMock.mock.calls.map((c) => String(c[1] && (c[1] as RequestInit).method))
    expect(methods).toContain('POST')
  })

  it('runs admin actions on an operator session without asking for a key', async () => {
    const seen: string[] = []
    installFetchMock({
      [ME_URL]: { body: { authenticated: true, actor: 'op@example.com', role: 'operator' } },
    })
    function Probe() {
      const a = useAdminKeyAction(async (key: string): Promise<AdminWriteResult<null>> => {
        seen.push(key)
        return { ok: true, status: 200, data: null }
      })
      const { loading } = useSession()
      // findByRole below waits for the session to load before clicking.
      if (loading) return <p>loading</p>
      return (
        <>
          <button onClick={() => void a.run()}>go</button>
          {a.error && <p>{a.error}</p>}
        </>
      )
    }
    render(
      <SessionProvider>
        <Probe />
      </SessionProvider>,
    )
    await userEvent.click(await screen.findByRole('button', { name: 'go' }))
    await waitFor(() => expect(seen).toEqual(['']))
    expect(screen.queryByPlaceholderText(/admin key/i)).not.toBeInTheDocument()
  })

  it('refuses admin actions on a reader session without calling the backend', async () => {
    const seen: string[] = []
    installFetchMock({
      [ME_URL]: { body: { authenticated: true, actor: 'ro@example.com', role: 'reader' } },
    })
    function Probe() {
      const a = useAdminKeyAction(async (key: string): Promise<AdminWriteResult<null>> => {
        seen.push(key)
        return { ok: true, status: 200, data: null }
      })
      const { loading } = useSession()
      if (loading) return <p>loading</p>
      return (
        <>
          <button onClick={() => void a.run()}>go</button>
          {a.error && <p>{a.error}</p>}
        </>
      )
    }
    render(
      <SessionProvider>
        <Probe />
      </SessionProvider>,
    )
    await userEvent.click(await screen.findByRole('button', { name: 'go' }))
    await screen.findByText(/reader role/i)
    expect(seen).toEqual([])
    expect(screen.queryByPlaceholderText(/admin key/i)).not.toBeInTheDocument()
  })

  it('refuses admin actions on a pre-RBAC session without a role', async () => {
    const seen: string[] = []
    installFetchMock({ [ME_URL]: { body: { authenticated: true, actor: 'old@example.com' } } })
    function Probe() {
      const a = useAdminKeyAction(async (key: string): Promise<AdminWriteResult<null>> => {
        seen.push(key)
        return { ok: true, status: 200, data: null }
      })
      const { loading } = useSession()
      if (loading) return <p>loading</p>
      return (
        <>
          <button onClick={() => void a.run()}>go</button>
          {a.error && <p>{a.error}</p>}
        </>
      )
    }
    render(
      <SessionProvider>
        <Probe />
      </SessionProvider>,
    )
    await userEvent.click(await screen.findByRole('button', { name: 'go' }))
    await screen.findByText(/reader role/i)
    expect(seen).toEqual([])
  })

  it('shows the session role badge next to the actor', async () => {
    installFetchMock({
      [ME_URL]: { body: { authenticated: true, actor: 'ro@example.com', role: 'reader' } },
    })
    render(
      <SessionProvider>
        <SessionButton />
      </SessionProvider>,
    )
    expect(await screen.findByText('ro@example.com')).toBeInTheDocument()
    expect(await screen.findByText('reader')).toBeInTheDocument()
  })
})
