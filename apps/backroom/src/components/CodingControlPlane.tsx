import { useServerFn } from '@tanstack/react-start'
import { AlertTriangle, CheckCircle2, Code2, RefreshCw, Rocket, ShieldCheck } from 'lucide-react'
import { useMemo, useState, type ReactNode } from 'react'
import {
  codingPrivateV2PublicationReceiptSchema,
  codingPrivateV2RegistrationConfirmation,
  codingPrivateV2RegistrationSchema,
  codingPrivateV2TransitionConfirmation,
  codingShadowReconciliationConfirmation,
  codingShadowTicketSetConfirmation,
  type AgentCodingShadowEvaluationStatus,
  type CodingCatalogControl,
  type CodingPrivateV2Releases,
  type CodingShadowReconciliationResponse,
  type CodingShadowTicketSetResponse,
} from '../lib/admin.schemas'
import {
  getAgentCodingShadowEvaluations,
  getCodingControlPlane,
  issueCodingShadowTicketSet,
  quarantineCodingPrivateV2Release,
  reconcileCodingShadowArtifact,
  registerCodingPrivateV2Release,
  retireCodingPrivateV2Release,
} from '../server/admin.functions'

export type CodingControlPlaneState = {
  catalog: CodingCatalogControl
  privateV2: CodingPrivateV2Releases
}

function shortDigest(value: string) {
  return `${value.slice(0, 10)}…${value.slice(-8)}`
}

function parseJson(value: string, label: string) {
  try {
    return JSON.parse(value) as unknown
  } catch {
    throw new Error(`${label} must be valid JSON`)
  }
}

function Notice({ children, tone = 'amber' }: { children: ReactNode; tone?: 'amber' | 'acid' | 'red' }) {
  const classes = {
    amber: 'border-[var(--amber)]/25 bg-[var(--amber-dim)] text-[var(--amber)]',
    acid: 'border-[var(--acid)]/25 bg-[var(--acid-dim)] text-[var(--acid)]',
    red: 'border-[var(--red)]/25 bg-[var(--red-dim)] text-[var(--red)]',
  }[tone]
  return <div className={`rounded-lg border px-4 py-3 text-xs leading-5 ${classes}`}>{children}</div>
}

function Field({ label, value, onChange, placeholder, type = 'text' }: {
  label: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  type?: 'text' | 'number'
}) {
  return (
    <label className="block text-xs text-[var(--muted-strong)]">
      {label}
      <input
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 font-mono text-xs text-[var(--fg)] outline-none focus:border-[var(--cyan)]"
      />
    </label>
  )
}

function TextArea({ label, value, onChange, rows = 4, placeholder }: {
  label: string
  value: string
  onChange: (value: string) => void
  rows?: number
  placeholder?: string
}) {
  return (
    <label className="block text-xs text-[var(--muted-strong)]">
      {label}
      <textarea
        value={value}
        rows={rows}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        className="mt-1.5 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 py-2 font-mono text-xs leading-5 text-[var(--fg)] outline-none focus:border-[var(--cyan)]"
      />
    </label>
  )
}

function ActionButton({ children, disabled, onClick, tone = 'cyan' }: {
  children: ReactNode
  disabled: boolean
  onClick: () => void
  tone?: 'cyan' | 'amber' | 'red'
}) {
  const classes = {
    cyan: 'border-[var(--cyan)]/35 bg-[var(--cyan-dim)] text-[var(--cyan)]',
    amber: 'border-[var(--amber)]/35 bg-[var(--amber-dim)] text-[var(--amber)]',
    red: 'border-[var(--red)]/35 bg-[var(--red-dim)] text-[var(--red)]',
  }[tone]
  return (
    <button type="button" disabled={disabled} onClick={onClick}
      className={`min-h-11 rounded-lg border px-4 text-xs font-semibold disabled:cursor-not-allowed disabled:opacity-40 ${classes}`}>
      {children}
    </button>
  )
}

export function CodingControlPlane({ initialState, readOnly }: {
  initialState: CodingControlPlaneState
  readOnly: boolean
}) {
  const fetchControl = useServerFn(getCodingControlPlane)
  const registerRelease = useServerFn(registerCodingPrivateV2Release)
  const quarantineRelease = useServerFn(quarantineCodingPrivateV2Release)
  const retireRelease = useServerFn(retireCodingPrivateV2Release)
  const reconcile = useServerFn(reconcileCodingShadowArtifact)
  const issueTickets = useServerFn(issueCodingShadowTicketSet)
  const fetchEvaluations = useServerFn(getAgentCodingShadowEvaluations)
  const [state, setState] = useState(initialState)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const [registrationJson, setRegistrationJson] = useState('')
  const [receiptJson, setReceiptJson] = useState('')
  const [publicKey, setPublicKey] = useState('')
  const [registerReason, setRegisterReason] = useState('')
  const [registerConfirmation, setRegisterConfirmation] = useState('')

  const [transitionId, setTransitionId] = useState('')
  const [transitionDigest, setTransitionDigest] = useState('')
  const [transitionAction, setTransitionAction] = useState<'QUARANTINE' | 'RETIRE'>('QUARANTINE')
  const [transitionReason, setTransitionReason] = useState('')
  const [transitionConfirmation, setTransitionConfirmation] = useState('')

  const [agentId, setAgentId] = useState('')
  const [benchVersion, setBenchVersion] = useState('12')
  const [corpusReleaseId, setCorpusReleaseId] = useState('')
  const [codingRunId, setCodingRunId] = useState('')
  const [reconcileReason, setReconcileReason] = useState('')
  const [reconcileConfirmation, setReconcileConfirmation] = useState('')
  const [reconciliation, setReconciliation] = useState<CodingShadowReconciliationResponse | null>(null)

  const [runRowId, setRunRowId] = useState('')
  const [ticketSetId, setTicketSetId] = useState('')
  const [validatorText, setValidatorText] = useState('')
  const [ticketReason, setTicketReason] = useState('')
  const [ticketConfirmation, setTicketConfirmation] = useState('')
  const [ticketSet, setTicketSet] = useState<CodingShadowTicketSetResponse | null>(null)
  const [evaluationAgentId, setEvaluationAgentId] = useState('')
  const [evaluations, setEvaluations] = useState<AgentCodingShadowEvaluationStatus | null>(null)

  const activeCatalogs = state.catalog.releases.filter((release) => !release.retired)
  const registrationPreview = useMemo(() => {
    if (!registrationJson.trim() || !receiptJson.trim()) return null
    try {
      return {
        registration: codingPrivateV2RegistrationSchema.parse(parseJson(registrationJson, 'Registration')),
        receipt: codingPrivateV2PublicationReceiptSchema.parse(parseJson(receiptJson, 'Publication receipt')),
      }
    } catch {
      return null
    }
  }, [receiptJson, registrationJson])
  const expectedRegisterConfirmation = registrationPreview
    ? codingPrivateV2RegistrationConfirmation(
        registrationPreview.registration.corpus_release_id,
        registrationPreview.registration.registration_sha256,
        registrationPreview.receipt.curator_signing_key_sha256,
      )
    : ''
  const expectedTransitionConfirmation = transitionId && transitionDigest
    ? codingPrivateV2TransitionConfirmation(transitionAction, transitionId, transitionDigest)
    : ''
  const parsedBenchVersion = Number(benchVersion)
  const expectedReconcileConfirmation = agentId && Number.isInteger(parsedBenchVersion)
    && corpusReleaseId && codingRunId
    ? codingShadowReconciliationConfirmation({ agentId, benchVersion: parsedBenchVersion, corpusReleaseId, codingRunId })
    : ''
  const validators = validatorText.split('\n').map((value) => value.trim()).filter(Boolean).sort()
  const expectedTicketConfirmation = runRowId && ticketSetId && validators.length === 3
    ? codingShadowTicketSetConfirmation({ runRowId, ticketSetId, validatorHotkeys: validators })
    : ''

  const execute = async (operation: () => Promise<void>) => {
    setBusy(true)
    setError('')
    setSuccess('')
    try {
      await operation()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Coding control operation failed')
    } finally {
      setBusy(false)
    }
  }

  const refresh = () => execute(async () => {
    setState(await fetchControl({ data: { limit: 50 } }))
    setSuccess('Coding control-plane state refreshed.')
  })

  const submitRegistration = () => execute(async () => {
    if (!registrationPreview) throw new Error('Registration and publication receipt must validate')
    const next = await registerRelease({ data: {
      registration: registrationPreview.registration,
      publicationReceipt: registrationPreview.receipt,
      curatorPublicKeyPem: publicKey,
      reason: registerReason,
      confirmation: registerConfirmation,
    } })
    setState((current) => ({ ...current, privateV2: next }))
    setSuccess(`Registered ${registrationPreview.registration.corpus_release_id}; it remains non-selectable and weight-zero.`)
  })

  const submitTransition = () => execute(async () => {
    const call = transitionAction === 'QUARANTINE' ? quarantineRelease : retireRelease
    const next = await call({ data: {
      corpusReleaseId: transitionId,
      expectedRegistrationSha256: transitionDigest,
      reason: transitionReason,
      confirmation: transitionConfirmation,
    } })
    setState((current) => ({ ...current, privateV2: next }))
    setSuccess(`${transitionAction === 'QUARANTINE' ? 'Quarantined' : 'Retired'} ${transitionId}.`)
  })

  const submitReconciliation = () => execute(async () => {
    const next = await reconcile({ data: {
      agentId,
      benchVersion: parsedBenchVersion,
      corpusReleaseId,
      codingRunId,
      reason: reconcileReason,
      confirmation: reconcileConfirmation,
    } })
    setReconciliation(next)
    if (next.run_row_id) setRunRowId(next.run_row_id)
    setSuccess(`Reconciliation reached ${next.state}; no validator tickets were issued by this step.`)
  })

  const submitTicketSet = () => execute(async () => {
    const next = await issueTickets({ data: {
      runRowId,
      ticketSetId,
      validatorHotkeys: validators,
      reason: ticketReason,
      confirmation: ticketConfirmation,
    } })
    setTicketSet(next)
    setSuccess(`Issued fixed k=3 ticket set ${next.ticket_set_id}; all results remain weight-zero.`)
  })

  const inspectEvaluations = () => execute(async () => {
    setEvaluations(await fetchEvaluations({ data: { agentId: evaluationAgentId, limit: 25 } }))
    setSuccess('Loaded the exact artifact-bound Coding ledger.')
  })

  return (
    <div className="mt-6 space-y-5">
      <section className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4 sm:p-5">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]"><Code2 className="h-4 w-4" /></div>
            <div><h2 className="text-sm font-semibold">Unified Coding state</h2><p className="mt-1 max-w-[72ch] text-xs leading-5 text-[var(--muted)]">Contract-v2 custody and contract-v1 execution remain separate authorities. This page coordinates both without turning registration into launch authority.</p></div>
          </div>
          <ActionButton disabled={busy} onClick={() => void refresh()}><span className="inline-flex items-center gap-2"><RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin' : ''}`} />Refresh</span></ActionButton>
        </div>
        <dl className="mt-5 grid gap-3 text-xs sm:grid-cols-4">
          <div><dt className="text-[var(--muted)]">Contract-v1 catalogs</dt><dd className="mt-1 text-lg font-semibold">{state.catalog.total}</dd></div>
          <div><dt className="text-[var(--muted)]">Active v1 catalogs</dt><dd className="mt-1 text-lg font-semibold">{activeCatalogs.length}</dd></div>
          <div><dt className="text-[var(--muted)]">Native v2 registrations</dt><dd className="mt-1 text-lg font-semibold">{state.privateV2.total}</dd></div>
          <div><dt className="text-[var(--muted)]">Reward eligibility</dt><dd className="mt-1 text-lg font-semibold text-[var(--acid)]">Always zero</dd></div>
        </dl>
        <div className="mt-4"><Notice>Backroom never receives an Ed25519 private key, RSA private key, Hippius credential, private task body, grader, patch, or object coordinate.</Notice></div>
      </section>

      {error ? <Notice tone="red"><AlertTriangle className="mr-2 inline h-4 w-4" />{error}</Notice> : null}
      {success ? <Notice tone="acid"><CheckCircle2 className="mr-2 inline h-4 w-4" />{success}</Notice> : null}

      <section className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4 sm:p-5">
        <div className="flex items-start gap-3"><ShieldCheck className="mt-0.5 h-4 w-4 text-[var(--acid)]" /><div><h2 className="text-sm font-semibold">Native private-v2 custody registry</h2><p className="mt-1 text-xs leading-5 text-[var(--muted)]">Register only after external signing and complete Hippius readback. Platform re-verifies every digest and Ed25519 signature.</p></div></div>
        <div className="mt-4 space-y-2">
          {state.privateV2.releases.length ? state.privateV2.releases.map((release) => (
            <button type="button" key={release.release_row_id} onClick={() => {
              setTransitionId(release.registration.corpus_release_id)
              setTransitionDigest(release.registration.registration_sha256)
              setTransitionConfirmation('')
            }} className="grid w-full gap-2 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3 py-3 text-left text-xs sm:grid-cols-[1fr_auto]">
              <span><span className="font-semibold">{release.registration.corpus_release_id}</span><span className="mt-1 block font-mono text-[10px] text-[var(--muted)]">registration {shortDigest(release.registration.registration_sha256)} · transport {shortDigest(release.registration.transport_sha256)}</span></span>
              <span className="text-[var(--muted-strong)]">{release.status} · {release.publication_object_count} objects</span>
            </button>
          )) : <p className="rounded-lg border border-dashed border-[var(--line)] p-4 text-xs text-[var(--muted)]">No native private-v2 release is registered.</p>}
        </div>

        <details className="mt-5 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4">
          <summary className="cursor-pointer text-xs font-semibold">Register externally signed release</summary>
          <div className="mt-4 grid gap-4">
            <TextArea label="Registration authority JSON" value={registrationJson} onChange={setRegistrationJson} rows={8} />
            <TextArea label="Complete publication receipt JSON" value={receiptJson} onChange={setReceiptJson} rows={8} />
            <TextArea label="Curator Ed25519 public key PEM" value={publicKey} onChange={setPublicKey} rows={5} placeholder="-----BEGIN PUBLIC KEY-----" />
            <Field label="Audit reason" value={registerReason} onChange={setRegisterReason} />
            {expectedRegisterConfirmation ? <p className="break-all rounded-lg border border-[var(--line)] p-3 font-mono text-[10px] text-[var(--muted-strong)]">{expectedRegisterConfirmation}</p> : <Notice>Valid registration and receipt JSON are required before the confirmation can be derived.</Notice>}
            <Field label="Exact confirmation" value={registerConfirmation} onChange={setRegisterConfirmation} />
            <ActionButton disabled={readOnly || busy || !registrationPreview || publicKey.length === 0 || registerReason.trim().length < 8 || registerConfirmation !== expectedRegisterConfirmation} onClick={() => void submitRegistration()}>Verify and register release</ActionButton>
          </div>
        </details>

        <details className="mt-3 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4">
          <summary className="cursor-pointer text-xs font-semibold">Quarantine or retire exact registration</summary>
          <div className="mt-4 grid gap-4 sm:grid-cols-2">
            <Field label="Corpus release ID" value={transitionId} onChange={setTransitionId} />
            <Field label="Expected registration SHA-256" value={transitionDigest} onChange={setTransitionDigest} />
            <label className="block text-xs text-[var(--muted-strong)]">Action<select value={transitionAction} onChange={(event) => { setTransitionAction(event.target.value as 'QUARANTINE' | 'RETIRE'); setTransitionConfirmation('') }} className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3"><option>QUARANTINE</option><option>RETIRE</option></select></label>
            <Field label="Audit reason" value={transitionReason} onChange={setTransitionReason} />
            <div className="sm:col-span-2"><p className="break-all rounded-lg border border-[var(--line)] p-3 font-mono text-[10px] text-[var(--muted-strong)]">{expectedTransitionConfirmation || 'Select an exact release above.'}</p></div>
            <div className="sm:col-span-2"><Field label="Exact confirmation" value={transitionConfirmation} onChange={setTransitionConfirmation} /></div>
            <div className="sm:col-span-2"><ActionButton tone={transitionAction === 'RETIRE' ? 'red' : 'amber'} disabled={readOnly || busy || transitionReason.trim().length < 8 || transitionConfirmation !== expectedTransitionConfirmation} onClick={() => void submitTransition()}>{transitionAction === 'RETIRE' ? 'Retire permanently' : 'Quarantine release'}</ActionButton></div>
          </div>
        </details>
      </section>

      <section className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4 sm:p-5">
        <div className="flex items-start gap-3"><Rocket className="mt-0.5 h-4 w-4 text-[var(--cyan)]" /><div><h2 className="text-sm font-semibold">Contract-v1 shadow launch</h2><p className="mt-1 text-xs leading-5 text-[var(--muted)]">First reconcile one exact qualified artifact into a future-height run. Ticket issuance is a separate k=3 transition.</p></div></div>
        <div className="mt-4"><Notice>These controls operate the established contract-v1 shadow ledger. Native private-v2 registrations remain non-selectable until a separately reviewed v2 activation layer exists.</Notice></div>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <Field label="Agent UUID" value={agentId} onChange={setAgentId} />
          <Field label="Benchmark version" type="number" value={benchVersion} onChange={setBenchVersion} />
          <label className="block text-xs text-[var(--muted-strong)]">Active contract-v1 corpus<select value={corpusReleaseId} onChange={(event) => setCorpusReleaseId(event.target.value)} className="mt-1.5 min-h-11 w-full rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] px-3"><option value="">Select exact release</option>{activeCatalogs.map((release) => <option key={release.release_row_id} value={release.commitment.corpus_release_id}>{release.commitment.corpus_release_id}</option>)}</select></label>
          <Field label="Unique coding run ID" value={codingRunId} onChange={setCodingRunId} />
          <div className="sm:col-span-2"><Field label="Audit reason" value={reconcileReason} onChange={setReconcileReason} /></div>
          <div className="sm:col-span-2"><p className="break-all rounded-lg border border-[var(--line)] p-3 font-mono text-[10px] text-[var(--muted-strong)]">{expectedReconcileConfirmation || 'Complete the exact run identity.'}</p></div>
          <div className="sm:col-span-2"><Field label="Exact confirmation" value={reconcileConfirmation} onChange={setReconcileConfirmation} /></div>
          <div className="sm:col-span-2"><ActionButton disabled={readOnly || busy || reconcileReason.trim().length < 8 || reconcileConfirmation !== expectedReconcileConfirmation} onClick={() => void submitReconciliation()}>Prepare shadow run</ActionButton></div>
        </div>
        {reconciliation ? <div className="mt-4"><Notice tone="acid">State {reconciliation.state}; assignment {shortDigest(reconciliation.assignment_row_id)}; selection block {reconciliation.selection_block_number}; run row {reconciliation.run_row_id ?? 'waiting for finality'}.</Notice></div> : null}
      </section>

      <section className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4 sm:p-5">
        <h2 className="text-sm font-semibold">Fixed k=3 ticket launch</h2>
        <p className="mt-1 text-xs leading-5 text-[var(--muted)]">Issue exactly three sorted, unique, certified validator tickets for one already-issued run. Validators still claim and execute independently.</p>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <Field label="Run row UUID" value={runRowId} onChange={setRunRowId} />
          <Field label="Ticket-set UUID" value={ticketSetId} onChange={setTicketSetId} placeholder="Enter independently approved UUID" />
          <div className="sm:col-span-2"><TextArea label="Validator hotkeys, one per line" value={validatorText} onChange={setValidatorText} rows={4} /></div>
          <div className="sm:col-span-2"><Field label="Audit reason" value={ticketReason} onChange={setTicketReason} /></div>
          <div className="sm:col-span-2"><p className="break-all rounded-lg border border-[var(--line)] p-3 font-mono text-[10px] text-[var(--muted-strong)]">{expectedTicketConfirmation || 'Supply one run, one ticket-set UUID and exactly three validator hotkeys.'}</p></div>
          <div className="sm:col-span-2"><Field label="Exact confirmation" value={ticketConfirmation} onChange={setTicketConfirmation} /></div>
          <div className="sm:col-span-2"><ActionButton tone="amber" disabled={readOnly || busy || ticketReason.trim().length < 8 || ticketConfirmation !== expectedTicketConfirmation} onClick={() => void submitTicketSet()}>Issue k=3 tickets</ActionButton></div>
        </div>
        {ticketSet ? <div className="mt-4"><Notice tone="acid">Ticket set {ticketSet.ticket_set_id} issued with {ticketSet.tickets.length} validators; idempotent replay: {String(ticketSet.idempotent)}.</Notice></div> : null}
      </section>

      <section className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-4 sm:p-5">
        <h2 className="text-sm font-semibold">Exact artifact run ledger</h2>
        <div className="mt-4 flex flex-col gap-3 sm:flex-row sm:items-end"><div className="flex-1"><Field label="Agent UUID" value={evaluationAgentId} onChange={setEvaluationAgentId} /></div><ActionButton disabled={busy || !evaluationAgentId} onClick={() => void inspectEvaluations()}>Inspect ledger</ActionButton></div>
        {evaluations ? <dl className="mt-4 grid gap-3 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4 text-xs sm:grid-cols-4"><div><dt className="text-[var(--muted)]">Agent</dt><dd className="mt-1 font-semibold">{evaluations.agent_name}</dd></div><div><dt className="text-[var(--muted)]">Artifact</dt><dd className="mt-1 font-mono">{shortDigest(evaluations.artifact_sha256)}</dd></div><div><dt className="text-[var(--muted)]">Assignments</dt><dd className="mt-1 font-semibold">{evaluations.total_assignments}</dd></div><div><dt className="text-[var(--muted)]">Runs</dt><dd className="mt-1 font-semibold">{evaluations.total_runs}</dd></div></dl> : null}
      </section>
    </div>
  )
}
