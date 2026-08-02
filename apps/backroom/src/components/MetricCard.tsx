import type { LucideIcon } from 'lucide-react'

export function MetricCard({
  label,
  value,
  note,
  icon: Icon,
  tone = 'neutral',
}: {
  label: string
  value: string | number
  note: string
  icon: LucideIcon
  tone?: 'neutral' | 'acid' | 'amber' | 'cyan'
}) {
  const tones = {
    neutral: 'text-[var(--muted-strong)] bg-white/5',
    acid: 'text-[var(--acid)] bg-[var(--acid-dim)]',
    amber: 'text-[var(--amber)] bg-[var(--amber-dim)]',
    cyan: 'text-[var(--cyan)] bg-[var(--cyan-dim)]',
  }

  return (
    <div className="min-w-0 px-4 py-3.5 sm:px-5 sm:py-4">
      <div className="flex items-center gap-3">
        <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg ${tones[tone]}`}>
          <Icon className="h-4 w-4" />
        </span>
        <div className="min-w-0">
          <p className="text-[11px] font-medium text-[var(--muted)]">
            {label}
          </p>
          <p className="mt-0.5 text-2xl font-semibold tracking-[-0.03em]">{value}</p>
        </div>
      </div>
      <p className="mt-2 text-[11px] leading-4 text-[var(--muted)]">{note}</p>
    </div>
  )
}
