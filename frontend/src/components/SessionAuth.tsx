import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../api/client'
import type { SessionInfo } from '../types'

interface SessionState {
  ssoAvailable: boolean
  session: SessionInfo
  loading: boolean
  refresh: () => Promise<void>
  login: () => void
  logout: () => Promise<void>
}

const defaultState: SessionState = {
  ssoAvailable: false,
  session: { authenticated: false },
  loading: true,
  refresh: async () => {},
  login: () => {},
  logout: async () => {},
}

const SessionContext = createContext<SessionState>(defaultState)

export function SessionProvider({ children }: { children: ReactNode }) {
  const [ssoAvailable, setSsoAvailable] = useState(false)
  const [session, setSession] = useState<SessionInfo>({ authenticated: false })
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    try {
      const r = await api.getSession()
      setSsoAvailable(r.ssoAvailable)
      setSession(r.session)
    } catch {
      // Transient failure: keep the last known state, the next poll or
      // action retries. Never log out the operator on a blip.
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const login = useCallback(() => api.startLogin(), [])
  const logout = useCallback(async () => {
    try {
      await api.logout()
    } finally {
      await refresh()
    }
  }, [refresh])

  return (
    <SessionContext.Provider value={{ ssoAvailable, session, loading, refresh, login, logout }}>
      {children}
    </SessionContext.Provider>
  )
}

export function useSession(): SessionState {
  return useContext(SessionContext)
}

export function SessionButton() {
  const { ssoAvailable, session, loading, login, logout } = useSession()
  if (!ssoAvailable || loading) return null
  if (session.authenticated) {
    return (
      <span className="flex items-center gap-2 text-xs text-zinc-400">
        <span title="Signed in via SSO">{session.actor ?? 'signed in'}</span>
        <button
          type="button"
          onClick={() => void logout()}
          className="uppercase tracking-widest text-zinc-500 hover:text-zinc-200"
        >
          Sign out
        </button>
      </span>
    )
  }
  return (
    <button
      type="button"
      onClick={login}
      className="text-xs uppercase tracking-widest text-zinc-500 hover:text-zinc-200"
    >
      Sign in with SSO
    </button>
  )
}
