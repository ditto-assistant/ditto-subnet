import '@tanstack/react-start/server-only'

function bytesToBase64Url(bytes: Uint8Array) {
  let binary = ''
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_')
}

function base64UrlToBytes(value: string) {
  const padded = value.replace(/-/g, '+').replace(/_/g, '/')
  const binary = atob(padded + '='.repeat((4 - (padded.length % 4)) % 4))
  return Uint8Array.from(binary, (character) => character.charCodeAt(0))
}

async function encryptionKey(secret = process.env.SESSION_SECRET) {
  if (!secret || secret.length < 32) {
    throw new Error('SESSION_SECRET must contain at least 32 characters')
  }
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret))
  return crypto.subtle.importKey('raw', digest, 'AES-GCM', false, ['encrypt', 'decrypt'])
}

export async function sealToken(value: unknown, secret?: string) {
  const iv = crypto.getRandomValues(new Uint8Array(12))
  const plaintext = new TextEncoder().encode(JSON.stringify(value))
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv },
    await encryptionKey(secret),
    plaintext,
  )
  const output = new Uint8Array(iv.length + ciphertext.byteLength)
  output.set(iv)
  output.set(new Uint8Array(ciphertext), iv.length)
  return bytesToBase64Url(output)
}

export async function unsealToken<T>(token: string, secret?: string) {
  try {
    const input = base64UrlToBytes(token)
    if (input.length < 29) return null
    const plaintext = await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: input.slice(0, 12) },
      await encryptionKey(secret),
      input.slice(12),
    )
    return JSON.parse(new TextDecoder().decode(plaintext)) as T
  } catch {
    return null
  }
}

export function randomToken(size = 32) {
  return bytesToBase64Url(crypto.getRandomValues(new Uint8Array(size)))
}

export function constantTimeEqual(left: string, right: string) {
  if (left.length !== right.length) return false
  let difference = 0
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index)
  }
  return difference === 0
}
