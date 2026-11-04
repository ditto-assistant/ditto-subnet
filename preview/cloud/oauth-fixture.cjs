// This server is only included in the disposable cloud preview compose stack.
// It creates no real identity and is scoped to PR 3359's fresh preview only.
const http = require('node:http')
const crypto = require('node:crypto')

const EMAIL = 'synthetic-preview-only@omniaura.ai'
const MAX_AGE = 300

function response(req, env, now = Date.now()) {
  const domain = env.PREVIEW_BASE_DOMAIN || ''
  const secret = env.PREVIEW_SESSION_SECRET || ''
  // runtime.sh generates an IPv4 sslip.io domain and a random hex secret.
  if (env.PREVIEW_PR !== '3359' || env.PREVIEW_PROFILE !== 'stack' || !/^\d{1,3}(?:\.\d{1,3}){3}\.sslip\.io$/.test(domain) || !/^[0-9a-f]{64}$/.test(secret)) {
    return { status: 404 }
  }
  const origin = `https://backroom.${domain}`
  if (req.url !== '/__preview/oauth-session' || req.headers.host !== `backroom.${domain}`) {
    return { status: 403 }
  }
  if (req.method === 'GET') {
    return { status: 200, page: '<!doctype html><title>Synthetic preview session</title><form method="post"><button>Start synthetic preview session</button></form>' }
  }
  if (req.method !== 'POST' || req.headers.origin !== origin) return { status: 403 }
  const session = { version: 2, uid: 'synthetic-preview-only', email: EMAIL, name: 'Synthetic preview operator', picture: '', accessLevel: 'write', issuedAt: now, expiresAt: now + MAX_AGE * 1000 }
  const iv = crypto.randomBytes(12)
  const cipher = crypto.createCipheriv('aes-256-gcm', crypto.createHash('sha256').update(secret).digest(), iv)
  const ciphertext = Buffer.concat([cipher.update(JSON.stringify(session)), cipher.final()])
  const token = Buffer.concat([iv, ciphertext, cipher.getAuthTag()]).toString('base64url')
  return { status: 204, cookie: `__Host-backroom_session=${token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${MAX_AGE}` }
}

if (require.main === module) {
  http.createServer((req, res) => {
    const result = response(req, process.env)
    res.setHeader('Cache-Control', 'no-store')
    res.setHeader('Content-Security-Policy', "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
    if (result.cookie) res.setHeader('Set-Cookie', result.cookie)
    if (result.page) res.setHeader('Content-Type', 'text/html; charset=utf-8')
    res.writeHead(result.status).end(result.page)
  }).listen(3001, '127.0.0.1')
}
module.exports = { response, EMAIL, MAX_AGE }
