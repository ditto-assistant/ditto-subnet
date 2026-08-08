import { createFileRoute } from '@tanstack/react-router'
import {
  ArrowRight,
  Bot,
  Check,
  CheckCircle2,
  Clipboard,
  Download,
  Eye,
  LockKeyhole,
  Settings2,
  ShieldCheck,
  Terminal,
} from 'lucide-react'
import { useState } from 'react'
import { PageHeader } from '../../components/PageHeader'

const MCP_URL = 'https://backroom.dittobench.ai/mcp'

const clients = [
  {
    name: 'Codex CLI',
    command: `codex mcp add sn118-backroom --url ${MCP_URL}`,
    steps: [
      'Run the command once in your terminal.',
      'Complete the Backroom browser sign-in and consent screen.',
      'Confirm the server with codex mcp list.',
    ],
  },
  {
    name: 'Claude Code',
    command: `claude mcp add --transport http sn118-backroom ${MCP_URL}`,
    steps: [
      'Run the command in the project where you use Claude Code.',
      'Open Claude Code and run /mcp.',
      'Choose Backroom and complete OAuth in the browser.',
    ],
  },
  {
    name: 'Cursor',
    command: MCP_URL,
    steps: [
      'Open Cursor Settings, then Tools & MCP.',
      'Add a remote Streamable HTTP server named Backroom.',
      'Paste the URL and complete the OAuth prompt.',
    ],
  },
]

export const Route = createFileRoute('/_authenticated/agent-access')({
  component: AgentAccessPage,
})

function AgentAccessPage() {
  const [selectedClient, setSelectedClient] = useState(0)
  const [copied, setCopied] = useState('')
  const client = clients[selectedClient]

  const copy = async (label: string, value: string) => {
    await navigator.clipboard.writeText(value)
    setCopied(label)
    window.setTimeout(() => setCopied(''), 1_500)
  }

  return (
    <div>
      <PageHeader
        label="Agent control"
        title="Connect agents to Backroom"
        description="Give Codex, Claude, Cursor, and other MCP clients scoped access to SN118 production operations without sharing an API key. The connection acts as your own operator account, so every change is attributed to you."
        aside={
          <div className="flex items-center gap-2 rounded-full border border-[var(--line)] bg-[var(--panel)] px-3 py-2 text-xs text-[var(--muted-strong)]">
            <ShieldCheck className="h-3.5 w-3.5 text-[var(--acid)]" />
            OAuth protected
          </div>
        }
      />

      <section className="mt-6 overflow-hidden rounded-xl border border-[var(--line)] bg-[var(--panel)]">
        <div className="grid gap-6 p-5 sm:p-6 lg:grid-cols-[1fr_auto] lg:items-center">
          <div>
            <div className="flex items-center gap-2 text-xs font-medium text-[var(--muted)]">
              <span className="pulse-dot h-2 w-2 rounded-full bg-[var(--acid)]" />
              Streamable HTTP endpoint
            </div>
            <code className="mt-3 block break-all text-sm font-medium text-white sm:text-base">
              {MCP_URL}
            </code>
            <p className="mt-2 text-xs leading-5 text-[var(--muted)]">
              Authentication opens in your browser. Only active OmniAura team members can authorize
              a connection.
            </p>
          </div>
          <button
            type="button"
            onClick={() => copy('url', MCP_URL)}
            className="inline-flex items-center justify-center gap-2 rounded-lg border border-[var(--line-strong)] bg-[var(--panel-raised)] px-4 py-3 text-sm font-medium transition-colors hover:bg-white/[0.07]"
          >
            {copied === 'url' ? (
              <Check className="h-4 w-4 text-[var(--acid)]" />
            ) : (
              <Clipboard className="h-4 w-4 text-[var(--muted)]" />
            )}
            {copied === 'url' ? 'Copied' : 'Copy URL'}
          </button>
        </div>
      </section>

      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,1.2fr)_minmax(22rem,0.8fr)]">
        <section className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-5 sm:p-6">
          <div className="flex items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-[var(--cyan-dim)] text-[var(--cyan)]">
              <Terminal className="h-4 w-4" />
            </div>
            <div>
              <h3 className="text-sm font-semibold">Connection instructions</h3>
              <p className="mt-0.5 text-xs text-[var(--muted)]">
                Choose the client you are configuring.
              </p>
            </div>
          </div>

          <div
            className="mt-5 flex gap-1 overflow-x-auto rounded-lg bg-[var(--panel-soft)] p-1"
            role="tablist"
            aria-label="MCP clients"
          >
            {clients.map((item, index) => (
              <button
                key={item.name}
                type="button"
                role="tab"
                aria-selected={selectedClient === index}
                onClick={() => setSelectedClient(index)}
                className={`whitespace-nowrap rounded-md px-3 py-2 text-xs font-medium transition-colors ${
                  selectedClient === index
                    ? 'bg-[var(--panel-raised)] text-white'
                    : 'text-[var(--muted)] hover:text-white'
                }`}
              >
                {item.name}
              </button>
            ))}
          </div>

          <div className="mt-4 overflow-hidden rounded-lg border border-[var(--line)] bg-[#090b0a]">
            <div className="flex items-center justify-between border-b border-[var(--line)] px-3 py-2">
              <span className="text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--muted)]">
                {client.name === 'Cursor' ? 'Server URL' : 'Terminal'}
              </span>
              <button
                type="button"
                onClick={() => copy('command', client.command)}
                className="inline-flex items-center gap-1.5 rounded px-2 py-1 text-[11px] text-[var(--muted)] hover:bg-white/5 hover:text-white"
              >
                {copied === 'command' ? (
                  <Check className="h-3.5 w-3.5 text-[var(--acid)]" />
                ) : (
                  <Clipboard className="h-3.5 w-3.5" />
                )}
                {copied === 'command' ? 'Copied' : 'Copy'}
              </button>
            </div>
            <pre className="scrollbar-thin overflow-x-auto p-4 text-xs leading-6 text-[var(--acid)]">
              <code>{client.command}</code>
            </pre>
          </div>

          <ol className="mt-5 space-y-3">
            {client.steps.map((step, index) => (
              <li key={step} className="flex items-start gap-3">
                <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full border border-[var(--line-strong)] bg-[var(--panel-raised)] text-[10px] font-semibold text-[var(--muted-strong)]">
                  {index + 1}
                </span>
                <span className="pt-0.5 text-sm leading-5 text-[var(--muted-strong)]">{step}</span>
              </li>
            ))}
          </ol>
        </section>

        <section className="rounded-xl border border-[var(--line)] bg-[var(--panel)] p-5 sm:p-6">
          <div className="flex items-center gap-3">
            <div className="grid h-9 w-9 place-items-center rounded-lg bg-[var(--acid-dim)] text-[var(--acid)]">
              <LockKeyhole className="h-4 w-4" />
            </div>
            <div>
              <h3 className="text-sm font-semibold">Permission levels</h3>
              <p className="mt-0.5 text-xs text-[var(--muted)]">Least privilege is the default.</p>
            </div>
          </div>

          <div className="mt-5 space-y-3">
            <PermissionSummary
              icon={Eye}
              title="Read only"
              scope="backroom:read"
              description="Inspect subnet policy, screening history, scores, and the emission split. New connections start here."
            />
            <PermissionSummary
              icon={Download}
              title="Source artifacts"
              scope="backroom:artifact:read"
              description="Read miner-submitted source: audited downloads, file listings, and copy or baseline diffs. No permission to change a verdict or policy."
            />
            <PermissionSummary
              icon={Settings2}
              title="Read & write"
              scope="backroom:write"
              description="Change subnet policy, set the emission burn, and resolve quarantine or dispute reviews. Does not grant source-download access."
              warning
            />
          </div>

          <div className="mt-4 rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4">
            <p className="flex items-center gap-2 text-xs font-medium text-white">
              <Bot className="h-3.5 w-3.5 text-[var(--cyan)]" />
              What the agent sees
            </p>
            <ul className="mt-3 space-y-2 text-xs leading-5 text-[var(--muted)]">
              <li className="flex gap-2">
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--acid)]" />
                Every tool is labeled read-only or destructive in MCP metadata.
              </li>
              <li className="flex gap-2">
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--acid)]" />A
                protected attempt from a read token triggers OAuth re-consent for only the required
                scope.
              </li>
              <li className="flex gap-2">
                <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--acid)]" />
                Platform still enforces the admin token and records your email as the actor on
                every production call.
              </li>
            </ul>
          </div>
        </section>
      </div>

      <section className="mt-6 rounded-xl border border-[var(--line)] bg-[var(--panel)] p-5 sm:p-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <h3 className="text-sm font-semibold">Available operations</h3>
            <p className="mt-1 text-xs leading-5 text-[var(--muted)]">
              The MCP surface exposes the supported subnet operations. Benchmark rollout start and supersede remain UI-only. Ditto app controls are not served here at all — they stay in the private product Backroom.
            </p>
          </div>
          <span className="inline-flex w-fit items-center gap-2 rounded-full border border-[var(--line)] px-3 py-1.5 text-[11px] text-[var(--muted-strong)]">
            66 tools <ArrowRight className="h-3.5 w-3.5" /> 3 permission scopes
          </span>
        </div>
        <div className="mt-5 grid gap-3 md:grid-cols-2">
          <ToolGroup
            title="Emissions & scoring policy"
            tools={[
              'Read and set the miner emission burn',
              'Read and set efficiency bonus policy',
              'Read and set continual retest policy',
              'Read leaderboards and score history',
            ]}
          />
          <ToolGroup
            title="Validator fleet"
            tools={[
              'Read and set queue and slot policy',
              'Retry, evict, or reinstate validator work',
              'Read stuck submissions and lease revocations',
              'Inspect the active identity and scopes',
            ]}
          />
          <ToolGroup
            title="Screening quarantine"
            tools={[
              'List active or resolved quarantines',
              'Fetch the active queue and batch review context',
              'Preview guarded per-item batch decisions',
              'Inspect screening attempt history',
              'Execute audited release, rescreen, or reject batches',
              'List one-time miner disputes',
              'Release or uphold a dispute',
              'Issue an audited artifact download',
            ]}
          />
        </div>
      </section>
    </div>
  )
}

function PermissionSummary({
  icon: Icon,
  title,
  scope,
  description,
  warning = false,
}: {
  icon: typeof Eye
  title: string
  scope: string
  description: string
  warning?: boolean
}) {
  return (
    <div
      className={`rounded-lg border p-4 ${
        warning
          ? 'border-[var(--amber)]/25 bg-[var(--amber-dim)]'
          : 'border-[var(--acid)]/20 bg-[var(--acid-dim)]'
      }`}
    >
      <div className="flex items-center gap-2.5">
        <Icon className={`h-4 w-4 ${warning ? 'text-[var(--amber)]' : 'text-[var(--acid)]'}`} />
        <span className="text-sm font-medium">{title}</span>
        <code className="ml-auto rounded bg-black/20 px-2 py-1 text-[10px] text-[var(--muted-strong)]">
          {scope}
        </code>
      </div>
      <p className="mt-2 text-xs leading-5 text-[var(--muted)]">{description}</p>
    </div>
  )
}

function ToolGroup({ title, tools }: { title: string; tools: Array<string> }) {
  return (
    <div className="rounded-lg border border-[var(--line)] bg-[var(--panel-soft)] p-4">
      <h4 className="text-xs font-semibold uppercase tracking-[0.08em] text-[var(--muted-strong)]">
        {title}
      </h4>
      <ul className="mt-3 grid gap-2 sm:grid-cols-2">
        {tools.map((tool) => (
          <li key={tool} className="flex items-start gap-2 text-xs leading-5 text-[var(--muted)]">
            <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-[var(--acid)]" />
            {tool}
          </li>
        ))}
      </ul>
    </div>
  )
}
