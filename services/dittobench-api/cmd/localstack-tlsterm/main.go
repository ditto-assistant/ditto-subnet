// Command localstack-tlsterm is a LOCAL-DEV TLS terminator: it serves HTTPS on
// PORT with a dev cert and reverse-proxies every request to a plaintext HTTP
// upstream (the model-relay). It exists only so the dittobench-api broker — which
// requires an https:// platform inference proxy URL and forwards over its
// default-trust HTTP client — can reach the local model-relay over TLS. The
// broker container mounts the same cert as SSL_CERT_FILE so Go (on Linux) trusts
// it. Not a production component.
//
// Env:
//
//	PORT             listen port (default 8443)
//	TLSTERM_CERT     server cert PEM (also the CA the broker trusts)
//	TLSTERM_KEY      server key PEM
//	TLSTERM_UPSTREAM plaintext upstream base URL (default http://localhost:8082)
package main

import (
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
	"time"
)

func envOr(k, d string) string {
	if v := strings.TrimSpace(os.Getenv(k)); v != "" {
		return v
	}
	return d
}

func main() {
	port := envOr("PORT", "8443")
	cert := envOr("TLSTERM_CERT", "")
	key := envOr("TLSTERM_KEY", "")
	upstream := envOr("TLSTERM_UPSTREAM", "http://localhost:8082")
	if cert == "" || key == "" {
		log.Fatal("TLSTERM_CERT and TLSTERM_KEY are required")
	}
	target, err := url.Parse(upstream)
	if err != nil {
		log.Fatalf("bad TLSTERM_UPSTREAM %q: %v", upstream, err)
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.Transport = &http.Transport{ResponseHeaderTimeout: 120 * time.Second}
	// Preserve Host as the upstream expects a plain host header.
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           proxy,
		ReadHeaderTimeout: 30 * time.Second,
	}
	log.Printf("localstack-tlsterm HTTPS :%s -> %s", port, upstream)
	log.Fatal(srv.ListenAndServeTLS(cert, key))
}
