export default {
  async fetch() {
    return new Response("This dashboard preview has been retired.\n", {
      status: 410,
      headers: {
        "cache-control": "no-store",
        "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
        "content-type": "text/plain; charset=utf-8",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
      },
    });
  },
};
