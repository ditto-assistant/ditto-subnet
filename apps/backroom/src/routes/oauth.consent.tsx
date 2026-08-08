import { createFileRoute, redirect } from '@tanstack/react-router'
import {
  ArrowRight,
  Bot,
  Check,
  Download,
  Eye,
  LockKeyhole,
  Settings2,
  ShieldCheck,
  X,
} from 'lucide-react'
import { useState } from 'react'
import { BackroomMark } from '../components/AppShell'
import { getCurrentUser } from '../server/auth.functions'
import { readMcpConsentRequest } from '../server/mcp-oauth.functions'

export const Route = createFileRoute('/oauth/consent')({
  validateSearch: (search) => ({
    request: typeof search.request === 'string' ? search.request.slice(0, 32_768) : '',
  }),
  beforeLoad: async ({ search }) => {
    const user = await getCurrentUser()
    if (!user) {
      const next = `/oauth/consent?request=${encodeURIComponent(search.request)}`
      throw redirect({ to: '/login', search: { error: '', next } })
    }
    return { user }
  },
  loaderDeps: ({ search }) => search,
  loader: ({ deps }) => readMcpConsentRequest({ data: { requestToken: deps.request } }),
  pendingComponent: ConsentPending,
  errorComponent: ConsentError,
  component: ConsentPage,
})

function ConsentPage() {
  const details = Route.useLoaderData()
  const { user } = Route.useRouteContext()
  const { request } = Route.useSearch()
  const [accessLevel, setAccessLevel] = useState<'read' | 'artifact' | 'write' | 'full'>('read')
  const [pending, setPending] = useState<'allow' | 'deny' | null>(null)
  const [error, setError] = useState('')
  // The signed-in Backroom account's own access level is the only gate on which
  // levels are offered — the human chooses what to grant, regardless of the
  // narrower scope set the OAuth client happened to request.
  const canGrantArtifact = user.accessLevel === 'write'
  const canGrantWrite = user.accessLevel === 'write'
  const canGrantFull = canGrantArtifact && canGrantWrite

  const decide = async (decision: 'allow' | 'deny') => {
    setPending(decision)
    setError('')
    try {
      const response = await fetch('/oauth/authorize/complete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          requestToken: request,
          csrf: details.csrf,
          decision,
          accessLevel,
        }),
      })
      const payload = (await response.json()) as {
        redirectTo?: string
        error?: string
        error_description?: string
      }
      if (!response.ok || !payload.redirectTo) {
        throw new Error(payload.error_description || payload.error || 'Authorization failed')
      }
      window.location.assign(payload.redirectTo)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Authorization failed')
      setPending(null)
    }
  }

  return (
    <main className="min-h-screen bg-[var(--ink)] px-4 py-8 sm:py-12">
      <div className="mx-auto w-full max-w-2xl">
        <div className="mb-8 flex items-center justify-between gap-4">
          <BackroomMark />
          <div className="text-right">
            <p className="text-xs font-medium text-white">{user.name}</p>
            <p className="mt-0.5 text-[11px] text-[var(--muted)]">{user.email}</p>
          </div>
        </div>

        <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
          <div className="border-b border-[var(--line)] px-6 py-7 sm:px-8">
            <div className="flex items-start gap-4">
              <div className="grid h-11 w-11 shrink-0 place-items-center rounded-xl border border-[var(--line-strong)] bg-[var(--panel-raised)]">
                <Bot className="h-5 w-5 text-[var(--acid)]" />
              </div>
              <div>
                <p className="text-xs font-medium text-[var(--acid)]">Agent connection request</p>
                <h1 className="mt-1.5 text-2xl font-semibold tracking-[-0.03em]">
                  Allow {details.clientName} to use Backroom?
                </h1>
                <p className="mt-3 text-sm leading-6 text-[var(--muted)]">
                  This connection acts as your verified operator account against SN118 production.
                  Choose the narrowest access it needs.
                </p>
              </div>
            </div>
          </div>

          <div className="space-y-3 px-6 py-6 sm:px-8">
            <PermissionOption
              selected={accessLevel === 'read'}
              onSelect={() => setAccessLevel('read')}
              icon={Eye}
              title="Read only"
              badge="Recommended"
              description="Inspect subnet policy, screening history, scores, and the emission split. No production state can change."
              items={[
                'Read queue, slot, scoring, and emission-burn policy',
                'Read screening quarantines, disputes, and attempt history',
                'Read leaderboards, score history, and owner footprints',
                'Preview guarded review batches without executing them',
              ]}
            />

            {canGrantArtifact ? (
              <PermissionOption
                selected={accessLevel === 'artifact'}
                onSelect={() => setAccessLevel('artifact')}
                icon={Download}
                title="Read & download source"
                badge="Sensitive source"
                description="Inspect production state and read miner-submitted source: tarball downloads, file listings, and copy or baseline diffs. Cannot change any verdict or policy."
                items={[
                  'Read subnet policy, screening history, and scores',
                  'Issue short-lived audited artifact URLs',
                  'Read miner source listings, excerpts, and diffs',
                  'No rescreen, resolve, or configuration access',
                ]}
              />
            ) : null}

            {canGrantWrite ? (
              <PermissionOption
                selected={accessLevel === 'write'}
                onSelect={() => setAccessLevel('write')}
                icon={Settings2}
                title="Read & write"
                badge="Production changes"
                description="Allow the agent to change subnet policy and resolve quarantine or dispute reviews. Source downloads remain separately scoped."
                items={[
                  'Change queue, slot, scoring, and retest policy',
                  'Set the emission burn — this moves TAO',
                  'Release, rescreen, or reject quarantines',
                  'Evict, reinstate, and retry validator work',
                ]}
                warning
              />
            ) : null}

            {canGrantFull ? (
              <PermissionOption
                selected={accessLevel === 'full'}
                onSelect={() => setAccessLevel('full')}
                icon={ShieldCheck}
                title="Full requested access"
                badge="Source & changes"
                description="Grant both miner-source reads and production mutation access requested by this client."
                items={[
                  'Read subnet policy, screening history, and scores',
                  'Read miner source and issue audited downloads',
                  'Change subnet policy, the burn, and review verdicts',
                ]}
                warning
              />
            ) : null}

            {!canGrantArtifact && !canGrantWrite ? (
              <div className="flex items-start gap-3 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4">
                <LockKeyhole className="mt-0.5 h-4 w-4 shrink-0 text-[var(--muted)]" />
                <p className="text-xs leading-5 text-[var(--muted)]">
                  Your Backroom account is read-only, so this connection can only receive read
                  access. Source downloads and production changes require a write-level account.
                </p>
              </div>
            ) : null}

            {error ? (
              <div className="rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] px-4 py-3 text-sm text-[var(--red)]">
                {error}
              </div>
            ) : null}
          </div>

          <div className="flex flex-col-reverse gap-3 border-t border-[var(--line)] bg-[var(--panel-soft)] px-6 py-5 sm:flex-row sm:items-center sm:justify-between sm:px-8">
            <button
              type="button"
              onClick={() => decide('deny')}
              disabled={pending !== null}
              className="inline-flex items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-4 py-3 text-sm font-medium text-[var(--muted-strong)] transition-colors hover:bg-white/5 hover:text-white disabled:opacity-50"
            >
              <X className="h-4 w-4" />
              {pending === 'deny' ? 'Declining…' : 'Decline'}
            </button>
            <button
              type="button"
              onClick={() => decide('allow')}
              disabled={pending !== null}
              className="inline-flex items-center justify-center gap-2 rounded-lg bg-[var(--acid)] px-5 py-3 text-sm font-semibold text-[#11150d] transition-colors hover:bg-[var(--acid-hover)] disabled:opacity-50"
            >
              {pending === 'allow'
                ? 'Authorizing…'
                : accessLevel === 'artifact'
                  ? 'Allow source downloads'
                  : accessLevel === 'write'
                    ? 'Allow production changes'
                    : accessLevel === 'full'
                      ? 'Allow full requested access'
                      : 'Allow read only'}
              <ArrowRight className="h-4 w-4" />
            </button>
          </div>
        </section>

        <p className="mt-5 flex items-center justify-center gap-2 text-center text-[11px] leading-5 text-[var(--muted)]">
          <ShieldCheck className="h-3.5 w-3.5" /> OAuth 2.1 · PKCE S256 · access can be revoked by
          reconnecting
        </p>
      </div>
    </main>
  )
}

function PermissionOption({
  selected,
  onSelect,
  icon: Icon,
  title,
  badge,
  description,
  items,
  warning = false,
}: {
  selected: boolean
  onSelect: () => void
  icon: typeof Eye
  title: string
  badge: string
  description: string
  items: Array<string>
  warning?: boolean
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={`w-full rounded-xl border p-4 text-left transition-colors sm:p-5 ${
        selected
          ? warning
            ? 'border-[var(--amber)]/55 bg-[var(--amber-dim)]'
            : 'border-[var(--acid)]/55 bg-[var(--acid-dim)]'
          : 'border-[var(--line)] bg-[var(--panel-soft)] hover:border-[var(--line-strong)]'
      }`}
    >
      <div className="flex items-start gap-3.5">
        <div
          className={`mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg ${
            warning
              ? 'bg-[var(--amber)]/10 text-[var(--amber)]'
              : 'bg-[var(--acid)]/10 text-[var(--acid)]'
          }`}
        >
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-white">{title}</span>
            <span
              className={`rounded-full px-2 py-0.5 text-[10px] font-medium ${
                warning
                  ? 'bg-[var(--amber)]/10 text-[var(--amber)]'
                  : 'bg-[var(--acid)]/10 text-[var(--acid)]'
              }`}
            >
              {badge}
            </span>
          </div>
          <p className="mt-1.5 text-xs leading-5 text-[var(--muted)]">{description}</p>
          <ul className="mt-3 space-y-1.5">
            {items.map((item) => (
              <li
                key={item}
                className="flex items-center gap-2 text-[11px] text-[var(--muted-strong)]"
              >
                <Check className="h-3.5 w-3.5 text-[var(--muted)]" />
                {item}
              </li>
            ))}
          </ul>
        </div>
        <span
          className={`mt-1 grid h-5 w-5 shrink-0 place-items-center rounded-full border ${
            selected
              ? warning
                ? 'border-[var(--amber)] bg-[var(--amber)] text-[#1a1309]'
                : 'border-[var(--acid)] bg-[var(--acid)] text-[#11150d]'
              : 'border-[var(--line-strong)]'
          }`}
        >
          {selected ? <Check className="h-3 w-3" /> : null}
        </span>
      </div>
    </button>
  )
}

function ConsentPending() {
  return (
    <main className="grid min-h-screen place-items-center bg-[var(--ink)]">
      <div className="text-center">
        <span className="pulse-dot mx-auto block h-3 w-3 rounded-full bg-[var(--acid)]" />
        <p className="mt-4 text-sm text-[var(--muted)]">Verifying the connection request…</p>
      </div>
    </main>
  )
}

function ConsentError({ error }: { error: Error }) {
  return (
    <main className="grid min-h-screen place-items-center bg-[var(--ink)] px-4">
      <section className="w-full max-w-md rounded-xl border border-[var(--red)]/30 bg-[var(--panel)] p-6 text-center">
        <LockKeyhole className="mx-auto h-6 w-6 text-[var(--red)]" />
        <h1 className="mt-4 text-lg font-semibold">Connection request failed</h1>
        <p className="mt-2 text-sm leading-6 text-[var(--muted)]">{error.message}</p>
      </section>
    </main>
  )
}
