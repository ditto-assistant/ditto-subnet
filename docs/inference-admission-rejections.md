# Inference admission rejections

Requests refused before an `inference_requests` row exists are stored in
`inference_admission_rejections`. The row has a stable code and HTTP status,
the grant id when the caller sent one, the request size, and the configured
byte limit for size rejections. It does not store the body, prompt, headers,
bearer, or provider payload.

Codes: `invalid_json`, `invalid_schema`, `request_too_large`, `stale_session`,
`model_not_allowed`, `grant_not_servable`.

Rows older than 14 days are deleted in small batches on the next write.
`inference_admission_rejections_grant_idx` is `(grant_id, created_at)`.

After proxy logs rotate, reconstruct one benchmark grant with:

```sql
SELECT created_at, lane, http_status, admission_code, request_bytes, byte_limit
FROM inference_admission_rejections
WHERE grant_id = '<grant-uuid>'
ORDER BY created_at;
```

Operators can read the same summary at
`GET /api/v1/admin/inference-admission-rejections?grant_id=<grant-uuid>`.
