import { useEffect, useRef, useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import type { z } from 'zod'
import { alphaDisplay, alphaRao, manualControlSchema, manualPreviewSchema } from '../lib/treasury-manual.schemas'
import { getManualTransfers, previewManualTransfer, queueManualTransfer } from '../server/treasury-manual.functions'

type Control = z.infer<typeof manualControlSchema>
type Preview = z.infer<typeof manualPreviewSchema>
const inputClass = 'w-full rounded border border-[var(--line)] bg-[var(--panel)] p-2 text-sm'
const statusLabels = {
  queued: 'Queued for custody', dispatched: 'Delivered to custody queue',
  pending: 'Delivery unresolved — funds will not be resent',
  audit_pending: 'Finalized — public receipt verification pending',
  published: 'Finalized — public receipt published',
  failed: 'Failed — no automatic replacement', refused: 'Refused — check delivery details',
}

export function TreasuryManualTransferPanel({ initialState, readOnly }: { initialState: Control; readOnly: boolean }) {
  const refresh = useServerFn(getManualTransfers)
  const previewTransfer = useServerFn(previewManualTransfer)
  const queue = useServerFn(queueManualTransfer)
  const [state, setState] = useState(initialState)
  const [bucket, setBucket] = useState(initialState.destinations.find(d => d.allocation_bps > 0)?.bucket_id ?? '')
  const [amount, setAmount] = useState('')
  const [reserve, setReserve] = useState('')
  const [reason, setReason] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [requestId, setRequestId] = useState(() => crypto.randomUUID())
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const refreshGeneration = useRef(0)
  const load = async () => {
    const generation = ++refreshGeneration.current
    try {
      const nextState = await refresh()
      if (generation === refreshGeneration.current) { setState(nextState); setError('') }
    } catch {
      if (generation === refreshGeneration.current) setError('Transfer status unavailable. Refresh before confirming another transfer.')
    }
  }
  useEffect(() => {
    const timer = setInterval(() => { void load() }, 15000)
    return () => { clearInterval(timer); ++refreshGeneration.current }
  }, [refresh]) // eslint-disable-line react-hooks/exhaustive-deps
  const invalidate = () => { setPreview(null); setConfirmation(''); setMessage('') }
  const expected = preview ? `TRANSFER ${alphaDisplay(preview.envelope.request.amount_rao)} ALPHA TO ${preview.envelope.request.bucket_id.toUpperCase()}` : ''
  const blocked = readOnly || busy || !!state.blocked_reason || !!error
  const previewDestinationEnabled = !!preview && state.destinations.some(d =>
    d.bucket_id === preview.envelope.request.bucket_id &&
    d.holding_coldkey === preview.envelope.destination && d.allocation_bps > 0)
  return <section className="mt-8 space-y-4 border-t border-[var(--line)] pt-6" aria-label="Manual collector transfers">
    <h2 className="text-lg font-semibold">Transfer to a service wallet</h2>
    <p className="text-sm text-[var(--muted-strong)]">Send SN118 alpha once from the collector to an approved holding wallet. Keep a minimum stake reserve. Recurring transfers are off. A wallet transfer does not prove purchased service credits.</p>
    <button className={inputClass} disabled={busy} onClick={() => { void load() }}>Refresh transfer status</button>
    {state.blocked_reason && <p role="status">{state.blocked_reason}</p>}
    {state.readiness && <p className="text-sm">Collector stake: {alphaDisplay(state.readiness.available_alpha_rao)} alpha · observed finalized block {state.readiness.finalized_block}</p>}
    <fieldset disabled={blocked} className="grid gap-4 md:grid-cols-2">
      <label>Destination wallet<select aria-label="Destination wallet" className={inputClass} value={bucket} onChange={e => { setBucket(e.target.value); invalidate() }}>
        {state.destinations.map(d => <option key={d.bucket_id} value={d.bucket_id} disabled={d.allocation_bps === 0}>{d.bucket_id.toUpperCase()}{d.allocation_bps === 0 ? ' — allocation off' : ''}</option>)}
      </select></label>
      <label>Amount (SN118 alpha)<input className={inputClass} inputMode="decimal" value={amount} onChange={e => { setAmount(e.target.value); invalidate() }} /></label>
      <label>Minimum alpha to retain staked<input className={inputClass} inputMode="decimal" value={reserve} onChange={e => { setReserve(e.target.value); invalidate() }} /></label>
      <label>Audit reason<input className={inputClass} maxLength={240} value={reason} onChange={e => { setReason(e.target.value); invalidate() }} /></label>
      <button className={inputClass} disabled={!alphaRao(amount) || !alphaRao(reserve) || reason.trim().length < 8 || !state.destinations.some(d => d.bucket_id === bucket && d.allocation_bps > 0)} onClick={async () => {
        setBusy(true); setError(''); setMessage('')
        try { setPreview(await previewTransfer({ data: { request_id: requestId, bucket_id: bucket, amount_rao: alphaRao(amount)!, retained_alpha_rao: alphaRao(reserve)!, reason: reason.trim() } })); setConfirmation('') }
        catch (cause) { setError(cause instanceof Error ? cause.message : 'Transfer preview unavailable') }
        finally { setBusy(false) }
      }}>Preview manual transfer</button>
    </fieldset>
    {preview && <div className="space-y-3 rounded border border-[var(--line)] p-4">
      <p>Send {alphaDisplay(preview.envelope.request.amount_rao)} alpha to {preview.envelope.request.bucket_id.toUpperCase()}; retain at least {alphaDisplay(preview.envelope.request.retained_alpha_rao)} alpha.</p>
      <p className="break-all font-mono text-xs">{preview.envelope.destination}</p>
      <p className="text-sm">Expires at finalized block {preview.envelope.request.expires_block}. Custody rechecks balances, reserve, signed destination and previous delivery before signing.</p>
      <label>Type “{expected}”<input className={inputClass} value={confirmation} disabled={readOnly || busy} onChange={e => setConfirmation(e.target.value)} /></label>
      {!previewDestinationEnabled && <p role="status">The destination wallet or allocation changed. Create a new preview before transferring.</p>}
      <button className={inputClass} disabled={blocked || confirmation !== expected || !previewDestinationEnabled} onClick={async () => {
        setBusy(true); setError('')
        try {
          const result = await queue({ data: { envelope: preview.envelope, confirmation_digest: preview.confirmation_digest, confirmation: 'TRANSFER SN118 ALPHA ONCE' } })
          setMessage(`${statusLabels[result.status]}. Track this request below.`); setPreview(null); setConfirmation(''); setRequestId(crypto.randomUUID()); await load()
        } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to queue transfer; retry this same confirmation') }
        finally { setBusy(false) }
      }}>Transfer once</button>
    </div>}
    {error && <p role="alert" className="text-[var(--red)]">{error}</p>}
    {message && <p role="status">{message}</p>}
    <ul className="space-y-3">{state.requests.map(r => <li key={r.request_id} className="rounded border border-[var(--line)] p-3 text-sm">
      <p>{alphaDisplay(r.envelope.request.amount_rao)} alpha → {r.envelope.request.bucket_id.toUpperCase()} · {statusLabels[r.status]}</p>
      <p className="break-all text-xs">Request {r.request_id} · {r.actor}</p>
      {r.last_error && <p>{r.last_error}</p>}
      {r.receipt && <><p className="break-all font-mono text-xs">Transaction {r.receipt.extrinsic_hash}</p><a href="https://dittobench.ai/gamma" target="_blank" rel="noreferrer">View public Gamma receipt</a></>}
    </li>)}</ul>
  </section>
}
