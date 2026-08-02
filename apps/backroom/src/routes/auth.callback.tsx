import { createFileRoute, redirect } from '@tanstack/react-router'
import { finishGoogleOAuth } from '../server/auth.functions'

export const Route = createFileRoute('/auth/callback')({
  validateSearch: (search) => ({
    code: typeof search.code === 'string' ? search.code : '',
    state: typeof search.state === 'string' ? search.state : '',
    error: typeof search.error === 'string' ? search.error : '',
  }),
  loaderDeps: ({ search }) => search,
  loader: async ({ deps }) => {
    if (deps.error) {
      throw redirect({
        to: '/login',
        search: {
          error: 'Google sign-in was cancelled or denied.',
          next: '/screener-capacity',
        },
      })
    }
    if (!deps.code || !deps.state) {
      throw redirect({
        to: '/login',
        search: {
          error: 'Google did not return a complete sign-in response.',
          next: '/screener-capacity',
        },
      })
    }

    let returnTo = '/screener-capacity'
    try {
      const result = await finishGoogleOAuth({
        data: { code: deps.code, state: deps.state },
      })
      returnTo = result.returnTo
    } catch (cause) {
      throw redirect({
        to: '/login',
        search: {
          error: cause instanceof Error ? cause.message : 'Unable to complete Google sign-in.',
          next: '/screener-capacity',
        },
      })
    }
    throw redirect({ href: returnTo })
  },
  pendingComponent: () => (
    <main className="grid min-h-screen place-items-center bg-[var(--ink)]">
      <div className="text-center">
        <span className="pulse-dot mx-auto block h-3 w-3 rounded-full bg-[var(--acid)]" />
        <p className="mt-4 text-sm text-[var(--muted)]">Securing your session…</p>
      </div>
    </main>
  ),
})
