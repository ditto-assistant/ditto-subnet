import { createFileRoute, redirect } from '@tanstack/react-router'
import { getCurrentUser } from '../server/auth.functions'

export const Route = createFileRoute('/')({
  beforeLoad: async () => {
    const user = await getCurrentUser()
    throw redirect({ to: user ? '/screener-capacity' : '/login' })
  },
})
