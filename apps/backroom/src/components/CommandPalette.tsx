import { useEffect, useMemo, useRef, useState } from 'react'
import { CornerDownLeft, Search, X } from 'lucide-react'
import type { ReactNode } from 'react'

export type CommandDestination = {
  to: string
  label: string
  description: string
  keywords?: string
}

type PageTarget = { element: HTMLElement; label: string; kind: 'Section' | 'Tab' }

function pageTargets(): PageTarget[] {
  const root = document.querySelector('main')
  if (!root) return []
  const seen = new Set<string>()
  return Array.from(root.querySelectorAll<HTMLElement>('h2, h3, [role="tab"]'))
    .map((element) => ({
      element,
      label: element.textContent?.replace(/\s+/g, ' ').trim() ?? '',
      kind: element.getAttribute('role') === 'tab' ? ('Tab' as const) : ('Section' as const),
    }))
    .filter((target) => {
      const key = `${target.kind}:${target.label.toLowerCase()}`
      if (!target.label || seen.has(key) || target.element.offsetParent === null) return false
      seen.add(key)
      return true
    })
}

export function CommandPalette({ destinations, open, onOpenChange }: {
  destinations: CommandDestination[]
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const [query, setQuery] = useState('')
  const [targets, setTargets] = useState<PageTarget[]>([])
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!open) return
    setQuery('')
    setTargets(pageTargets())
    inputRef.current?.focus()
  }, [open])

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        onOpenChange(!open)
      } else if (event.key === 'Escape' && open) {
        onOpenChange(false)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [onOpenChange, open])

  const normalizedQuery = query.trim().toLowerCase()
  const filteredDestinations = useMemo(() => destinations.filter((item) =>
    `${item.label} ${item.description} ${item.keywords ?? ''}`.toLowerCase().includes(normalizedQuery),
  ), [destinations, normalizedQuery])
  const filteredTargets = targets.filter((target) =>
    `${target.kind} ${target.label}`.toLowerCase().includes(normalizedQuery),
  )

  if (!open) return null

  const activateTarget = (target: PageTarget) => {
    if (target.kind === 'Tab') target.element.click()
    target.element.scrollIntoView({ behavior: 'smooth', block: 'start' })
    if (!target.element.hasAttribute('tabindex')) target.element.tabIndex = -1
    target.element.focus({ preventScroll: true })
    onOpenChange(false)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/65 px-3 pt-[max(4rem,env(safe-area-inset-top))] sm:px-6 sm:pt-[12vh]" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onOpenChange(false)
    }}>
      <section role="dialog" aria-modal="true" aria-label="Search Backroom" className="w-full max-w-2xl overflow-hidden rounded-xl border border-[var(--line-strong)] bg-[var(--panel-raised)] shadow-[0_8px_8px_rgba(0,0,0,0.4)]">
        <div className="flex min-h-14 items-center gap-3 border-b border-[var(--line)] px-4">
          <Search className="h-4 w-4 shrink-0 text-[var(--muted)]" />
          <input ref={inputRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search tasks, tabs, and sections…" aria-label="Search tasks, tabs, and sections" className="min-w-0 flex-1 bg-transparent py-4 text-base text-white placeholder:text-[var(--muted)] sm:text-sm" />
          <button type="button" onClick={() => onOpenChange(false)} className="grid h-11 w-11 place-items-center rounded-lg text-[var(--muted)] hover:bg-white/5 hover:text-white sm:h-8 sm:w-8" aria-label="Close search"><X className="h-4 w-4" /></button>
        </div>
        <div className="scrollbar-thin max-h-[min(65vh,32rem)] overflow-y-auto p-2">
          {filteredTargets.length > 0 ? <CommandGroup label="On this page">{filteredTargets.map((target) => (
            <CommandRow key={`${target.kind}:${target.label}`} label={target.label} meta={target.kind} onClick={() => activateTarget(target)} />
          ))}</CommandGroup> : null}
          {filteredDestinations.length > 0 ? <CommandGroup label="Tasks">{filteredDestinations.map((item) => (
            <a key={item.to} href={item.to} onClick={() => onOpenChange(false)} className="group flex min-h-12 items-center gap-3 rounded-lg px-3 py-2.5 text-left hover:bg-white/[0.055] focus-visible:bg-white/[0.055]">
              <span className="min-w-0 flex-1"><span className="block text-sm font-medium text-white">{item.label}</span><span className="mt-0.5 block truncate text-xs text-[var(--muted)]">{item.description}</span></span>
              <CornerDownLeft className="h-3.5 w-3.5 text-[var(--muted)] opacity-0 group-hover:opacity-100 group-focus-visible:opacity-100" />
            </a>
          ))}</CommandGroup> : null}
          {filteredTargets.length === 0 && filteredDestinations.length === 0 ? <div className="px-4 py-12 text-center"><p className="text-sm font-medium">No matching task or section</p><p className="mt-1 text-xs text-[var(--muted)]">Try a page name, control, or review state.</p></div> : null}
        </div>
        <div className="hidden items-center justify-between border-t border-[var(--line)] px-4 py-2 text-[10px] text-[var(--muted)] sm:flex"><span>Results include visible sections on the current page</span><span>Esc to close</span></div>
      </section>
    </div>
  )
}

function CommandGroup({ label, children }: { label: string; children: ReactNode }) {
  return <div className="mb-2 last:mb-0"><p className="px-3 pb-1.5 pt-2 text-[10px] font-medium uppercase tracking-[0.1em] text-[var(--muted)]">{label}</p>{children}</div>
}

function CommandRow({ label, meta, onClick }: { label: string; meta: string; onClick: () => void }) {
  return <button type="button" onClick={onClick} className="flex min-h-12 w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left hover:bg-white/[0.055] focus-visible:bg-white/[0.055]"><span className="min-w-0 flex-1 truncate text-sm font-medium text-white">{label}</span><span className="text-[10px] text-[var(--muted)]">{meta}</span></button>
}
