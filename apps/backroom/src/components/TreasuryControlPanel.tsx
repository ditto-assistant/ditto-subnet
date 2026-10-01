import { useState } from 'react'
import { useServerFn } from '@tanstack/react-start'
import type { z } from 'zod'
import { treasuryControlSchema, treasurySettingsSchema } from '../lib/treasury.schemas'
import { getTreasurySettings, saveTreasurySettings } from '../server/treasury.functions'

type Settings = z.infer<typeof treasurySettingsSchema>
type Bucket = Settings['service_buckets'][number]
type Control = z.infer<typeof treasuryControlSchema>
const inputClass = 'w-full rounded border border-[var(--line)] bg-[var(--panel)] p-2 text-sm'
const blankBucket = (bucket_id: string, purpose: string): Bucket => ({
  bucket_id,
  purpose,
  allocation_bps: 0,
  holding_coldkey: null,
  service_account_ref: null,
  publish_payments: true,
  payee_rules: [],
})

function editable(settings: Settings): Settings {
  if (settings.allocation_version === 2) return structuredClone(settings)
  return {
    ...settings,
    allocation_version: 2,
    maintenance_bps: 0,
    gm_bps: 0,
    gm_account_ref: null,
    max_daily_outflow_rao: 0,
    max_single_topup_rao: 0,
    max_slippage_bps: 0,
    service_buckets: [
      blankBucket('gm_credits', 'GM inference credits'),
      blankBucket('bitsec_audits', 'Bitsec security audits'),
      blankBucket('bitcast_ads', 'Bitcast advertising campaigns'),
    ],
  }
}

export function TreasuryControlPanel({
  initialState,
  readOnly,
}: {
  initialState: Control
  readOnly: boolean
}) {
  const refresh = useServerFn(getTreasurySettings)
  const save = useServerFn(saveTreasurySettings)
  const [state, setState] = useState(initialState)
  const [settings, setSettings] = useState(() => editable(initialState.effective))
  const [reason, setReason] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const total = settings.service_buckets.reduce((sum, bucket) => sum + bucket.allocation_bps, 0)
  const parsed = treasurySettingsSchema.safeParse(settings)
  const updateBucket = (index: number, change: Partial<Bucket>) => {
    setSettings((current) => ({
      ...current,
      service_buckets: current.service_buckets.map((bucket, i) =>
        i === index ? { ...bucket, ...change } : bucket,
      ),
    }))
    setConfirmation('')
  }
  const load = (next: Control) => {
    setState(next)
    setSettings(editable(next.effective))
    setConfirmation('')
    setReason('')
  }
  const submit = async () => {
    if (
      readOnly ||
      !parsed.success ||
      reason.trim().length < 8 ||
      confirmation !== 'RECORD TREASURY SHADOW POLICY'
    )
      return
    setBusy(true)
    setError('')
    setMessage('')
    try {
      load(
        await save({
          data: {
            expectedRevision: state.revision,
            settings: parsed.data,
            reason,
            confirmation,
          },
        }),
      )
      setMessage('Wallet policy recorded. Funding, distribution, and payment observation remain inactive.')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unable to record treasury policy')
    } finally {
      setBusy(false)
    }
  }
  return (
    <section
      className="mt-8 space-y-5 border-t border-[var(--line)] pt-6"
      aria-label="Service treasury wallets"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">Service treasury wallets</h2>
          <p className="mt-2 max-w-[70ch] text-sm text-[var(--muted-strong)]">
            One collector, a shared 1,000 bps service pool, and separately controlled holding wallets. Burn
            applies only to the miner remainder after service allocation. This policy is shadow only.
          </p>
        </div>
        <button
          className={inputClass}
          disabled={busy}
          onClick={async () => {
            setBusy(true)
            setError('')
            setMessage('')
            try {
              load(await refresh())
            } catch (cause) {
              setError(cause instanceof Error ? cause.message : 'Unable to refresh policy')
            } finally {
              setBusy(false)
            }
          }}
        >
          Refresh policy
        </button>
      </div>
      <p className="text-sm">
        Revision {state.revision} · Configured pool {total} / 1,000 bps · Effective funding 0% · No active
        sweep
      </p>
      {error && (
        <p role="alert" className="text-[var(--red)]">
          {error}
        </p>
      )}
      {message && <p role="status">{message}</p>}
      <fieldset disabled={readOnly || busy} className="space-y-5">
        <legend className="mb-3 font-semibold">Collector</legend>
        <div className="grid gap-4 sm:grid-cols-2">
          <label>
            Collector hotkey
            <input
              className={inputClass}
              value={settings.treasury_hotkey ?? ''}
              onChange={(event) => {
                setSettings({
                  ...settings,
                  treasury_hotkey: event.target.value || null,
                })
                setConfirmation('')
              }}
            />
          </label>
          <label>
            Collector coldkey
            <input
              className={inputClass}
              value={settings.treasury_coldkey ?? ''}
              onChange={(event) => {
                setSettings({
                  ...settings,
                  treasury_coldkey: event.target.value || null,
                })
                setConfirmation('')
              }}
            />
          </label>
          <label>
            Distribution interval (hours)
            <input
              className={inputClass}
              type="number"
              min="1"
              max="168"
              value={settings.sweep_interval_hours}
              onChange={(event) => {
                setSettings({
                  ...settings,
                  sweep_interval_hours: Number(event.target.value),
                })
                setConfirmation('')
              }}
            />
          </label>
        </div>
        <p className="text-sm text-[var(--muted)]">
          Public addresses only. Create and back up holding-wallet seeds yourself. Platform and Backroom never
          store them. Collector registration and signing are a separate activation step.
        </p>
        {settings.service_buckets.map((bucket, index) => (
          <fieldset key={index} className="space-y-3 border-t border-[var(--line)] pt-4">
            <legend className="font-semibold">{bucket.purpose || 'New service bucket'}</legend>
            <div className="grid gap-3 sm:grid-cols-2">
              <label>
                Bucket ID
                <input
                  className={inputClass}
                  value={bucket.bucket_id}
                  onChange={(event) => updateBucket(index, { bucket_id: event.target.value })}
                />
              </label>
              <label>
                Purpose
                <input
                  className={inputClass}
                  value={bucket.purpose}
                  onChange={(event) => updateBucket(index, { purpose: event.target.value })}
                />
              </label>
              <label>
                Allocation (bps of full miner emissions)
                <input
                  className={inputClass}
                  type="number"
                  min="0"
                  max="1000"
                  value={bucket.allocation_bps}
                  onChange={(event) =>
                    updateBucket(index, {
                      allocation_bps: Number(event.target.value),
                    })
                  }
                />
              </label>
              <label>
                Holding coldkey
                <input
                  className={inputClass}
                  value={bucket.holding_coldkey ?? ''}
                  onChange={(event) =>
                    updateBucket(index, {
                      holding_coldkey: event.target.value || null,
                    })
                  }
                />
              </label>
              <label>
                Private service account reference (optional)
                <input
                  className={inputClass}
                  value={bucket.service_account_ref ?? ''}
                  onChange={(event) =>
                    updateBucket(index, {
                      service_account_ref: event.target.value || null,
                    })
                  }
                />
              </label>
            </div>
            <label className="flex gap-2">
              <input
                type="checkbox"
                checked={bucket.publish_payments}
                onChange={(event) =>
                  updateBucket(index, {
                    publish_payments: event.target.checked,
                  })
                }
              />
              Publish finalized payee payments
            </label>
            {bucket.payee_rules.map((rule, ruleIndex) => (
              <fieldset key={ruleIndex} className="grid gap-3 sm:grid-cols-2">
                <legend>Payee rule {ruleIndex + 1}</legend>
                {(['rule_id', 'label', 'recipient_coldkey', 'recipient_hotkey'] as const).map((field) => (
                  <label key={field}>
                    {
                      {
                        rule_id: 'Rule ID',
                        label: 'Public payment label',
                        recipient_coldkey: 'Payee coldkey',
                        recipient_hotkey: 'Payee hotkey (stake only)',
                      }[field]
                    }
                    <input
                      className={inputClass}
                      value={rule[field] ?? ''}
                      onChange={(event) =>
                        updateBucket(index, {
                          payee_rules: bucket.payee_rules.map((item, i) =>
                            i === ruleIndex
                              ? {
                                  ...item,
                                  [field]:
                                    field === 'recipient_hotkey'
                                      ? event.target.value || null
                                      : event.target.value,
                                }
                              : item,
                          ),
                        })
                      }
                    />
                  </label>
                ))}
                <label>
                  Payment asset
                  <select
                    className={inputClass}
                    value={rule.asset}
                    onChange={(event) =>
                      updateBucket(index, {
                        payee_rules: bucket.payee_rules.map((item, i) =>
                          i === ruleIndex
                            ? {
                                ...item,
                                asset: event.target.value as typeof rule.asset,
                                recipient_hotkey: event.target.value === 'TAO' ? null : item.recipient_hotkey,
                              }
                            : item,
                        ),
                      })
                    }
                  >
                    <option>TAO</option>
                    <option>SN28_ALPHA</option>
                    <option>SN118_ALPHA</option>
                  </select>
                </label>
                <label className="flex gap-2">
                  <input
                    type="checkbox"
                    checked={rule.enabled}
                    onChange={(event) =>
                      updateBucket(index, {
                        payee_rules: bucket.payee_rules.map((item, i) =>
                          i === ruleIndex ? { ...item, enabled: event.target.checked } : item,
                        ),
                      })
                    }
                  />
                  Enabled
                </label>
                <button
                  className={inputClass}
                  onClick={() =>
                    updateBucket(index, {
                      payee_rules: bucket.payee_rules.filter((_, i) => i !== ruleIndex),
                    })
                  }
                >
                  Remove payee rule
                </button>
              </fieldset>
            ))}
            <button
              className={inputClass}
              disabled={bucket.payee_rules.length >= 20}
              onClick={() =>
                updateBucket(index, {
                  payee_rules: [
                    ...bucket.payee_rules,
                    {
                      rule_id: '',
                      label: '',
                      recipient_coldkey: '',
                      asset: 'TAO',
                      recipient_hotkey: null,
                      enabled: true,
                    },
                  ],
                })
              }
            >
              Add payee rule
            </button>
          </fieldset>
        ))}
        <button
          className={inputClass}
          disabled={settings.service_buckets.length >= 20}
          onClick={() => {
            setSettings({
              ...settings,
              service_buckets: [...settings.service_buckets, blankBucket('', '')],
            })
            setConfirmation('')
          }}
        >
          Add service wallet
        </button>
        <p className="text-sm text-[var(--muted)]">
          Rules classify finalized transfers to exact payees. A GM treasury payment is distinct from confirmed
          GM credits. Wallets, allocations, and rule changes appear in public admin activity; service account
          references stay private.
        </p>
        {!parsed.success && <p role="alert">{parsed.error.issues[0]?.message}</p>}
        <label className="block">
          Reason for change
          <textarea
            className={inputClass}
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <label className="block">
          Type RECORD TREASURY SHADOW POLICY
          <input
            className={inputClass}
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
          />
        </label>
        <button
          className={inputClass}
          disabled={
            !parsed.success || reason.trim().length < 8 || confirmation !== 'RECORD TREASURY SHADOW POLICY'
          }
          onClick={submit}
        >
          Record wallet policy
        </button>
      </fieldset>
    </section>
  )
}
