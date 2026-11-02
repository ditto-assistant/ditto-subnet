import { KeyRound, RefreshCw, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'

export type McpGrant = {
  id: string
  clientId: string
  clientName: string
  scopes: Array<string>
  requestedScopes: Array<string> | null
  accessLevel: string
  authorizedAt: string | null
  createdAt: number
  expiresAt: number | null
}

function formatTimestamp(seconds: number | null) {
  if (!seconds) return 'n/a'
  return new Date(seconds * 1_000).toISOString().replace('T', ' ').slice(0, 16) + ' UTC'
}

/**
 * The signed-in operator's own MCP OAuth grants. Each row names the exact
 * grant and client ids that `get_backroom_access` reports for a connection, so
 * an operator can match a live agent to its grant and revoke it (with every
 * access and refresh token issued under it) without waiting for expiry.
 */
export function McpGrantsPanel() {
  const [grants, setGrants] = useState<Array<McpGrant> | null>(null)
  const [error, setError] = useState('')
  const [revoking, setRevoking] = useState('')

  const load = useCallback(async () => {
    setError('')
    try {
      const response = await fetch('/oauth/grants', { headers: { Accept: 'application/json' } })
      const payload = (await response.json()) as { grants?: Array<McpGrant>; error?: string }
      if (!response.ok || !payload.grants) {
        throw new Error(payload.error || 'Unable to load agent connections')
      }
      setGrants(payload.grants)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to load agent connections')
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const revoke = async (grant: McpGrant) => {
    if (!window.confirm(`Revoke ${grant.clientName} (${grant.id})? The agent must reconnect.`)) {
      return
    }
    setRevoking(grant.id)
    setError('')
    try {
      const response = await fetch('/oauth/grants/revoke', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ grantId: grant.id }),
      })
      const payload = (await response.json()) as { revoked?: string; error?: string }
      if (!response.ok || !payload.revoked) {
        throw new Error(payload.error || 'Unable to revoke the connection')
      }
      await load()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to revoke the connection')
    } finally {
      setRevoking('')
    }
  }

  return (
    <section className="mt-6 rounded-xl border border-[var(--line)] bg-[var(--panel)] p-5 sm:p-6">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="grid h-9 w-9 place-items-center rounded-lg bg-[var(--acid-dim)] text-[var(--acid)]">
            <KeyRound className="h-4 w-4" />
          </div>
          <div>
            <h3 className="text-sm font-semibold">Your agent connections</h3>
            <p className="mt-0.5 text-xs text-[var(--muted)]">
              Match a grant id to get_backroom_access, then revoke it and all of its tokens.
            </p>
          </div>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="inline-flex items-center gap-1.5 rounded px-2 py-1 text-[11px] text-[var(--muted)] hover:bg-white/5 hover:text-white"
        >
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </button>
      </div>

      {error ? (
        <div className="mt-4 rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] px-4 py-3 text-sm text-[var(--red)]">
          {error}
        </div>
      ) : null}

      {grants === null && !error ? (
        <p className="mt-4 text-xs text-[var(--muted)]">Loading connections…</p>
      ) : null}
      {grants?.length === 0 ? (
        <p className="mt-4 text-xs text-[var(--muted)]">No agent connections are authorized.</p>
      ) : null}

      {grants && grants.length > 0 ? (
        <ul className="mt-4 space-y-3">
          {grants.map((grant) => (
            <li
              key={grant.id}
              className="flex flex-col gap-3 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4 sm:flex-row sm:items-start sm:justify-between"
            >
              <div className="min-w-0 space-y-1 text-xs text-[var(--muted)]">
                <p className="text-sm font-medium text-white">
                  {grant.clientName}{' '}
                  <span className="text-[11px] font-normal text-[var(--muted-strong)]">
                    {grant.accessLevel}
                  </span>
                </p>
                <p className="break-all">
                  Grant <code className="text-[var(--muted-strong)]">{grant.id}</code> · Client{' '}
                  <code className="text-[var(--muted-strong)]">{grant.clientId}</code>
                </p>
                <p>
                  Granted: <code>{grant.scopes.join(' ')}</code>
                  {grant.requestedScopes ? (
                    <>
                      {' '}
                      · Requested: <code>{grant.requestedScopes.join(' ')}</code>
                    </>
                  ) : null}
                </p>
                <p>
                  Authorized {formatTimestamp(grant.createdAt)}
                  {grant.expiresAt ? ` · Expires ${formatTimestamp(grant.expiresAt)}` : ''}
                </p>
              </div>
              <button
                type="button"
                onClick={() => void revoke(grant)}
                disabled={revoking !== ''}
                className="inline-flex shrink-0 items-center justify-center gap-2 rounded-lg border border-[var(--red)]/30 px-3 py-2 text-xs font-medium text-[var(--red)] transition-colors hover:bg-[var(--red-dim)] disabled:opacity-50"
              >
                <Trash2 className="h-3.5 w-3.5" />
                {revoking === grant.id ? 'Revoking…' : 'Revoke'}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  )
}
