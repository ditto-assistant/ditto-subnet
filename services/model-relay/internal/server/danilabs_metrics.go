package server

import (
	"net"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"

	"github.com/ditto-assistant/model-relay/internal/config"
)

// The relay may bind on all interfaces. Only an on-host caller receives
// provider binding metadata, even if a network firewall is misconfigured.
func (s *Server) metricsHandler() http.Handler {
	standard := promhttp.HandlerFor(prometheus.DefaultGatherer, promhttp.HandlerOpts{})
	local := promhttp.HandlerFor(
		prometheus.Gatherers{prometheus.DefaultGatherer, daniLabsBindingMetrics(s.cfg.Inference, s.cfg.DittoRouter)},
		promhttp.HandlerOpts{},
	)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		host, _, err := net.SplitHostPort(r.RemoteAddr)
		if err == nil {
			if ip := net.ParseIP(host); ip != nil && ip.IsLoopback() {
				local.ServeHTTP(w, r)
				return
			}
		}
		standard.ServeHTTP(w, r)
	})
}

// daniLabsBindingMetrics describes process-loaded configuration, never the key
// itself or provider health. The registry is per server, so tests and multiple
// server instances do not share mutable global configuration.
func daniLabsBindingMetrics(cfg config.InferenceProxyConfig, router config.DittoRouterConfig) prometheus.Gatherer {
	registry := prometheus.NewRegistry()
	keyPresent := 0.0
	if cfg.DaniLabsAPIKey != "" {
		keyPresent = 1
	}
	scope := 0.0 // missing or invalid
	switch cfg.DaniLabsTrialScope {
	case "canary":
		scope = 1
	case "fleet":
		scope = 2
	}
	deadline := 0.0 // missing or invalid
	if until, err := time.Parse(time.RFC3339, cfg.DaniLabsTrialUntil); err == nil && until.Unix() > 0 {
		deadline = float64(until.Unix())
	}
	routerCompetition := 0.0
	if router.RoutesLane(config.DittoRouterLaneCompetition) {
		routerCompetition = 1
	}
	for _, metric := range []struct {
		name, help string
		value      float64
	}{
		{"ditto_danilabs_binding_key_present", "Whether the process loaded a DaniLabs key; not an authentication check.", keyPresent},
		{"ditto_danilabs_binding_scope", "Loaded DaniLabs scope: 0 invalid or unset, 1 canary, 2 fleet.", scope},
		{"ditto_danilabs_binding_deadline_seconds", "Loaded DaniLabs deadline as Unix seconds; 0 means invalid or unset.", deadline},
		{"ditto_danilabs_binding_router_competition", "Whether the loaded Router competition lane takes precedence over DaniLabs.", routerCompetition},
	} {
		m := prometheus.NewGauge(prometheus.GaugeOpts{Name: metric.name, Help: metric.help})
		m.Set(metric.value)
		registry.MustRegister(m)
	}
	return registry
}
