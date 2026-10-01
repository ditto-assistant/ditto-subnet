import '@tanstack/react-start/server-only'
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import type { McpGrantProps } from './mcp.server'
import { fetchTreasuryObserverSettings, recordTreasuryReceipt } from './admin.service'
import { treasuryReceiptInputSchema } from '../lib/treasury-receipts.schemas'
import { observerGrant } from './treasury-observer-access.server'

export function createTreasuryObserverServer(props: McpGrantProps) {
  if (!observerGrant(props.scopes) || props.session.accessLevel !== 'write' || props.session.expiresAt <= Date.now()) {
    throw new Error('Dedicated observer access requires a live write-level staff session')
  }
  const server = new McpServer({ name: 'SN118 treasury observer', version: '1.0.0' }, {
    capabilities: { tools: {} },
    instructions: 'Only exact historical treasury settings and verified receipt ingestion. No general read/write, source, spending, signing or provider-credit authority.',
  })
  const result = (value: Record<string, unknown>) => ({
    content: [{ type: 'text' as const, text: JSON.stringify(value) }], structuredContent: value,
  })
  server.registerTool('get_treasury_settings', {
    description: 'Read exactly one immutable historical treasury revision and checksum; no general settings or audit history.',
    inputSchema: { revision: z.number().int().min(1).max(2_147_483_647) },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }, async ({ revision }) => result(await fetchTreasuryObserverSettings(revision)))
  server.registerTool('record_treasury_receipt', {
    description: 'Verify one finalized historical payment selection; INGEST VERIFIED TREASURY RECEIPT confirmation. No transfer or provider credits.',
    inputSchema: treasuryReceiptInputSchema.extend({
      stage: z.enum(['service_distribution', 'vendor_payment']),
    }),
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }, async (input) => result(await recordTreasuryReceipt(input, props.session.email)))
  return server
}
