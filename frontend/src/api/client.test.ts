import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock } from '../test/harness'
import { api } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('adminWrite error handling', () => {
  it('flattens a 422 validation-error list into a readable string', async () => {
    installFetchMock({
      'PATCH /api/v1/admin/schedules/sch1': {
        status: 422,
        body: {
          detail: [
            {
              type: 'value_error',
              loc: ['body', 'timezone'],
              msg: "Value error, unknown timezone: 'xyz'",
              input: 'xyz',
            },
          ],
        },
      },
    })

    const r = await api.updateSchedule('sch1', 'key', { timezone: 'xyz' })

    expect(r.ok).toBe(false)
    expect(r.status).toBe(422)
    expect(r.detail).toBe("timezone: Value error, unknown timezone: 'xyz'")
  })

  it('passes a plain string detail through unchanged', async () => {
    installFetchMock({
      'DELETE /api/v1/admin/schedules/sch1': {
        status: 404,
        body: { detail: 'schedule not found' },
      },
    })

    const r = await api.deleteSchedule('sch1', 'key')

    expect(r.ok).toBe(false)
    expect(r.detail).toBe('schedule not found')
  })

  it('falls back to a generic message when there is no usable detail', async () => {
    installFetchMock({
      'PATCH /api/v1/admin/schedules/sch1': { status: 500, body: {} },
    })

    const r = await api.updateSchedule('sch1', 'key', {})

    expect(r.detail).toBe('Request failed (500).')
  })
})
