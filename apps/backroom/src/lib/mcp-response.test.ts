import { describe, expect, it } from 'vitest'
import {
  compactListField,
  compactListFields,
  hoistSharedFields,
} from './mcp-response'

describe('hoistSharedFields', () => {
  it('lifts the fields every row repeats and leaves the rest', () => {
    const { shared, rows } = hoistSharedFields([
      { agent_id: 'a', actor: 'operator@example.com', bench_version: 7 },
      { agent_id: 'b', actor: 'operator@example.com', bench_version: 7 },
    ])
    expect(shared).toEqual({ actor: 'operator@example.com', bench_version: 7 })
    expect(rows).toEqual([{ agent_id: 'a' }, { agent_id: 'b' }])
  })

  it('is lossless: every row rebuilds from the shared block', () => {
    const original = [
      { id: 'a', reason: 'same', score: 1 },
      { id: 'b', reason: 'same', score: 2 },
    ]
    const { shared, rows } = hoistSharedFields(original)
    expect(rows.map((row) => ({ ...shared, ...row }))).toEqual(original)
  })

  it('compares by value, so a differing array stays on its row', () => {
    const { shared, rows } = hoistSharedFields([
      { id: 'a', hotkeys: ['5A', '5B'] },
      { id: 'b', hotkeys: ['5A', '5C'] },
    ])
    expect(shared).toEqual({})
    expect(rows).toEqual([
      { id: 'a', hotkeys: ['5A', '5B'] },
      { id: 'b', hotkeys: ['5A', '5C'] },
    ])
  })

  it('hoists an identical array once', () => {
    const { shared, rows } = hoistSharedFields([
      { id: 'a', hotkeys: ['5A', '5B'] },
      { id: 'b', hotkeys: ['5A', '5B'] },
    ])
    expect(shared).toEqual({ hotkeys: ['5A', '5B'] })
    expect(rows).toEqual([{ id: 'a' }, { id: 'b' }])
  })

  it('does not hoist a field missing from any row', () => {
    const { shared } = hoistSharedFields([
      { id: 'a', detail: 'skipped' },
      { id: 'b' },
    ])
    expect(shared).toEqual({})
  })

  it('leaves a single row alone: there is nothing repeated to remove', () => {
    const { shared, rows } = hoistSharedFields([{ id: 'a', actor: 'operator' }])
    expect(shared).toEqual({})
    expect(rows).toEqual([{ id: 'a', actor: 'operator' }])
  })

  it('keeps pinned identity fields on every row', () => {
    const { shared, rows } = hoistSharedFields(
      [
        { agent_id: 'a', status: 'granted' },
        { agent_id: 'a', status: 'granted' },
      ],
      { pin: ['agent_id'] },
    )
    expect(shared).toEqual({ status: 'granted' })
    expect(rows).toEqual([{ agent_id: 'a' }, { agent_id: 'a' }])
  })

  it('drops omitted fields outright', () => {
    const { shared, rows } = hoistSharedFields(
      [
        { agent_id: 'a', quorum: 3 },
        { agent_id: 'b', quorum: 3 },
      ],
      { omit: ['quorum'] },
    )
    expect(shared).toEqual({})
    expect(rows).toEqual([{ agent_id: 'a' }, { agent_id: 'b' }])
  })
})

describe('compactListField', () => {
  it('keeps the list under its own key and adds a shared sibling', () => {
    const compact = compactListField(
      {
        count: 2,
        items: [
          { id: 'a', status: 'active' },
          { id: 'b', status: 'active' },
        ],
      },
      'items',
    )
    expect(compact).toEqual({
      count: 2,
      items: [{ id: 'a' }, { id: 'b' }],
      items_shared: { status: 'active' },
    })
  })

  it('omits the shared sibling when nothing was invariant', () => {
    const compact = compactListField(
      { items: [{ id: 'a' }, { id: 'b' }] },
      'items',
    )
    expect(compact).not.toHaveProperty('items_shared')
  })

  it('leaves arrays of scalars untouched', () => {
    const envelope = { applied: ['flag.a', 'flag.b'] }
    expect(compactListField(envelope, 'applied')).toEqual(envelope)
  })

  it('leaves a missing or non-array field untouched', () => {
    const envelope = { withdrawal: null, count: 0 }
    expect(compactListField(envelope, 'withdrawal')).toEqual(envelope)
  })

  it('compacts several lists in one envelope', () => {
    const compact = compactListFields(
      {
        tickets: [
          { validator_hotkey: '5A', bench_version: 7 },
          { validator_hotkey: '5B', bench_version: 7 },
        ],
        recoveries: [
          { recovery_id: 'r1', agent_id: 'a', actor: 'operator' },
          { recovery_id: 'r2', agent_id: 'a', actor: 'operator' },
        ],
      },
      {
        tickets: { pin: ['validator_hotkey'] },
        recoveries: { pin: ['recovery_id'], omit: ['agent_id'] },
      },
    )
    expect(compact).toEqual({
      tickets: [{ validator_hotkey: '5A' }, { validator_hotkey: '5B' }],
      tickets_shared: { bench_version: 7 },
      recoveries: [{ recovery_id: 'r1' }, { recovery_id: 'r2' }],
      recoveries_shared: { actor: 'operator' },
    })
  })
})
