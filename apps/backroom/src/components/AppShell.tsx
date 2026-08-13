import { Link, Outlet, useRouter, useRouterState } from '@tanstack/react-router'
import { useServerFn } from '@tanstack/react-start'
import { useState } from 'react'
import {
  Activity,
  Bot,
  CircleDollarSign,
  Copy,
  Flame,
  FlaskConical,
  Gauge,
  Layers,
  LogOut,
  Network,
  Search,
  RotateCw,
  ServerCog,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  TimerReset,
  Timer,
  Zap,
} from 'lucide-react'
import type { BackroomUser } from '../lib/auth.types'
import { logout } from '../server/auth.functions'
import { CommandPalette } from './CommandPalette'

const navigation = [
  {
    to: '/miner-fees' as const,
    label: 'Miner fees',
    description: 'Submission revenue ledger',
    icon: CircleDollarSign,
  },
  {
    to: '/benchmark-rollout' as const,
    label: 'Bench rollout',
    description: 'SN118 contract activation',
    icon: Gauge,
  },
  {
    to: '/agent-access' as const,
    label: 'Agent access',
    description: 'OAuth MCP connection',
    icon: Bot,
  },
  {
    to: '/burn' as const,
    label: 'Emission burn',
    description: 'Miner / owner-burn split',
    icon: Flame,
  },
  {
    to: '/continual-retests' as const,
    label: 'Continual retests',
    description: 'Top-five wave controls',
    icon: RotateCw,
  },
  {
    to: '/confirmation-bundles' as const,
    label: 'V9 confirmation',
    description: 'Qualification evidence & spend',
    icon: FlaskConical,
  },
  {
    to: '/validator-slots' as const,
    label: 'Validator fleet',
    description: 'Concurrent benchmark cap',
    icon: Layers,
  },
  {
    to: '/inference-concurrency' as const,
    label: 'Inference budgets',
    description: 'Hosted v7 spend & concurrency',
    icon: Zap,
  },
  {
    to: '/artifact-release' as const,
    label: 'Source release',
    description: 'Public code embargo',
    icon: TimerReset,
  },
  {
    to: '/submission-settings' as const,
    label: 'Upload cooldown',
    description: 'Miner submission cadence',
    icon: Timer,
  },
  {
    to: '/screener-review-settings' as const,
    label: 'Review controls',
    description: 'L2/L3 shadow & budgets',
    icon: SlidersHorizontal,
  },
  {
    to: '/screener-capacity' as const,
    label: 'Screener capacity',
    description: 'Targon & GCE fleet state',
    icon: ServerCog,
  },
  {
    to: '/inference-routing' as const,
    label: 'Inference routing',
    description: 'Provider health & admission',
    icon: Network,
  },
  {
    to: '/score-outliers' as const,
    label: 'Score outliers',
    description: 'Finalized score re-tests',
    icon: Activity,
  },
  {
    to: '/screening-quarantine' as const,
    label: 'Quarantine',
    description: 'SN118 screening decisions',
    icon: ShieldAlert,
  },
  {
    to: '/copy-review' as const,
    label: 'Operator reviews',
    description: 'SN118 anti-copy holds',
    icon: Copy,
  },
]

const commandDestinations = [
  ...navigation,
  {
    to: '/screening-quarantine',
    label: 'Quarantine · Pending',
    description: 'Submissions awaiting screening decisions',
    keywords: 'tab active review',
  },
  {
    to: '/screening-quarantine/disputes',
    label: 'Quarantine · Disputes',
    description: 'Miner dispute review queue',
    keywords: 'tab appeals',
  },
  {
    to: '/screening-quarantine/history',
    label: 'Quarantine · History',
    description: 'Resolved screening outcomes',
    keywords: 'tab resolved decisions',
  },
]

export function BackroomMark({ compact = false }: { compact?: boolean }) {
  return (
    <div className="flex items-center gap-3">
      <div className="relative grid h-9 w-9 place-items-center rounded-[10px] border border-[#536635] bg-[var(--acid-dim)]">
        <ShieldCheck className="h-4 w-4 text-[var(--acid)]" />
        <span className="absolute -right-1 -top-1 h-2.5 w-2.5 rounded-full border-2 border-[var(--sidebar)] bg-[var(--acid)]" />
      </div>
      {!compact ? (
        <div>
          <p className="text-[11px] font-medium text-[var(--muted)]">Ditto subnet</p>
          <p className="text-[17px] font-semibold tracking-[-0.02em]">Backroom</p>
        </div>
      ) : null}
    </div>
  )
}

export function AppShell({ user }: { user: BackroomUser }) {
  const [commandOpen, setCommandOpen] = useState(false)
  const router = useRouter()
  const pathname = useRouterState({
    select: (state) => state.location.pathname,
  })
  const signOut = useServerFn(logout)
  const current = navigation.find((item) => pathname.startsWith(item.to)) ?? navigation[0]

  const handleSignOut = async () => {
    await signOut()
    await router.navigate({
      to: '/login',
      search: { error: '', next: '/screener-capacity' },
    })
    await router.invalidate()
  }

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[15.5rem_1fr]">
      <aside className="hidden border-r border-[var(--line)] bg-[var(--sidebar)] px-4 py-5 lg:flex lg:flex-col">
        <BackroomMark />

        <button
          type="button"
          onClick={() => setCommandOpen(true)}
          className="mt-6 flex min-h-10 items-center gap-2 rounded-[10px] border border-[var(--line)] bg-white/[0.025] px-3 text-xs text-[var(--muted)] transition-colors hover:border-[var(--line-strong)] hover:bg-white/[0.045] hover:text-white"
        >
          <Search className="h-3.5 w-3.5" />
          <span className="flex-1 text-left">Search Backroom</span>
          <kbd className="rounded border border-[var(--line)] px-1.5 py-0.5 font-sans text-[9px]">⌘K</kbd>
        </button>

        <nav className="mt-4 space-y-1.5" aria-label="Backroom sections">
          {navigation.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              className="group flex items-center gap-3 rounded-[10px] border border-transparent px-3 py-2.5 text-[var(--muted)] transition-colors duration-150 hover:bg-white/[0.04] hover:text-white"
              activeProps={{
                className: 'border-[#46552f] bg-[var(--acid-dim)] text-white',
              }}
            >
              <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-white/[0.045] transition-colors group-hover:bg-white/[0.07]">
                <item.icon className="h-4 w-4" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] font-medium">{item.label}</span>
                <span className="mt-0.5 block truncate text-[11px] text-[var(--muted)]">
                  {item.description}
                </span>
              </span>
            </Link>
          ))}
        </nav>

        <div className="mt-auto border-t border-[var(--line)] pt-4">
          <div className="mb-4 flex items-center gap-2 px-1 text-[11px] text-[var(--muted-strong)]">
            <span className="pulse-dot h-2 w-2 rounded-full bg-[var(--acid)]" />
            Production connected
          </div>
          <div className="flex items-center gap-3">
            {user.picture ? (
              <img
                src={user.picture}
                alt=""
                referrerPolicy="no-referrer"
                className="h-9 w-9 rounded-full object-cover"
              />
            ) : (
              <div className="grid h-9 w-9 place-items-center rounded-full bg-[var(--panel-raised)] text-sm font-semibold">
                {user.email.slice(0, 1).toUpperCase()}
              </div>
            )}
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium">{user.name}</p>
              <p className="mt-0.5 truncate text-[10px] text-[var(--muted)]">{user.email}</p>
              <p className="mt-0.5 text-[9px] uppercase tracking-[0.12em] text-[var(--muted)]">
                {user.accessLevel === 'write' ? 'Editor' : 'Read only'}
              </p>
            </div>
            <button
              type="button"
              onClick={handleSignOut}
              className="rounded-lg p-2 text-[var(--muted)] transition-colors hover:bg-white/5 hover:text-white"
              aria-label="Sign out"
            >
              <LogOut className="h-4 w-4" />
            </button>
          </div>
        </div>
      </aside>

      <div className="min-w-0">
        <header className="sticky top-0 z-30 border-b border-[var(--line)] bg-[var(--ink)]/95 px-4 py-3 backdrop-blur-md sm:px-6 lg:px-8">
          <div className="mx-auto flex max-w-[86rem] items-center justify-between gap-4">
            <div className="flex items-center gap-3">
              <div className="lg:hidden">
                <BackroomMark compact />
              </div>
              <div>
                <h1 className="text-base font-semibold tracking-tight">{current.label}</h1>
                <p className="mt-0.5 hidden text-[11px] text-[var(--muted)] sm:block">
                  {current.description}
                </p>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <span className="hidden items-center gap-2 text-[11px] text-[var(--muted-strong)] sm:flex">
                <span className="pulse-dot h-2 w-2 rounded-full bg-[var(--acid)]" />
                Production
              </span>
              <button
                type="button"
                onClick={() => setCommandOpen(true)}
                className="hidden min-h-9 items-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs text-[var(--muted)] transition-colors hover:bg-white/5 hover:text-white sm:flex"
                aria-label="Search Backroom"
              >
                <Search className="h-3.5 w-3.5" />
                <span className="hidden md:inline">Search</span>
                <kbd className="hidden rounded border border-[var(--line)] px-1 py-0.5 font-sans text-[9px] md:inline">⌘K</kbd>
              </button>
              <button
                type="button"
                onClick={handleSignOut}
                className="rounded-lg border border-[var(--line)] p-2 text-[var(--muted)] transition-colors hover:bg-white/5 hover:text-white lg:hidden"
                aria-label="Sign out"
              >
                <LogOut className="h-4 w-4" />
              </button>
            </div>
          </div>
        </header>

        <main className="min-h-[calc(100vh-65px)] bg-[var(--ink)] px-4 py-6 pb-[calc(5.75rem+env(safe-area-inset-bottom))] sm:px-6 lg:px-8 lg:py-8 lg:pb-8">
          <div className="mx-auto max-w-[86rem]">
            <Outlet />
          </div>
        </main>

        <nav className="fixed inset-x-0 bottom-0 z-40 border-t border-[var(--line-strong)] bg-[var(--panel-raised)]/98 px-3 pb-[env(safe-area-inset-bottom)] lg:hidden" aria-label="Mobile task switcher">
          <div className="mx-auto grid max-w-md grid-cols-2 gap-2 py-2">
            <button type="button" onClick={() => setCommandOpen(true)} className="flex min-h-12 min-w-0 items-center justify-center gap-2 rounded-lg bg-[var(--acid-dim)] px-3 text-[var(--acid)]">
              <current.icon className="h-4 w-4 shrink-0" />
              <span className="truncate text-xs font-medium">{current.label}</span>
            </button>
            <button type="button" onClick={() => setCommandOpen(true)} className="flex min-h-12 items-center justify-center gap-2 rounded-lg px-3 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5 hover:text-white">
              <Search className="h-4 w-4" />
              All tasks
            </button>
          </div>
        </nav>
      </div>
      <CommandPalette destinations={commandDestinations} open={commandOpen} onOpenChange={setCommandOpen} />
    </div>
  )
}
