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

describe('campaign endpoints', () => {
  it('createCampaign POSTs the body with the admin key', async () => {
    const fn = installFetchMock({
      'POST /api/v1/admin/campaigns': { status: 201, body: { id: 'c1', status: 'draft' } },
    })

    const r = await api.createCampaign('adm', {
      name: 'march',
      stages: [1, 'rest'],
      max_concurrency: 2,
      max_failures: 1,
      tag: 'env=prod',
    })

    expect(r.ok).toBe(true)
    const [, init] = fn.mock.calls[0]
    expect(new Headers(init?.headers).get('X-Admin-Key')).toBe('adm')
    expect(JSON.parse(init?.body as string)).toEqual({
      name: 'march',
      stages: [1, 'rest'],
      max_concurrency: 2,
      max_failures: 1,
      tag: 'env=prod',
    })
  })

  it('campaignAction hits /admin/campaigns/{id}/{action}', async () => {
    const fn = installFetchMock({
      'POST *': { status: 200, body: { id: 'c1', status: 'running' } },
    })

    await api.campaignAction('c1', 'adm', 'activate')
    await api.campaignAction('c1', 'adm', 'cancel')

    expect(String(fn.mock.calls[0][0])).toBe('/api/v1/admin/campaigns/c1/activate')
    expect(String(fn.mock.calls[1][0])).toBe('/api/v1/admin/campaigns/c1/cancel')
  })

  it('listCampaigns / getCampaign are plain GETs', async () => {
    const fn = installFetchMock({
      'GET /api/v1/campaigns': { body: [] },
      'GET /api/v1/campaigns/c1': { body: { id: 'c1' } },
    })

    await api.listCampaigns()
    await api.getCampaign('c1')

    expect(fn.mock.calls.map((c) => String(c[0]))).toEqual([
      '/api/v1/campaigns',
      '/api/v1/campaigns/c1',
    ])
  })
})

describe('enrollment endpoints', () => {
  it('lists filtered enrollments with the admin key', async () => {
    const fn = installFetchMock({ 'GET /api/v1/admin/enrollments': { body: [] } })

    const r = await api.listEnrollments('adm', 'pending')

    expect(r).toMatchObject({ ok: true, data: [] })
    expect(String(fn.mock.calls[0][0])).toBe('/api/v1/admin/enrollments?state=pending')
    expect(new Headers(fn.mock.calls[0][1]?.headers).get('X-Admin-Key')).toBe('adm')
  })

  it('creates and revokes an enrollment code', async () => {
    const fn = installFetchMock({
      'POST /api/v1/admin/enrollments': {
        status: 201,
        body: { id: 'en1', code: 'cad1.secret.fingerprint' },
      },
      'DELETE /api/v1/admin/enrollments/en1': { status: 204 },
    })

    await api.createEnrollment('adm', { expected_hostname: 'vm-new', ttl_minutes: 30 })
    await api.revokeEnrollment('en1', 'adm')

    expect(fn.mock.calls.map((call) => String(call[0]))).toEqual([
      '/api/v1/admin/enrollments',
      '/api/v1/admin/enrollments/en1',
    ])
    expect(JSON.parse(fn.mock.calls[0][1]?.body as string)).toEqual({
      expected_hostname: 'vm-new',
      ttl_minutes: 30,
    })
  })

  it('lists and revokes certificates through their host-scoped paths', async () => {
    const fn = installFetchMock({
      'GET /api/v1/admin/hosts/h1/certificates': { body: [] },
      'DELETE /api/v1/admin/hosts/h1/certificates/7': { status: 204 },
    })

    await api.listAgentCertificates('h1', 'adm')
    await api.revokeAgentCertificate('h1', 7, 'adm')

    expect(fn.mock.calls.map((call) => String(call[0]))).toEqual([
      '/api/v1/admin/hosts/h1/certificates',
      '/api/v1/admin/hosts/h1/certificates/7',
    ])
  })
})
