import { defineConfig } from 'vitest/config'
import viteReact from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [viteReact()],
  test: {
    environment: 'node',
    include: ['src/**/*.test.{ts,tsx}'],
    // The jsdom panel tests each spin up their own DOM environment and now share
    // the runner with the MCP suites, so under full-suite parallelism they get
    // starved past the 5s default and fail as timeouts rather than on any
    // assertion. They finish in well under a second of actual work; this is
    // headroom for scheduling, not permission for a slow test.
    testTimeout: 20_000,
  },
})
