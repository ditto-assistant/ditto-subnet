import { useServerFn } from '@tanstack/react-start'
import { FileDiff, Loader2 } from 'lucide-react'
import { useState } from 'react'
import type { SourceDiffFile, SourceDiffManifest } from '../lib/admin.schemas'
import { sanitizeSourceLine } from '../lib/source-text'
import {
  getCopyReviewSourceDiff,
  getCopyReviewSourceDiffFile,
} from '../server/admin.functions'

const STATUS_STYLES: Record<SourceDiffFile['status'], string> = {
  added: 'text-[var(--acid)] border-[var(--acid)]/30 bg-[var(--acid-dim)]/40',
  removed: 'text-[var(--red)] border-[var(--red)]/30 bg-[var(--red-dim)]/40',
  modified: 'text-[var(--amber)] border-[var(--amber)]/30 bg-[var(--amber-dim)]/40',
  identical: 'text-[var(--muted)] border-white/10 bg-white/[0.03]',
}

// Show verbatim copies and near-copies first; identical-but-boilerplate files
// (mostly the shared starter kit) sink to the bottom where they don't distract.
const STATUS_ORDER: Record<SourceDiffFile['status'], number> = {
  modified: 0,
  added: 1,
  removed: 2,
  identical: 3,
}

function DiffBody({ lines }: { lines: Array<string> }) {
  return (
    <pre
      // Untrusted miner source: force LTR plaintext bidi isolation so
      // direction-control characters (rendered as visible escapes by
      // sanitizeSourceLine) can never visually reorder what is reviewed —
      // including reordering which side of the diff a line appears to be on.
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

export function CopyReviewSourceDiff({
  agentId,
  canView,
}: {
  agentId: string
  canView: boolean
}) {
  const manifestFn = useServerFn(getCopyReviewSourceDiff)
  const fileFn = useServerFn(getCopyReviewSourceDiffFile)
  const [manifest, setManifest] = useState<SourceDiffManifest | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
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
      setError(cause instanceof Error ? cause.message : 'Failed to load source diff')
    } finally {
      setLoading(false)
    }
  }

  async function toggleFile(path: string, status: SourceDiffFile['status']) {
    if (openPath === path) {
      setOpenPath(null)
      return
    }
    setOpenPath(path)
    setFileLines(null)
    setFileError(null)
    if (status === 'identical') {
      setFileLines([])
      return
    }
    setFileLoading(true)
    try {
      const detail = await fileFn({ data: { agentId, path } })
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
          Source diff vs. copied submission
        </h4>
        <p className="mt-3 text-xs text-[var(--muted)]">
          Viewing miner source requires write access.
        </p>
      </section>
    )
  }

  const sorted = manifest
    ? [...manifest.files].sort(
        (a, b) =>
          STATUS_ORDER[a.status] - STATUS_ORDER[b.status] ||
          a.path.localeCompare(b.path),
      )
    : []

  return (
    <section className="rounded-lg border border-white/10 p-4">
      <div className="flex items-center justify-between gap-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-[var(--muted)]">
          Source diff vs. copied submission
        </h4>
        {manifest ? (
          <span className="text-xs text-[var(--muted)]">
            {manifest.identical_count} identical · {manifest.modified_count} modified ·{' '}
            {manifest.added_count} added · {manifest.removed_count} removed
          </span>
        ) : null}
      </div>

      {!manifest ? (
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
          Load file-by-file diff
        </button>
      ) : null}

      {error ? <p className="mt-3 text-xs text-[var(--red)]">{error}</p> : null}

      {manifest ? (
        <div className="mt-3 space-y-1">
          {manifest.truncated ? (
            <p className="text-xs text-[var(--amber)]">
              Showing {manifest.files.length} of {manifest.file_count} files.
            </p>
          ) : null}
          {sorted.map((file) => (
            <div key={file.path} className="rounded-md border border-white/5">
              <button
                type="button"
                onClick={() => void toggleFile(file.path, file.status)}
                className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-xs transition-colors hover:bg-white/[0.04]"
              >
                <span className="flex items-center gap-2 truncate font-mono">
                  <span
                    className={`rounded border px-1.5 py-0.5 text-[10px] uppercase ${STATUS_STYLES[file.status]}`}
                  >
                    {file.status}
                  </span>
                  <span className="truncate">{file.path}</span>
                  {file.status === 'modified' && file.normalized_identical ? (
                    <span
                      className="rounded bg-[var(--red-dim)]/60 px-1.5 py-0.5 text-[10px] text-[var(--red)]"
                      title="Identical once comments and whitespace are canonicalized"
                    >
                      copy (reformatted)
                    </span>
                  ) : null}
                </span>
                <span className="shrink-0 font-mono text-[var(--muted)]">
                  {file.status === 'modified' ? (
                    <>
                      <span className="text-[var(--acid)]">+{file.added_lines}</span>{' '}
                      <span className="text-[var(--red)]">−{file.removed_lines}</span>
                    </>
                  ) : file.status === 'identical' ? (
                    '—'
                  ) : file.status === 'added' ? (
                    <span className="text-[var(--acid)]">+{file.added_lines}</span>
                  ) : (
                    <span className="text-[var(--red)]">−{file.removed_lines}</span>
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
                      Byte-for-byte identical to the copied submission.
                    </p>
                  ) : fileLines ? (
                    <DiffBody lines={fileLines} />
                  ) : null}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
    </section>
  )
}
