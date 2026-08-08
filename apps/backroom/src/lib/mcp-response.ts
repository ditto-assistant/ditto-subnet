// Shaping helpers for MCP tool payloads.
//
// Platform admin responses are built for machine consumers that read one row at
// a time, so they repeat every invariant on every row: the same actor, the same
// reason paragraph, the same created_at, the same bench version. An MCP client
// pays for all of it in context. These helpers remove the repetition without
// removing information: nothing is summarised away, no row disappears, and a
// reader reconstructs the original row as `{ ...shared, ...row }`.
//
// This is presentation only. The platform still receives, checks, and audits
// exactly what it did before.

/** A JSON object row inside a tool response list. */
export type ResponseRow = Record<string, unknown>

export type HoistOptions = {
  /** Fields that stay on every row even when they never vary. */
  pin?: ReadonlyArray<string>
  /** Fields removed outright, for values the envelope already carries. */
  omit?: ReadonlyArray<string>
}

export type HoistedList = {
  shared: ResponseRow
  rows: Array<ResponseRow>
}

function stable(value: unknown): string {
  return JSON.stringify(value ?? null)
}

function isPlainObject(value: unknown): value is ResponseRow {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/**
 * Lift the fields that every row repeats into one `shared` object.
 *
 * A field is hoisted only when it is present on every row with an identical
 * value, so the transform is lossless and reversible. A single row is left
 * alone: there is no repetition to remove, and hoisting would only add a level
 * of indirection.
 */
export function hoistSharedFields(
  rows: ReadonlyArray<ResponseRow>,
  options: HoistOptions = {},
): HoistedList {
  const omit = new Set(options.omit ?? [])
  const trimmed = rows.map((row) => {
    const next: ResponseRow = {}
    for (const [key, value] of Object.entries(row)) {
      if (!omit.has(key)) next[key] = value
    }
    return next
  })

  if (trimmed.length < 2) {
    return { shared: {}, rows: trimmed }
  }

  const pin = new Set(options.pin ?? [])
  const [first, ...rest] = trimmed
  const shared: ResponseRow = {}
  for (const [key, value] of Object.entries(first)) {
    if (pin.has(key)) continue
    const encoded = stable(value)
    const invariant = rest.every(
      (row) => key in row && stable(row[key]) === encoded,
    )
    if (invariant) shared[key] = value
  }

  const sharedKeys = Object.keys(shared)
  if (sharedKeys.length === 0) {
    return { shared: {}, rows: trimmed }
  }

  return {
    shared,
    rows: trimmed.map((row) => {
      const next: ResponseRow = {}
      for (const [key, value] of Object.entries(row)) {
        if (!(key in shared)) next[key] = value
      }
      return next
    }),
  }
}

/**
 * Replace one list inside a response envelope with its compacted form.
 *
 * The list keeps its own key so existing readers still find an array there; the
 * hoisted fields land in a sibling `<key>_shared` object, which is omitted
 * entirely when nothing was invariant. Envelope keys are otherwise untouched.
 */
export function compactListField<T extends Record<string, unknown>>(
  envelope: T,
  key: keyof T & string,
  options: HoistOptions = {},
): Record<string, unknown> {
  const list = envelope[key]
  // Only lists of plain objects have fields to hoist. Arrays of strings or
  // numbers are left exactly as they are.
  if (!Array.isArray(list) || !list.every(isPlainObject)) return envelope
  const { shared, rows } = hoistSharedFields(list as Array<ResponseRow>, options)
  const next: Record<string, unknown> = { ...envelope, [key]: rows }
  if (Object.keys(shared).length > 0) {
    next[`${key}_shared`] = shared
  }
  return next
}

/** Apply {@link compactListField} to several lists in one envelope. */
export function compactListFields<T extends Record<string, unknown>>(
  envelope: T,
  fields: Record<string, HoistOptions>,
): Record<string, unknown> {
  let next: Record<string, unknown> = envelope
  for (const [key, options] of Object.entries(fields)) {
    next = compactListField(next, key, options)
  }
  return next
}

/** Drop null and undefined entries so absent detail costs nothing. */
export function withoutNulls(row: ResponseRow): ResponseRow {
  const next: ResponseRow = {}
  for (const [key, value] of Object.entries(row)) {
    if (value !== null && value !== undefined) next[key] = value
  }
  return next
}
