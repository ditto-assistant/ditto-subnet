/**
 * Test-only stand-in for the Workers-runtime `cloudflare:workers` module, which
 * node's loader cannot resolve. Only `WorkerEntrypoint`'s ctx/env plumbing is
 * needed: the MCP handler and the OAuth provider library use nothing else from
 * it. `vitest.config.ts` aliases the module here.
 */
export class WorkerEntrypoint<Env = unknown, Props = unknown> {
  constructor(
    public ctx: ExecutionContext & { props: Props },
    public env: Env,
  ) {}
}
