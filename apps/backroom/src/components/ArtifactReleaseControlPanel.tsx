import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, Clock3, EyeOff, RefreshCw } from 'lucide-react'
import {
  ARTIFACT_RELEASE_DEFAULT_HOURS,
  ARTIFACT_RELEASE_MAX_HOURS,
  ARTIFACT_RELEASE_MIN_HOURS,
  artifactReleaseConfirmation,
  artifactReleaseWindowGloss,
  type ArtifactReleaseControl,
  type SourceDisclosure,
} from '../lib/admin.schemas'
import {
  getArtifactReleaseControl,
  setArtifactReleaseSettings,
} from '../server/admin.functions'

// Longest first, so the row reads the same direction as the risk: the top-left
// stage is the most private, the bottom-right the most exposed. `never` sits
// above them all as its own control rather than as a ninth tile, because it is
// not a longer window — it is the end of the scale, and a tile row that ran
// "1 year, 30 days, …" with "never" among them would read as one more duration.
const stages = [8760, 720, 336, 168, 72, 48, 24, 12, 6] as const

function formatWhen(value: string | null) {
  if (!value) return 'Built-in default'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

export function ArtifactReleaseControlPanel({
  initialState,
  readOnly,
}: {
  initialState: ArtifactReleaseControl
  readOnly: boolean
}) {
  const refreshState = useServerFn(getArtifactReleaseControl)
  const applySettings = useServerFn(setArtifactReleaseSettings)
  const [state, setState] = useState(initialState)
  const [selectedHours, setSelectedHours] = useState<number | null>(null)
  const [selectedDisclosure, setSelectedDisclosure] = useState<SourceDisclosure>('public')
  const [customHours, setCustomHours] = useState('')
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const currentHours = state.current.embargo_hours
  const currentDisclosure = state.current.disclosure
  const withheld = currentDisclosure === 'never'
  const currentGloss = artifactReleaseWindowGloss(currentHours)
  const choosingNever = selectedDisclosure === 'never'
  // Only meaningful within a public policy; under `never` nothing is released
  // at all, so there is no window to shorten and no retroactive release.
  const shortening =
    !choosingNever && !withheld && selectedHours !== null && selectedHours < currentHours
  // A policy change is pending when the axis flips, or when the window moves
  // within a public policy. Returning to `public` always counts as a change,
  // even at the same hour count, because the axis itself is what moved.
  const pending =
    choosingNever !== withheld ||
    (!choosingNever && selectedHours !== null && selectedHours !== currentHours)
  const expectedConfirmation = choosingNever
    ? artifactReleaseConfirmation(currentHours, 'never')
    : selectedHours
      ? artifactReleaseConfirmation(selectedHours)
      : ''
  const selectedGloss =
    !choosingNever && selectedHours !== null
      ? artifactReleaseWindowGloss(selectedHours)
      : null
  const ready =
    pending &&
    (choosingNever || selectedHours !== null) &&
    reason.trim().length >= 8 &&
    confirmation === expectedConfirmation

  const resetReview = () => {
    setSelectedHours(null)
    setSelectedDisclosure(state.current.disclosure)
    setCustomHours('')
    setReason('')
    setConfirmation('')
  }

  const selectHours = (hours: number, fromCustom: boolean) => {
    setSelectedHours(hours)
    setSelectedDisclosure('public')
    if (!fromCustom) setCustomHours('')
    setReason('')
    setConfirmation('')
    setError('')
    setSuccess('')
  }

  const selectNever = () => {
    setSelectedDisclosure('never')
    setSelectedHours(null)
    setCustomHours('')
    setReason('')
    setConfirmation('')
    setError('')
    setSuccess('')
  }

  const onCustomChange = (raw: string) => {
    setCustomHours(raw)
    setSelectedDisclosure('public')
    setReason('')
    setConfirmation('')
    setError('')
    setSuccess('')
    const trimmed = raw.trim()
    if (trimmed === '') {
      setSelectedHours(null)
      return
    }
    const parsed = Number(trimmed)
    const valid =
      Number.isInteger(parsed) &&
      parsed >= ARTIFACT_RELEASE_MIN_HOURS &&
      parsed <= ARTIFACT_RELEASE_MAX_HOURS
    setSelectedHours(valid ? parsed : null)
  }

  const customInvalid = customHours.trim() !== '' && selectedHours === null
  const customGloss =
    customHours.trim() !== '' && selectedHours !== null
      ? artifactReleaseWindowGloss(selectedHours)
      : null

  const refresh = async () => {
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      setState(await refreshState())
      resetReview()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to refresh source-release policy')
    } finally {
      setLoading(false)
    }
  }

  const submit = async () => {
    if (!ready) return
    const wasShortening = shortening
    const goingDark = choosingNever
    // Under `never` the window is sent unchanged rather than omitted. The
    // platform requires it in range under every policy and retains it, so
    // returning to `public` restores the window the subnet last agreed on.
    const hours = goingDark ? currentHours : selectedHours
    if (hours === null) return
    setLoading(true)
    setError('')
    setSuccess('')
    try {
      const next = await applySettings({
        data: {
          expectedRevision: state.current.revision,
          disclosure: selectedDisclosure,
          embargoHours: hours,
          reason,
          confirmation,
        },
      })
      setState(next)
      setSuccess(
        goingDark
          ? 'Submitted source is no longer published. Anything already released stays public.'
          : withheld
            ? `Source release resumed on a ${hours}-hour window.`
            : wasShortening
              ? `Source embargo shortened to ${hours} hours.`
              : `Source embargo extended to ${hours} hours.`,
      )
      setSelectedHours(null)
      setSelectedDisclosure(next.current.disclosure)
      setCustomHours('')
      setReason('')
      setConfirmation('')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to update source-release policy')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="mt-6 space-y-5">
      <section className="overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="flex flex-col gap-4 border-b border-[var(--line)] p-4 sm:flex-row sm:items-start sm:justify-between sm:p-5">
          <div className="flex items-start gap-3">
            <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
              <Clock3 className="h-4 w-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold">Current privacy window</h2>
              <p className="mt-1 max-w-[70ch] text-xs leading-5 text-[var(--muted)]">
                Applies to the leaderboard king only — rank #1. No other miner&rsquo;s source is
                ever released. The window starts the moment an agent first takes the throne; even a
                brief stint as king triggers it.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void refresh()}
            disabled={loading}
            className="inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-lg border border-[var(--line)] px-3 text-xs font-medium text-[var(--muted-strong)] transition-colors hover:border-[var(--line-strong)] hover:bg-white/5 disabled:opacity-40"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            Refresh policy
          </button>
        </div>

        <div className="p-4 sm:p-5">
          <div className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <p className="text-[11px] font-medium uppercase tracking-[0.12em] text-[var(--muted)]">
                Effective policy
              </p>
              <p className="mt-2 text-3xl font-semibold tracking-tight">
                {withheld ? 'Never released' : `${currentHours} hours`}
              </p>
              {withheld ? (
                <p className="mt-1 text-xs text-[var(--muted)]">
                  No submitted source is published. The window below is retained and
                  applies again if release resumes.
                </p>
              ) : currentGloss ? (
                <p className="mt-1 text-xs text-[var(--muted)]">{currentGloss}</p>
              ) : null}
            </div>
            <dl className="grid gap-3 text-xs sm:grid-cols-2 sm:text-right">
              <div>
                <dt className="text-[var(--muted)]">Revision</dt>
                <dd className="mt-1 font-medium">{state.current.revision}</dd>
              </div>
              <div>
                <dt className="text-[var(--muted)]">Applied</dt>
                <dd className="mt-1 font-medium">{formatWhen(state.current.created_at)}</dd>
              </div>
            </dl>
          </div>

          <div className="mt-5 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-4 py-3 text-xs leading-5 text-[var(--amber)]">
            <div className="flex items-start gap-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
              <p>
                Shortening is retroactive. If the king has already held the throne longer than the
                new window, its source becomes downloadable immediately, and that cannot be reversed
                because released source cannot be made private again. Extending the window only holds
                the king&rsquo;s source private for longer; anything already released stays public.
              </p>
            </div>
          </div>

          <button
            type="button"
            disabled={readOnly || loading}
            onClick={() => (withheld ? selectHours(currentHours, false) : selectNever())}
            aria-pressed={withheld}
            className={`mt-5 flex min-h-16 w-full items-start gap-3 rounded-lg border px-4 py-3 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
              withheld
                ? 'border-[var(--acid)]/30 bg-[var(--acid-dim)]'
                : choosingNever
                  ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                  : 'border-[var(--line)] bg-[var(--panel-soft)] hover:border-[var(--line-strong)]'
            }`}
          >
            <EyeOff className="mt-0.5 h-4 w-4 shrink-0 text-[var(--muted-strong)]" />
            <span>
              <span className="block text-sm font-semibold">
                Never release source
              </span>
              <span className="mt-1 block text-[11px] leading-5 text-[var(--muted)]">
                {withheld
                  ? 'Current policy. Select a window below to resume publishing.'
                  : 'No submission’s source is ever published, however long the window is set to. The screener, the three validators and copy review still read it, so scoring and plagiarism checks are unchanged.'}
              </span>
            </span>
          </button>

          <div className="mt-3 grid gap-2 sm:grid-cols-4">
            {stages.map((hours) => {
              // Under `never` no window is in force, so no tile is "current" —
              // marking one would imply source is being released on it.
              const current = !withheld && hours === currentHours
              const gloss = artifactReleaseWindowGloss(hours)
              return (
                <button
                  key={hours}
                  type="button"
                  disabled={readOnly || loading}
                  onClick={() => selectHours(hours, false)}
                  className={`min-h-16 rounded-lg border px-4 text-left transition-colors disabled:cursor-not-allowed disabled:opacity-45 ${
                    current
                      ? 'border-[var(--acid)]/30 bg-[var(--acid-dim)]'
                      : !choosingNever &&
                          selectedHours === hours &&
                          customHours.trim() === ''
                        ? 'border-[var(--amber)]/40 bg-[var(--amber-dim)]'
                        : 'border-[var(--line)] bg-[var(--panel-soft)] hover:border-[var(--line-strong)]'
                  }`}
                >
                  <span className="block text-sm font-semibold">
                    {hours} hours
                    {gloss ? (
                      <span className="ml-1.5 font-normal text-[var(--muted)]">{gloss}</span>
                    ) : null}
                  </span>
                  <span className="mt-1 block text-[11px] text-[var(--muted)]">
                    {current
                      ? 'Current stage'
                      : withheld
                        ? 'Resume release on this window'
                        : hours < currentHours
                          ? 'Shorten to this stage'
                          : 'Extend to this stage'}
                  </span>
                </button>
              )
            })}
          </div>

          <div className="mt-4">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Custom window ({ARTIFACT_RELEASE_MIN_HOURS}–{ARTIFACT_RELEASE_MAX_HOURS} hours)
              <div className="mt-2 flex items-center gap-2">
                <input
                  type="number"
                  inputMode="numeric"
                  min={ARTIFACT_RELEASE_MIN_HOURS}
                  max={ARTIFACT_RELEASE_MAX_HOURS}
                  step={1}
                  value={customHours}
                  disabled={readOnly || loading}
                  onChange={(event) => onCustomChange(event.target.value)}
                  placeholder="e.g. 168"
                  aria-invalid={customInvalid}
                  className={`min-h-11 w-full max-w-[12rem] rounded-lg border bg-[var(--ink)] px-3 text-sm text-white outline-none transition-colors focus:border-[var(--amber)] disabled:cursor-not-allowed disabled:opacity-45 ${
                    customInvalid ? 'border-[var(--red)]' : 'border-[var(--line)]'
                  }`}
                />
                <span className="text-xs text-[var(--muted)]">
                  hours{customGloss ? ` · ${customGloss}` : ''}
                </span>
              </div>
            </label>
            {customInvalid ? (
              <p className="mt-2 text-[11px] text-[var(--red)]">
                Enter a whole number between {ARTIFACT_RELEASE_MIN_HOURS} and{' '}
                {ARTIFACT_RELEASE_MAX_HOURS} hours.
              </p>
            ) : null}
          </div>

          {readOnly ? (
            <p className="mt-4 text-xs text-[var(--muted)]">
              Your Backroom account is read only. An editor must apply policy changes.
            </p>
          ) : null}
        </div>
      </section>

      {pending ? (
        <section className="rounded-xl border border-[var(--amber)]/25 bg-[var(--panel)] p-4 sm:p-5">
          <h2 className="text-sm font-semibold">
            {choosingNever ? (
              'Confirm never releasing source'
            ) : (
              <>
                Confirm {selectedHours}-hour {shortening ? 'release' : 'window'}
                {selectedGloss ? (
                  <span className="ml-1.5 font-normal text-[var(--muted)]">
                    ({selectedGloss})
                  </span>
                ) : null}
              </>
            )}
          </h2>
          <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
            {choosingNever
              ? 'This stops publishing every submission’s source, with no end date.'
              : withheld
                ? 'This resumes publishing; eligible source past the window is released immediately.'
                : shortening
                  ? 'This shortens the window and releases eligible source immediately.'
                  : 'This extends the window; submissions stay private for longer.'}{' '}
            Record why this stage is safe, then type the exact confirmation phrase.
          </p>
          {choosingNever ? (
            <div className="mt-3 rounded-lg border border-[var(--amber)]/25 bg-[var(--amber-dim)] px-4 py-3 text-xs leading-5 text-[var(--amber)]">
              <div className="flex items-start gap-3">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <p>
                  The champion&rsquo;s source being inspectable is what lets anyone outside
                  SN118 check a crown is not a repackaged copy. Under this policy that check
                  is ours alone &mdash; the anti-copy gate and operator review. Source
                  already released stays public; this is not a recall.
                </p>
              </div>
            </div>
          ) : null}
          {!choosingNever &&
          selectedHours !== null &&
          selectedHours > ARTIFACT_RELEASE_DEFAULT_HOURS ? (
            <p className="mt-3 text-xs leading-5 text-[var(--amber)]">
              Past the {ARTIFACT_RELEASE_DEFAULT_HOURS}-hour window SN118 agreed on. Nothing
              breaks, but the king&rsquo;s source stays private longer than miners expect, so
              say who asked for it and when it should go back.
            </p>
          ) : null}
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Operator reason
              <textarea
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                rows={3}
                className="mt-2 w-full rounded-lg border border-[var(--line)] bg-[var(--ink)] px-3 py-2.5 text-sm text-white outline-none transition-colors focus:border-[var(--amber)]"
              />
            </label>
            <label className="text-xs font-medium text-[var(--muted-strong)]">
              Type {expectedConfirmation}
              <input
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                className="mt-2 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--ink)] px-3 text-sm text-white outline-none transition-colors focus:border-[var(--amber)]"
              />
            </label>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <button
              type="button"
              disabled={!ready || loading}
              onClick={() => void submit()}
              className="min-h-11 rounded-lg bg-[var(--amber)] px-4 text-xs font-semibold text-[var(--ink)] transition-opacity disabled:opacity-40"
            >
              {choosingNever
                ? 'Stop releasing source'
                : withheld
                  ? 'Resume releasing source'
                  : shortening
                    ? 'Shorten embargo'
                    : 'Extend embargo'}
            </button>
            <button
              type="button"
              disabled={loading}
              onClick={resetReview}
              className="min-h-11 rounded-lg border border-[var(--line)] px-4 text-xs font-medium text-[var(--muted-strong)] hover:bg-white/5"
            >
              Cancel
            </button>
          </div>
        </section>
      ) : null}

      {error ? (
        <p role="alert" className="rounded-lg border border-[var(--red)]/25 bg-[var(--red-dim)] px-4 py-3 text-xs text-[var(--red)]">
          {error}
        </p>
      ) : null}
      {success ? (
        <p role="status" className="flex items-center gap-2 rounded-lg border border-[var(--acid)]/25 bg-[var(--acid-dim)] px-4 py-3 text-xs text-[var(--acid)]">
          <CheckCircle2 className="h-4 w-4" />
          {success}
        </p>
      ) : null}
    </div>
  )
}
