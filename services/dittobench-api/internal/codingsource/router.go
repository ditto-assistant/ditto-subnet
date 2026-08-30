package codingsource

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codinggateway"
)

const (
	workspacePrefix  = "/v1/coding/workspace/"
	inferencePrefix  = "/v1/coding/inference/"
	defaultMaxRoutes = 64
)

var (
	ErrRouterConfig = errors.New("coding source router configuration is invalid")
	ErrRoute        = errors.New("coding source route publication failed")
	ErrRouteRevoked = errors.New("coding source route is revoked")
)

type RouterConfig struct {
	Listener      net.Listener
	PublicBaseURL string
	Registry      *Registry
	NewToken      func() string
	MaxRoutes     int
}

// Router owns one host-gateway listener and only admits requests whose direct
// socket source still matches the active harness registration.
type Router struct {
	mu sync.Mutex

	registry *Registry
	baseURL  string
	host     string
	newToken func() string
	maximum  int
	routes   map[string]*route
	server   *http.Server
	done     chan struct{}
	serveErr error
	closed   bool
}

type route struct {
	mu sync.Mutex

	owner    *Router
	token    string
	prefix   string
	url      string
	source   *sourceRecord
	handler  http.Handler
	inflight int
	drained  chan struct{}
	revoked  bool
	closed   bool
}

type WorkspacePublisher struct{ router *Router }
type InferencePublisher struct{ router *Router }

func NewRouter(config RouterConfig) (*Router, error) {
	if config.Listener == nil || nilLike(config.Registry) {
		return nil, ErrRouterConfig
	}
	parsed, err := url.ParseRequestURI(config.PublicBaseURL)
	if err != nil || parsed.Scheme != "http" || parsed.Hostname() != "host.docker.internal" ||
		parsed.Port() == "" || parsed.User != nil || (parsed.Path != "" && parsed.Path != "/") ||
		parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, ErrRouterConfig
	}
	listenerHost, listenerPort, err := net.SplitHostPort(config.Listener.Addr().String())
	listenerIP := net.ParseIP(listenerHost)
	if err != nil || listenerIP == nil || listenerIP.IsLoopback() || listenerPort != parsed.Port() {
		return nil, ErrRouterConfig
	}
	maximum := config.MaxRoutes
	if maximum == 0 {
		maximum = defaultMaxRoutes
	}
	if maximum < 1 || maximum > 4096 {
		return nil, ErrRouterConfig
	}
	newToken := config.NewToken
	if newToken == nil {
		newToken = randomRouteToken
	}
	router := &Router{
		registry: config.Registry, baseURL: strings.TrimRight(config.PublicBaseURL, "/"),
		host: parsed.Host, newToken: newToken, maximum: maximum, routes: make(map[string]*route), done: make(chan struct{}),
	}
	router.server = &http.Server{
		Handler:           router,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       5 * time.Minute,
		WriteTimeout:      5 * time.Minute,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    32 << 10,
	}
	go func() {
		err := router.server.Serve(config.Listener)
		router.mu.Lock()
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			router.serveErr = ErrRoute
		}
		close(router.done)
		router.mu.Unlock()
	}()
	return router, nil
}

func (router *Router) WorkspacePublisher() WorkspacePublisher {
	return WorkspacePublisher{router: router}
}
func (router *Router) InferencePublisher() InferencePublisher {
	return InferencePublisher{router: router}
}

func (publisher WorkspacePublisher) Publish(
	ctx context.Context,
	binding codingcertifier.CapabilityBinding,
	handler http.Handler,
) (codingcertifier.PublishedCapability, error) {
	if publisher.router == nil || ctx == nil || ctx.Err() != nil || nilLike(handler) {
		return nil, ErrRoute
	}
	source, ok := publisher.router.registry.resolve(
		binding.HarnessInstanceID, binding.AgentArtifactSHA256, binding.TicketID,
		binding.CaseID, binding.ProfileCapabilityID, nil,
	)
	if !ok {
		return nil, ErrRoute
	}
	return publisher.router.publish(workspacePrefix, "/tool", source, handler)
}

func (publisher InferencePublisher) Publish(
	ctx context.Context,
	binding codinggateway.CapabilityBinding,
	handler http.Handler,
) (codinggateway.PublishedCapability, error) {
	if publisher.router == nil || ctx == nil || ctx.Err() != nil || nilLike(handler) {
		return nil, ErrRoute
	}
	source, ok := publisher.router.registry.resolve(
		binding.HarnessInstanceID, binding.AgentArtifactSHA256, binding.TicketID,
		binding.CaseID, binding.ProfileCapabilityID, &binding.Deadline,
	)
	if !ok {
		return nil, ErrRoute
	}
	return publisher.router.publish(inferencePrefix, "", source, handler)
}

func (router *Router) publish(kindPrefix, suffix string, source *sourceRecord, handler http.Handler) (*route, error) {
	for attempt := 0; attempt < 8; attempt++ {
		token := router.newToken()
		if !validToken(token) {
			return nil, ErrRoute
		}
		key := kindPrefix + token
		router.mu.Lock()
		if router.closed || router.serveErr != nil || len(router.routes) >= router.maximum {
			router.mu.Unlock()
			return nil, ErrRoute
		}
		if router.routes[key] != nil {
			router.mu.Unlock()
			continue
		}
		published := &route{
			owner: router, token: key, prefix: key, url: router.baseURL + key + suffix,
			source: source, handler: handler,
		}
		router.routes[key] = published
		router.mu.Unlock()
		return published, nil
	}
	return nil, ErrRoute
}

func (router *Router) ServeHTTP(response http.ResponseWriter, request *http.Request) {
	response.Header().Set("Cache-Control", "no-store")
	response.Header().Set("X-Content-Type-Options", "nosniff")
	if router == nil || request == nil || request.URL.RawPath != "" || request.URL.RawQuery != "" ||
		!strings.EqualFold(request.Host, router.host) {
		http.NotFound(response, request)
		return
	}
	key := routeKey(request.URL.Path)
	router.mu.Lock()
	published := router.routes[key]
	router.mu.Unlock()
	if published == nil {
		http.NotFound(response, request)
		return
	}
	address, err := remoteAddress(request.RemoteAddr)
	if err != nil || !router.registry.matches(published.source, address) || !published.admit() {
		http.NotFound(response, request)
		return
	}
	defer published.release()
	http.StripPrefix(published.prefix, published.handler).ServeHTTP(response, request)
}

func routeKey(path string) string {
	for _, prefix := range []string{workspacePrefix, inferencePrefix} {
		if !strings.HasPrefix(path, prefix) {
			continue
		}
		remainder := strings.TrimPrefix(path, prefix)
		token, _, _ := strings.Cut(remainder, "/")
		if validToken(token) {
			return prefix + token
		}
	}
	return ""
}

func remoteAddress(value string) (netip.Addr, error) {
	host, _, err := net.SplitHostPort(value)
	if err != nil {
		return netip.Addr{}, err
	}
	address, err := netip.ParseAddr(host)
	if err != nil {
		return netip.Addr{}, err
	}
	return address.Unmap(), nil
}

func (published *route) admit() bool {
	published.mu.Lock()
	defer published.mu.Unlock()
	if published.revoked || published.closed {
		return false
	}
	published.inflight++
	return true
}

func (published *route) release() {
	published.mu.Lock()
	defer published.mu.Unlock()
	published.inflight--
	if published.inflight == 0 && published.drained != nil {
		close(published.drained)
		published.drained = nil
	}
}

func (published *route) URL() string {
	if published == nil {
		return ""
	}
	published.mu.Lock()
	defer published.mu.Unlock()
	if published.revoked || published.closed {
		return ""
	}
	return published.url
}

func (published *route) Revoke(ctx context.Context) error {
	if published == nil || ctx == nil {
		return ErrRoute
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	published.owner.mu.Lock()
	if published.owner.routes[published.token] == published {
		delete(published.owner.routes, published.token)
	}
	published.owner.mu.Unlock()
	published.mu.Lock()
	published.revoked = true
	if published.inflight == 0 {
		published.mu.Unlock()
		return nil
	}
	if published.drained == nil {
		published.drained = make(chan struct{})
	}
	drained := published.drained
	published.mu.Unlock()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-drained:
		return nil
	}
}

func (published *route) Close() error {
	if published == nil {
		return nil
	}
	published.mu.Lock()
	defer published.mu.Unlock()
	if published.closed {
		return nil
	}
	if !published.revoked || published.inflight != 0 {
		return ErrRouteRevoked
	}
	published.closed = true
	published.handler = nil
	published.source = nil
	published.url = ""
	return nil
}

func (router *Router) Close(ctx context.Context) error {
	if router == nil || ctx == nil {
		return ErrRouterConfig
	}
	router.mu.Lock()
	if router.closed {
		done, err := router.done, router.serveErr
		router.mu.Unlock()
		select {
		case <-done:
			return err
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	if len(router.routes) != 0 {
		router.mu.Unlock()
		return ErrRouteRevoked
	}
	router.closed = true
	router.mu.Unlock()
	if err := router.server.Shutdown(ctx); err != nil {
		return err
	}
	select {
	case <-router.done:
		router.mu.Lock()
		err := router.serveErr
		router.mu.Unlock()
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func randomRouteToken() string {
	var body [32]byte
	if _, err := rand.Read(body[:]); err != nil {
		return ""
	}
	return base64.RawURLEncoding.EncodeToString(body[:])
}

func validToken(value string) bool {
	if len(value) < 32 || len(value) > 128 {
		return false
	}
	for _, character := range value {
		if (character < 'a' || character > 'z') && (character < 'A' || character > 'Z') &&
			(character < '0' || character > '9') && character != '_' && character != '-' {
			return false
		}
	}
	return true
}

func (*Router) MarshalJSON() ([]byte, error)            { return nil, ErrRouterConfig }
func (WorkspacePublisher) MarshalJSON() ([]byte, error) { return nil, ErrRouterConfig }
func (InferencePublisher) MarshalJSON() ([]byte, error) { return nil, ErrRouterConfig }
func (*route) MarshalJSON() ([]byte, error)             { return nil, ErrRouterConfig }

func (router *Router) String() string         { return "CodingSourceRouter{private}" }
func (router *Router) GoString() string       { return router.String() }
func (router *Router) LogValue() slog.Value   { return slog.StringValue("coding-source-router") }
func (published *route) String() string       { return "CodingSourceRoute{private}" }
func (published *route) GoString() string     { return published.String() }
func (published *route) LogValue() slog.Value { return slog.StringValue("coding-source-route") }

var _ http.Handler = (*Router)(nil)
var _ codingcertifier.CapabilityPublisher = WorkspacePublisher{}
var _ codinggateway.CapabilityPublisher = InferencePublisher{}
var _ codingcertifier.PublishedCapability = (*route)(nil)
var _ codinggateway.PublishedCapability = (*route)(nil)
var _ json.Marshaler = (*Router)(nil)
var _ json.Marshaler = WorkspacePublisher{}
var _ json.Marshaler = InferencePublisher{}
var _ json.Marshaler = (*route)(nil)
