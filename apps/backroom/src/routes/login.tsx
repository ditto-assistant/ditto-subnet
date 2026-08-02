import { createFileRoute, redirect } from '@tanstack/react-router'
import { useServerFn } from '@tanstack/react-start'
import { ArrowRight, LockKeyhole, ShieldCheck } from 'lucide-react'
import { useState } from 'react'
import { BackroomMark } from '../components/AppShell'
import { safeReturnTo } from '../lib/auth.policy'
import { beginGoogleOAuth, getCurrentUser } from '../server/auth.functions'

export const Route = createFileRoute('/login')({
  validateSearch: (search) => ({
    error: typeof search.error === 'string' ? search.error.slice(0, 240) : '',
    next:
      typeof search.next === 'string'
        ? safeReturnTo(search.next.slice(0, 8_192))
        : '/screener-capacity',
  }),
  beforeLoad: async ({ search }) => {
    const user = await getCurrentUser()
    if (user) throw redirect({ href: safeReturnTo(search.next) })
  },
  component: LoginPage,
})

function LoginPage() {
  const search = Route.useSearch()
  const startOAuth = useServerFn(beginGoogleOAuth)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')

  const login = async () => {
    setPending(true)
    setError('')
    try {
      await startOAuth({ data: { returnTo: search.next } })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to start sign-in')
      setPending(false)
    }
  }

  return (
    <main className="grid min-h-screen place-items-center bg-[var(--ink)] px-4 py-12">
      <section className="w-full max-w-md rounded-xl border border-[var(--line)] bg-[var(--panel)] p-6 sm:p-8">
        <BackroomMark />

        <div className="mt-10">
          <div className="mb-4 inline-flex items-center gap-2 text-xs font-medium text-[var(--acid)]">
            <span className="pulse-dot h-1.5 w-1.5 rounded-full bg-[var(--acid)]" />
            Restricted operations
          </div>
          <h1 className="text-3xl font-semibold tracking-[-0.035em]">Sign in to Backroom</h1>
          <p className="mt-4 max-w-sm text-sm leading-6 text-[var(--muted)]">
            Manage production rollouts and review developer submissions from one focused internal
            console.
          </p>
        </div>

        {search.error || error ? (
          <div className="mt-6 rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] px-4 py-3 text-sm text-[var(--red)]">
            {error || search.error}
          </div>
        ) : null}

        <button
          type="button"
          onClick={login}
          disabled={pending}
          className="mt-8 flex w-full items-center justify-between rounded-lg bg-[var(--acid)] px-4 py-3.5 text-sm font-semibold text-[#11150d] transition-colors hover:bg-[var(--acid-hover)] disabled:opacity-60"
        >
          <span className="flex items-center gap-3">
            <span className="grid h-7 w-7 place-items-center rounded-md bg-black/10 text-xs font-bold">
              G
            </span>
            {pending ? 'Opening Google…' : 'Continue with Google'}
          </span>
          <ArrowRight className="h-4 w-4" />
        </button>

        <div className="mt-5 flex items-start gap-3 rounded-lg bg-[var(--panel-soft)] p-3">
          <LockKeyhole className="mt-0.5 h-4 w-4 shrink-0 text-[var(--muted)]" />
          <p className="text-xs leading-5 text-[var(--muted)]">
            Access is limited to active members of the OmniAura team. Production actions are
            re-authorized by the Ditto API using your team role.
          </p>
        </div>

        <div className="mt-8 flex items-center justify-between border-t border-[var(--line)] pt-5 text-[11px] text-[var(--muted)]">
          <span className="flex items-center gap-2">
            <ShieldCheck className="h-3.5 w-3.5" /> Google OAuth + PKCE
          </span>
          <span>Internal only</span>
        </div>
      </section>
    </main>
  )
}
