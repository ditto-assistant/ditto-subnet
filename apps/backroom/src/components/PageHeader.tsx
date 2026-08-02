import type { ReactNode } from 'react'

export function PageHeader({
  title,
  description,
  label,
  aside,
}: {
  title: string
  description: string
  label: string
  aside?: ReactNode
}) {
  return (
    <div className="flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
      <div className="min-w-0">
        <div className="mb-2.5 flex items-center gap-2 text-xs font-medium text-[var(--acid)]">
          <span className="h-1.5 w-1.5 rounded-full bg-[var(--acid)]" />
          {label}
        </div>
        <h2 className="text-balance text-[1.75rem] font-semibold leading-[1.15] tracking-[-0.035em] sm:text-[2rem]">
          {title}
        </h2>
        <p className="mt-2.5 max-w-[68ch] text-pretty text-sm leading-6 text-[var(--muted)]">
          {description}
        </p>
      </div>
      {aside ? <div className="shrink-0">{aside}</div> : null}
    </div>
  )
}
