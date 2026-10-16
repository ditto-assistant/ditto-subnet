import { PlatformAdminError, platformAdminRequest } from './ditto.server'

/**
 * What reading a write's target back shows.
 * - `applied`: this exact write is visible.
 * - `superseded`: the exact record this write would create holds a different
 *   write (the revision after its guard, an immutable row, a terminal audit),
 *   so it can never apply. Read that record itself, never the latest state: a
 *   write that committed and was then followed by others reads back `applied`.
 * - `absent`: the write is not visible. After a timeout Platform may still be
 *   processing it, so this alone never proves it failed.
 */
export type ReadBack<T> =
  | { state: 'applied'; value: T }
  | { state: 'superseded'; detail: string }
  | { state: 'absent' }

/** A write that committed, or one whose outcome Backroom cannot decide. */
export type WriteOutcome<T> =
  | { state: 'committed'; value: T }
  /** `cause` is the Platform failure behind the uncertainty, when there was one. */
  | { state: 'unknown'; reason: string; cause?: PlatformAdminError }

/** The write definitely applied nothing; the message says what to do next. */
export class NothingAppliedError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options)
    this.name = 'NothingAppliedError'
  }
}

/**
 * Send one Platform admin write and decide its outcome honestly.
 *
 * Every write needs the same three answers, so they live here rather than in
 * each caller: report a commit as a commit, never report a write that may have
 * committed as failed (a retry with stale guards would then misfire), and
 * never report a write that definitely did nothing as possibly applied.
 *
 * - Not sent (a plain Error or 'unreachable'), auth and other 4xx refusals
 *   propagate unchanged: nothing was applied.
 * - `refusedBeforeWrite` marks Platform refusals raised before any write,
 *   such as a lock timeout answered with 503.
 * - A 409 may be this same write already applied by an earlier attempt; the
 *   read-back decides, otherwise the 409 stands.
 * - A timeout, a 5xx or a transport failure, and a 2xx with an unreadable body
 *   (including a parser that throws), are settled by the read-back; an absent
 *   write stays unknown.
 * - Every error derived from a Platform failure keeps it as `cause`, so callers
 *   can still read its status and details.
 *
 * `readBack` must be read-only and must recognise this exact write, not just
 * any write to the same target.
 */
export async function platformAdminWrite<T>(
  path: string,
  request: { actor: string; body: unknown; method?: 'POST' | 'PUT' | 'PATCH' | 'DELETE' },
  options: {
    parse: (payload: unknown) => T | null
    readBack: () => Promise<ReadBack<T>>
    refusedBeforeWrite?: (error: PlatformAdminError) => boolean
    /** Appended to a definite refusal: how to retry safely. */
    retryHint: string
  },
): Promise<WriteOutcome<T>> {
  let payload: unknown
  try {
    payload = await platformAdminRequest(path, {
      method: request.method ?? 'POST',
      actor: request.actor,
      body: request.body,
    })
  } catch (error) {
    if (!(error instanceof PlatformAdminError)) throw error
    if (error.failure === 'unreachable' || error.failure === 'auth') throw error
    if (options.refusedBeforeWrite?.(error)) {
      throw new NothingAppliedError(`${error.message}. Nothing was applied: ${options.retryHint}`, {
        cause: error,
      })
    }
    if (error.failure === 'request') {
      if (error.status !== 409) throw error
      const seen = await tryReadBack(options.readBack)
      if (seen?.state === 'applied') return { state: 'committed', value: seen.value }
      if (seen === undefined) {
        throw new Error(
          `${error.message}. Reading the target back failed, so an earlier attempt may already have ` +
            'applied this write; read it before retrying.',
          { cause: error },
        )
      }
      throw error
    }
    return settle(error, options.readBack)
  }
  // A parser that throws reads the 2xx as unreadable, never as a failure.
  let value: T | null = null
  try {
    value = options.parse(payload)
  } catch {
    value = null
  }
  if (value !== null) return { state: 'committed', value }
  // Platform answers 2xx only after committing; settle the unreadable body. A
  // superseded target means the 2xx was not Platform committing this write.
  const seen = await tryReadBack(options.readBack)
  if (seen?.state === 'applied') return { state: 'committed', value: seen.value }
  if (seen?.state === 'superseded') {
    throw new NothingAppliedError(
      `Platform reported success but returned an unreadable response. ${seen.detail}, so this write cannot have applied.`,
    )
  }
  return {
    state: 'unknown',
    reason: 'Platform reported success but returned an unreadable response, so the write probably committed',
  }
}

async function settle<T>(
  failure: PlatformAdminError,
  readBack: () => Promise<ReadBack<T>>,
): Promise<WriteOutcome<T>> {
  const seen = await tryReadBack(readBack)
  if (seen?.state === 'applied') return { state: 'committed', value: seen.value }
  if (seen?.state === 'superseded') {
    throw new NothingAppliedError(`${failure.message}. ${seen.detail}, so this write cannot have applied.`, {
      cause: failure,
    })
  }
  return {
    state: 'unknown',
    reason:
      seen === undefined
        ? `${failure.message}; reading the target back also failed`
        : `${failure.message}; it is not visible yet and may still commit`,
    cause: failure,
  }
}

async function tryReadBack<T>(readBack: () => Promise<ReadBack<T>>) {
  try {
    return await readBack()
  } catch {
    return undefined
  }
}
