import { beforeEach, describe, expect, it, vi } from 'vitest'

const getConfirmationBundleSettings = vi.fn()
const listConfirmationBundles = vi.fn()

vi.mock('@tanstack/react-router', () => ({
  createFileRoute: () => (options: unknown) => options,
}))

vi.mock('../../server/admin.functions', () => ({
  getConfirmationBundleSettings: () => getConfirmationBundleSettings(),
  listConfirmationBundles: (input: unknown) => listConfirmationBundles(input),
}))

import { loadConfirmationBundlesPage } from './confirmation-bundles'

describe('confirmation bundles route loader', () => {
  beforeEach(() => {
    getConfirmationBundleSettings.mockReset().mockResolvedValue({ revision: 3 })
    listConfirmationBundles.mockReset().mockResolvedValue({ items: [], count: 0 })
  })

  it('loads exactly the same first-page size that the panel paginates', async () => {
    await expect(loadConfirmationBundlesPage()).resolves.toEqual({
      settings: { revision: 3 },
      bundles: { items: [], count: 0 },
    })
    expect(listConfirmationBundles).toHaveBeenCalledWith({
      data: { generation: 'active', limit: 20 },
    })
  })
})
