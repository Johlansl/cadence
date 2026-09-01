// The X-Admin-Key needed to trigger jobs. Kept in sessionStorage: entered once
// per browser session, never sent anywhere but the Cadence API.

const STORAGE_KEY = 'cadence.adminKey'

export function getAdminKey(): string {
  try {
    return sessionStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setAdminKey(value: string): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, value)
  } catch {
    /* sessionStorage unavailable -- the key just won't persist */
  }
}

export function clearAdminKey(): void {
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}
