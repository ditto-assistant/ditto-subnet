import { useServerFn } from '@tanstack/react-start'
import { FileDiff, Loader2 } from 'lucide-react'
import { useState } from 'react'
import type { BaselineDiffFile, BaselineDiffManifest } from '../lib/admin.schemas'
import { sanitizeSourceLine } from '../lib/source-text'
import {
  getScreeningBaselineDiff,
  readScreeningBaselineDiffFile,
} from '../server/admin.functions'

const STATUS_STYLES: Record<BaselineDiffFile['status'], string> = {
  added: 'text-[var(--acid)] border-[var(--acid)]/30 bg-[var(--acid-dim)]/40',
  removed: 'text-[var(--red)] border-[var(--red)]/30 bg-[var(--red-dim)]/40',
  modified: 'text-[var(--amber)] border-[var(--amber)]/30 bg-[var(--amber-dim)]/40',
  identical: 'text-[var(--muted)] border-white/10 bg-white/[0.03]',
}

// Custom code first, then the miner's edits to kit files. Stock kit code is
// hidden entirely by default: it is the bulk of every submission and none of it
// is the miner's work, so surfacing it buries the part under review.
const STATUS_ORDER: Record<BaselineDiffFile['status'], number> = {
  added: 0,
  modified: 1,
  removed: 2,
  identical: 3,
}

function DiffBody({ lines }: { lines: Array<string> }) {
  return (
    <pre
      // Untrusted miner source: force LTR plaintext bidi isolation so
      // direction-control characters (rendered as visible escapes by
      // sanitizeSourceLine) can never visually reorder what is reviewed.
      dir="ltr"
      style={{ unicodeBidi: 'plaintext' }}
      className="mt-2 max-h-96 overflow-auto rounded-md border border-white/10 bg-black/40 p-3 text-xs leading-5"
    >
      {lines.map((line, index) => {
        const tone = line.startsWith('+')
          ? 'text-[var(--acid)]'
          : line.startsWith('-')
            ? 'text-[var(--red)]'
            : line.startsWith('@@')
              ? 'text-[var(--amber)]'
              : 'text-[var(--muted-strong)]'
        return (
          <div
            key={index}
            dir="ltr"
            style={{ unicodeBidi: 'isolate' }}
            className={`whitespace-pre font-mono ${tone}`}
          >
            {sanitizeSourceLine(line) || ' '}
          </div>
        )
      })}
    </pre>
  )
}

export function QuarantineBaselineDiff({
  agentId,
  canView,
}: {
  agentId: string
  canView: boolean
}) {
  const manifestFn = useServerFn(getScreeningBaselineDiff)
  const fileFn = useServerFn(readScreeningBaselineDiffFile)
  const [manifest, setManifest] = useState<BaselineDiffManifest | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [showStock, setShowStock] = useState(false)
  const [openPath, setOpenPath] = useState<string | null>(null)
  const [fileLines, setFileLines] = useState<Array<string> | null>(null)
  const [fileError, setFileError] = useState<string | null>(null)
  const [fileLoading, setFileLoading] = useState(false)

  async function loadManifest() {
    setLoading(true)
    setError(null)
    try {
      setManifest(await manifestFn({ data: { agentId } }))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Failed to load baseline diff')
    } finally {
      setLoading(false)
    }
  }

  async function toggleFile(file: BaselineDiffFile) {
    if (openPath === file.path) {
      setOpenPath(null)
      return
    }
    setOpenPath(file.path)
    setFileLines(null)
    setFileError(null)
    if (file.status === 'identical') {
      setFileLines([])
      return
    }
    setFileLoading(true)
    try {
      const detail = await fileFn({ data: { agentId, path: file.path } })
      setFileLines(detail.diff_lines)
    } catch (cause) {
      setFileError(cause instanceof Error ? cause.message : 'Failed to load file diff')
    } finally {
      setFileLoading(false)
    }
  }

  if (!canView) {
    return (
      <section className="rounded-lg border border-white/10 p-4">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Diff vs. starter kit
        </h4>
        <p className="mt-3 text-xs text-[var(--muted)]">
          Viewing miner source requires write access.
        </p>
      </section>
    )
  }

  const visible = manifest
    ? [...manifest.files]
        .filter((file) => showStock || !file.stock_kit)
        .sort(
          (a, b) =>
            STATUS_ORDER[a.status] - STATUS_ORDER[b.status] ||
            b.added_lines - a.added_lines ||
            a.path.localeCompare(b.path),
        )
    : []
  const hiddenStock = manifest ? manifest.stock_kit_count : 0

  return (
    <section className="rounded-lg border border-white/10 p-4">
      <div className="flex items-center justify-between gap-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Diff vs. starter kit
        </h4>
        {manifest ? (
          <span className="text-xs text-[var(--muted)]">
            {manifest.file_count} files · {manifest.stock_kit_count} stock kit
          </span>
        ) : null}
      </div>

      {!manifest ? (
        <>
          <p className="mt-2 text-xs text-[var(--muted)]">
            Subtract the harness every miner starts from, leaving only what this one
            wrote.
          </p>
          <button
            type="button"
            onClick={() => void loadManifest()}
            disabled={loading}
            className="mt-3 inline-flex items-center gap-2 rounded-lg border border-white/10 px-3 py-2 text-xs font-medium text-[var(--muted-strong)] transition-colors hover:bg-white/[0.04] disabled:opacity-50"
          >
            {loading ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <FileDiff className="h-3.5 w-3.5" />
            )}
            Load diff vs. starter kit
          </button>
        </>
      ) : null}

      {error ? <p className="mt-3 text-xs text-[var(--red)]">{error}</p> : null}

      {manifest ? (
        <>
          {/* The headline an operator triages on: a kit variant with a handful of
              custom lines reads very differently from a real custom harness. */}
          <div className="mt-3 rounded-lg border border-white/10 bg-white/[0.02] px-3 py-2.5">
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="font-mono text-lg text-[var(--acid)]">
                {manifest.custom_added_lines.toLocaleString()}
              </span>
              <span className="text-xs text-[var(--muted-strong)]">
                custom lines across {manifest.custom_file_count}{' '}
                {manifest.custom_file_count === 1 ? 'file' : 'files'}
              </span>
            </div>
            <p className="mt-1 text-[11px] text-[var(--muted)]">
              Lines that are neither baseline nor starter-kit code at any revision.
              Baseline {manifest.baseline.revision.slice(0, 12)} ·{' '}
              {manifest.baseline.commit_count} commits.
            </p>
            {manifest.path_aligned ? (
              <p className="mt-1 text-[11px] text-[var(--amber)]">
                Paths realigned by stripping one wrapping directory to match the kit
                layout.
              </p>
            ) : null}
          </div>

          {manifest.truncated ? (
            <p className="mt-3 text-xs text-[var(--amber)]">
              Showing {manifest.files.length} of {manifest.file_count} files.
            </p>
          ) : null}

          {hiddenStock > 0 ? (
            <button
              type="button"
              onClick={() => setShowStock((current) => !current)}
              className="mt-3 text-xs text-[var(--muted)] underline-offset-2 transition-colors hover:text-[var(--muted-strong)] hover:underline"
            >
              {showStock
                ? `Hide ${hiddenStock} stock kit ${hiddenStock === 1 ? 'file' : 'files'}`
                : `Show ${hiddenStock} stock kit ${hiddenStock === 1 ? 'file' : 'files'}`}
            </button>
          ) : null}

          <div className="mt-3 space-y-1">
            {visible.length === 0 ? (
              <p className="text-xs text-[var(--muted)]">
                No files differ from the starter kit.
              </p>
            ) : null}
            {visible.map((file) => (
              <div key={file.path} className="rounded-md border border-white/5">
                <button
                  type="button"
                  onClick={() => void toggleFile(file)}
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs transition-colors hover:bg-white/[0.04]"
                >
                  <span className="flex items-center gap-2 truncate font-mono">
                    <span
                      className={`rounded border px-1.5 py-0.5 text-[10px] uppercase ${STATUS_STYLES[file.status]}`}
                    >
                      {file.status}
                    </span>
                    <span className="truncate">{file.path}</span>
                    {file.stock_kit && file.status !== 'identical' ? (
                      <span
                        className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10px] text-[var(--muted)]"
                        title="Matches starter-kit code at another revision — not the miner's work"
                      >
                        stock kit
                      </span>
                    ) : null}
                  </span>
                  <span className="shrink-0 font-mono text-[var(--muted)]">
                    {file.status === 'identical' ? (
                      '—'
                    ) : file.status === 'removed' ? (
                      <span className="text-[var(--red)]">−{file.removed_lines}</span>
                    ) : (
                      <>
                        <span className="text-[var(--acid)]">+{file.added_lines}</span>{' '}
                        <span className="text-[var(--red)]">−{file.removed_lines}</span>
                      </>
                    )}
                  </span>
                </button>
                {openPath === file.path ? (
                  <div className="border-t border-white/5 px-3 pb-3">
                    {fileLoading ? (
                      <p className="mt-2 flex items-center gap-2 text-xs text-[var(--muted)]">
                        <Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading diff…
                      </p>
                    ) : fileError ? (
                      <p className="mt-2 text-xs text-[var(--red)]">{fileError}</p>
                    ) : file.status === 'identical' ? (
                      <p className="mt-2 text-xs text-[var(--muted)]">
                        Byte-for-byte identical to the starter kit.
                      </p>
                    ) : fileLines ? (
                      <DiffBody lines={fileLines} />
                    ) : null}
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        </>
      ) : null}
    </section>
  )
}
