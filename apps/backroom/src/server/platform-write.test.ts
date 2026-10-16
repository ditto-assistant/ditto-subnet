import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PlatformAdminError } from './ditto.server'
import { NothingAppliedError, platformAdminWrite, type ReadBack } from './platform-write'

const originalToken = process.env.DITTO_ADMIN_API_TOKEN

beforeEach(() => {
  process.env.DITTO_ADMIN_API_TOKEN = 'secret-admin-token'
})

afterEach(() => {
  vi.unstubAllGlobals()
  if (originalToken === undefined) delete process.env.DITTO_ADMIN_API_TOKEN
  else process.env.DITTO_ADMIN_API_TOKEN = originalToken
})

type Row = { id: number }
const committedRow: Row = { id: 7 }
const envelope = (message: string, status: number) => Response.json({ error_code: 3000, message, request_id: null }, { status })
const answers = {
  ok: () => Response.json(committedRow),
  unreadable: () => Response.json({ unexpected: true }),
  busy: () => envelope('Fixture is busy; retry shortly', 503),
  conflict: () => envelope('fixture conflict', 409),
}
// Failures after which the write may or may not have committed.
const uncertain = {
  '5xx': () => vi.fn().mockResolvedValueOnce(envelope('upstream exploded', 502)),
  timeout: () => vi.fn().mockRejectedValueOnce(new DOMException('timed out', 'TimeoutError')),
  transport: () => vi.fn().mockRejectedValueOnce(new TypeError('socket closed after send')),
}
const readBacks: Record<string, () => Promise<ReadBack<Row>>> = {
  applied: async () => ({ state: 'applied', value: committedRow }),
  superseded: async () => ({ state: 'superseded', detail: 'Row 7 already holds another write' }),
  absent: async () => ({ state: 'absent' }),
  failing: async () => { throw new Error('read-back unavailable') },
}

function write(readBack: () => Promise<ReadBack<Row>>) {
  return platformAdminWrite<Row>('/api/v1/admin/fixture', { actor: 'operator@example.com', body: { value: 1 } }, {
    parse: (payload) => (payload && typeof payload === 'object' && 'id' in payload ? (payload as Row) : null),
    readBack,
    refusedBeforeWrite: (error) => error.status === 503 && error.message.startsWith('Fixture is busy'),
    retryHint: 'retry with the same input.',
  })
}

describe('platformAdminWrite decides every outcome once', () => {
  it('returns a readable 2xx as committed without reading back', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(answers.ok()))
    const readBack = vi.fn(readBacks.absent)
    await expect(write(readBack)).resolves.toEqual({ state: 'committed', value: committedRow })
    expect(readBack).not.toHaveBeenCalled()
  })

  it.each([
    ['applied', { state: 'committed', value: committedRow }],
    ['absent', { state: 'unknown', reason: expect.stringContaining('probably committed') }],
    ['failing', { state: 'unknown', reason: expect.stringContaining('probably committed') }],
  ])('settles an unreadable 2xx with a %s read-back', async (seen, outcome) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(answers.unreadable()))
    await expect(write(readBacks[seen])).resolves.toEqual(outcome)
  })

  it('fails as not applied when an unreadable 2xx meets a superseding write', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(answers.unreadable()))
    await expect(write(readBacks.superseded)).rejects.toThrow(NothingAppliedError)
  })

  it.each([
    ['not sent (no admin token)', () => { delete process.env.DITTO_ADMIN_API_TOKEN; return vi.fn() }, Error, 0],
    ['unreachable', () => vi.fn().mockRejectedValueOnce(Object.assign(new TypeError('fetch failed'), { cause: { code: 'ECONNREFUSED' } })), PlatformAdminError, 1],
    ['401', () => vi.fn().mockResolvedValueOnce(envelope('bad token', 401)), PlatformAdminError, 1],
    ['403', () => vi.fn().mockResolvedValueOnce(envelope('forbidden', 403)), PlatformAdminError, 1],
    ['400', () => vi.fn().mockResolvedValueOnce(envelope('bad input', 400)), PlatformAdminError, 1],
    ['422', () => vi.fn().mockResolvedValueOnce(envelope('invalid', 422)), PlatformAdminError, 1],
    ['302', () => vi.fn().mockResolvedValueOnce(new Response(null, { status: 302, headers: { Location: '/elsewhere' } })), PlatformAdminError, 1],
    ['refused before the write', () => vi.fn().mockResolvedValueOnce(answers.busy()), NothingAppliedError, 1],
  ] as const)('fails as definitely not applied when %s, without reading back', async (_name, stub, type, calls) => {
    const fetchMock = stub()
    vi.stubGlobal('fetch', fetchMock)
    const readBack = vi.fn(readBacks.applied)
    const error = await write(readBack).catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(type)
    if (type === Error) expect(error).not.toBeInstanceOf(PlatformAdminError)
    if (error instanceof PlatformAdminError) expect(['unreachable', 'auth', 'request']).toContain(error.failure)
    expect(readBack).not.toHaveBeenCalled()
    expect(fetchMock).toHaveBeenCalledTimes(calls)
  })

  describe.each(Object.keys(uncertain) as (keyof typeof uncertain)[])('after a %s', (failure) => {
    it('returns a visible write as committed', async () => {
      vi.stubGlobal('fetch', uncertain[failure]())
      await expect(write(readBacks.applied)).resolves.toEqual({ state: 'committed', value: committedRow })
    })

    it('fails as not applied when another write supersedes it', async () => {
      vi.stubGlobal('fetch', uncertain[failure]())
      await expect(write(readBacks.superseded)).rejects.toThrow(NothingAppliedError)
    })

    it.each([
      ['absent', 'not visible yet and may still commit'],
      ['failing', 'reading the target back also failed'],
    ])('stays unknown when the read-back is %s', async (seen, reason) => {
      vi.stubGlobal('fetch', uncertain[failure]())
      const outcome = await write(readBacks[seen])
      expect(outcome).toEqual({ state: 'unknown', reason: expect.stringContaining(reason) })
    })
  })

  describe('after a 409', () => {
    it('returns this write when an earlier attempt already applied it', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(answers.conflict()))
      await expect(write(readBacks.applied)).resolves.toEqual({ state: 'committed', value: committedRow })
    })

    it.each(['absent', 'superseded'])('keeps the 409 when the read-back is %s', async (seen) => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(answers.conflict()))
      const error = await write(readBacks[seen]).catch((caught: unknown) => caught)
      expect(error).toBeInstanceOf(PlatformAdminError)
      expect((error as PlatformAdminError).status).toBe(409)
    })

    it('says an earlier attempt may have applied it when the read-back fails', async () => {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(answers.conflict()))
      await expect(write(readBacks.failing)).rejects.toThrow(/fixture conflict\. Reading the target back failed/)
    })
  })
})

describe('platformAdminWrite keeps every outcome decidable', () => {
  function throwingParse(readBack: () => Promise<ReadBack<Row>>) {
    return platformAdminWrite<Row>('/api/v1/admin/fixture', { actor: 'operator@example.com', body: { value: 1 } }, {
      parse: () => { throw new Error('fixture parser bug') },
      readBack,
      retryHint: 'retry with the same input.',
    })
  }

  it.each([
    ['applied', { state: 'committed', value: committedRow }],
    ['absent', { state: 'unknown', reason: expect.stringContaining('probably committed') }],
  ])('settles a 2xx whose parser throws like an unreadable one (%s read-back)', async (seen, outcome) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(answers.ok()))
    await expect(throwingParse(readBacks[seen])).resolves.toEqual(outcome)
  })

  async function rejection(promise: Promise<unknown>) {
    try {
      await promise
    } catch (error) {
      return error as Error
    }
    throw new Error('expected a rejection')
  }

  it.each([
    ['a 409 whose read-back fails', answers.conflict, readBacks.failing, Error, 409],
    ['a refusal before any write', answers.busy, readBacks.absent, NothingAppliedError, 503],
    ['a superseded write after a 5xx', () => envelope('upstream exploded', 502), readBacks.superseded, NothingAppliedError, 502],
  ])('keeps the Platform failure as the cause of %s', async (_name, answer, readBack, kind, status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(answer()))
    const error = await rejection(write(readBack))
    expect(error).toBeInstanceOf(kind)
    expect(error.cause).toBeInstanceOf(PlatformAdminError)
    expect((error.cause as PlatformAdminError).status).toBe(status)
  })
})
