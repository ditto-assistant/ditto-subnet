const { test } = require('node:test')
const assert = require('node:assert/strict')
const crypto = require('node:crypto')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const http = require('node:http')
const { once } = require('node:events')
const { response, startServer, EMAIL, MAX_AGE } = require('./oauth-fixture.cjs')

const env = { PREVIEW_PR: '3359', PREVIEW_PROFILE: 'stack', PREVIEW_BASE_DOMAIN: '192.0.2.1.sslip.io', PREVIEW_SESSION_SECRET: 'a'.repeat(64) }
const req = { method: 'POST', url: '/__preview/oauth-session', headers: { origin: 'https://backroom.192.0.2.1.sslip.io', host: 'backroom.192.0.2.1.sslip.io' } }

test('only the isolated fresh stack can mint a same-origin short session', () => {
  for (const bad of [{ ...env, PREVIEW_PR: '3358' }, { ...env, PREVIEW_PR: '' }, { ...env, PREVIEW_PROFILE: 'stack-copy' }, { ...env, PREVIEW_PROFILE: '' }, { ...env, PREVIEW_SESSION_SECRET: 'weak' }, { ...env, PREVIEW_BASE_DOMAIN: 'example.com' }]) {
    assert.equal(response(req, bad).status, 404)
  }
  for (const bad of [{ ...req, method: 'PUT' }, { ...req, url: '/' }, { ...req, headers: { ...req.headers, origin: 'https://attacker.invalid' } }, { ...req, headers: { ...req.headers, origin: undefined } }, { ...req, headers: { ...req.headers, host: 'attacker.invalid' } }]) {
    assert.equal(response(bad, env).status, 403)
  }
  const page = response({ ...req, method: 'GET' }, env)
  assert.equal(page.status, 200)
  assert.match(page.page, /<form method="post">/)
  assert.equal(page.cookie, undefined)
  const result = response(req, env, 1000)
  assert.equal(result.status, 204)
  assert.match(result.cookie, /; HttpOnly; Secure; SameSite=Lax; Path=\/; Max-Age=300$/)
  const token = Buffer.from(result.cookie.split(';')[0].split('=')[1], 'base64url')
  const decipher = crypto.createDecipheriv('aes-256-gcm', crypto.createHash('sha256').update(env.PREVIEW_SESSION_SECRET).digest(), token.subarray(0, 12))
  decipher.setAuthTag(token.subarray(-16))
  const session = JSON.parse(Buffer.concat([decipher.update(token.subarray(12, -16)), decipher.final()]))
  assert.equal(session.email, EMAIL)
  assert.equal(session.accessLevel, 'write')
  assert.equal(session.expiresAt, 1000 + MAX_AGE * 1000)
})

test('serves through a local socket and recovers a stale socket on restart', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'preview-oauth-'))
  const socket = path.join(dir, 'fixture.sock')
  try {
    for (let n = 0; n < 2; n++) {
      const server = startServer(socket, env)
      await once(server, 'listening')
      const status = await new Promise((resolve, reject) => {
        const request = http.request({ socketPath: socket, path: req.url, method: 'POST', headers: req.headers }, (res) => {
          res.resume()
          res.on('end', () => resolve(res.statusCode))
        })
        request.on('error', reject)
        request.end()
      })
      assert.equal(status, 204)
      await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()))
      if (n === 0) fs.writeFileSync(socket, 'stale')
    }
  } finally {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})
