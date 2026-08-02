export type BackroomAccessLevel = 'read' | 'write'

export interface BackroomUser {
  uid: string
  email: string
  name: string
  picture: string
  accessLevel: BackroomAccessLevel
}

export interface BackroomSession {
  version: 2
  uid: string
  email: string
  name: string
  picture: string
  accessLevel: BackroomAccessLevel
  issuedAt: number
  expiresAt: number
}

export interface OAuthAttempt {
  version: 2
  state: string
  verifier: string
  nonce: string
  createdAt: number
  returnTo: string
}
