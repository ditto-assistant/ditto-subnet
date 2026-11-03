export const contributorFixture = {
  netuid: 118, enabled: true, link: {
    link_id: '11111111-1111-4111-8111-111111111111', netuid: 118,
    hotkey: '5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY', coldkey: 'fixture-owner',
    github_user_id: 100, github_login: 'miner-fixture',
    link_attempt_id: '22222222-2222-4222-8222-222222222222', key_kind: 'hotkey',
    signer: 'fixture-hotkey', signature: 'ab'.repeat(64), nonce: '33333333-3333-4333-8333-333333333333',
    issued_at: '2026-10-13T10:00:00Z', linked_at: '2026-10-13T10:00:01Z', revoked_at: null,
    owner_block: 123, owner_block_hash: '0x' + 'ab'.repeat(32),
  },
}
export const claVersionFixture = { version: 1, sha256: 'ab'.repeat(32), published_at: '2026-10-13T00:00:00Z', effective_at: '2026-10-14T00:00:00Z' }
export const claStatusFixture = { netuid: 118, mode: 'off', current: claVersionFixture, signature: null, is_current: false }
const claSeedRevision = { revision: 0, parent_revision: 0, mode: 'off', actor: 'migration', reason: 'default off', created_at: '2026-10-13T00:00:00Z' }
export const claControlFixture = {
  // Platform lists the current revision first in its history.
  current: claSeedRevision,
  history: [claSeedRevision], effective_version: claVersionFixture, active_hotkeys: 2, signed_active_hotkeys: 1, unsigned_active_hotkeys: 1,
  active_hotkeys_basis: 'non_retired_submissions', unsigned_shadow_uploads: 3,
  history_total: 1, history_limit: 25, history_offset: 0, history_has_more: false,
  minimum_enforced_notice_seconds: 86400, earliest_enforced_effective_at: null,
  earliest_enforcement_at: '2026-10-14T00:00:00Z',
}
export const claShadowRevisionFixture = {
  revision: 1, parent_revision: 0, mode: 'shadow', actor: 'operator@omniaura.ai', reason: 'measure consent adoption', created_at: '2026-10-14T00:00:00Z',
}
export const githubRevocationFixture = {
  link: contributorFixture.link, operator_revocation: null, can_revoke: true,
  blocking_reason: null, confirmation: `REVOKE GITHUB LINK ${contributorFixture.link.link_id}`,
}
export const revokedGithubFixture = {
  ...githubRevocationFixture,
  link: { ...contributorFixture.link, revoked_at: '2026-10-14T10:00:00Z' },
  operator_revocation: { link_id: contributorFixture.link.link_id, actor: 'operator@omniaura.ai',
    reason: 'recover a verified hostile link', revoked_at: '2026-10-14T10:00:00Z' },
  can_revoke: false, blocking_reason: 'GitHub link is already revoked',
}
