const PLATFORM_ORIGIN = "https://platform-api.heyditto.ai";
const PUBLIC_API_PREFIX = "/api/v1/public/";
const PREVIEW_HEADERS = {
  "content-security-policy": [
    "default-src 'self'",
    "base-uri 'none'",
    "connect-src 'self'",
    "font-src 'self' data:",
    "form-action 'none'",
    "frame-ancestors 'none'",
    "frame-src 'none'",
    "img-src 'self' data:",
    "media-src 'self'",
    "object-src 'none'",
    "script-src 'self' 'unsafe-inline'",
    "sandbox allow-downloads allow-same-origin allow-scripts",
    "style-src 'self' 'unsafe-inline'",
  ].join("; "),
  "permissions-policy": "camera=(), geolocation=(), microphone=(), payment=(), usb=()",
  "referrer-policy": "no-referrer",
  "x-content-type-options": "nosniff",
  "x-frame-options": "DENY",
};

function withPreviewHeaders(response) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(PREVIEW_HEADERS)) headers.set(name, value);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function previewHealth(request) {
  const url = new URL(request.url);
  return Response.json(
    { status: "ok", host: url.hostname, mode: "untrusted-read-only-dashboard-preview" },
    { headers: { "cache-control": "no-store" } },
  );
}

async function proxyPublicRead(request) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("preview API proxy is read-only\n", {
      status: 405,
      headers: { allow: "GET, HEAD", "cache-control": "no-store" },
    });
  }

  const incoming = new URL(request.url);
  if (!incoming.pathname.startsWith(PUBLIC_API_PREFIX)) {
    return new Response("preview exposes only the public API\n", {
      status: 404,
      headers: { "cache-control": "no-store" },
    });
  }

  const upstream = new URL(incoming.pathname + incoming.search, PLATFORM_ORIGIN);
  const headers = new Headers();
  for (const name of ["accept", "accept-language", "if-none-match", "if-modified-since"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  headers.set("x-ditto-preview", "dashboard");

  const response = await fetch(upstream, {
    method: request.method,
    headers,
    redirect: "manual",
  });
  const safeHeaders = new Headers(response.headers);
  safeHeaders.delete("set-cookie");
  safeHeaders.set("x-ditto-preview-api", "production-public-read-only");
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: safeHeaders,
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    let response;
    if (url.pathname === "/__preview/health") response = previewHealth(request);
    else if (url.pathname.startsWith("/api/")) response = await proxyPublicRead(request);
    else response = await env.ASSETS.fetch(request);
    return withPreviewHeaders(response);
  },
};
