// Package codingtransport terminates the dedicated validator mTLS connection
// and forwards signed requests to the scorer's private Unix ingress.
package codingtransport

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontrol"
)

const (
	ControlSocketPath    = "/run/ditto-coding-scorer/control.sock"
	ReadinessPath        = "/v1/coding/ready"
	validatorURIPrefix   = "spiffe://dittobench.ai/validator/"
	maximumRequestBytes  = 8 << 20
	maximumResponseBytes = 32 << 20
)

var (
	ErrInvalidConfig = errors.New("coding executor mTLS transport configuration is invalid")
	ErrPrivate       = errors.New("coding executor mTLS transport is private")
)

type Config struct {
	ValidatorHotkey string
	UnixSocketPath  string
	RoundTripper    http.RoundTripper
}

type Proxy struct {
	hotkey string
	client *http.Client
}

func New(config Config) (*Proxy, error) {
	if !codingcontrol.ValidValidatorHotkey(config.ValidatorHotkey) ||
		config.UnixSocketPath != ControlSocketPath {
		return nil, ErrInvalidConfig
	}
	transport := config.RoundTripper
	if transport == nil {
		transport = &http.Transport{
			Proxy: nil, DisableCompression: true, ForceAttemptHTTP2: false,
			MaxIdleConns: 2, MaxIdleConnsPerHost: 1, MaxConnsPerHost: 2,
			IdleConnTimeout: 30 * time.Second,
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{Timeout: 5 * time.Second}).DialContext(
					ctx, "unix", ControlSocketPath,
				)
			},
		}
	}
	return &Proxy{
		hotkey: config.ValidatorHotkey,
		client: &http.Client{
			Transport: transport,
			Timeout:   33 * time.Minute,
			CheckRedirect: func(*http.Request, []*http.Request) error {
				return errors.New("coding executor redirect rejected")
			},
		},
	}, nil
}

func (config Config) String() string               { return "CodingExecutorMTLSTransportConfig{private}" }
func (config Config) GoString() string             { return config.String() }
func (config Config) MarshalJSON() ([]byte, error) { return nil, ErrPrivate }

func (proxy *Proxy) Handler() http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		setPrivateHeaders(response)
		if proxy == nil {
			writeError(response, http.StatusServiceUnavailable)
			return
		}
		readiness := request.Method == http.MethodGet &&
			request.URL.Path == ReadinessPath && request.URL.RawQuery == ""
		if _, ok := codingcontrol.OperationForRequest(
			request.Method, request.URL.Path, request.URL.RawQuery,
		); !ok && !readiness {
			writeError(response, http.StatusNotFound)
			return
		}
		certificateHotkey, certificateOK := validatorCertificateHotkey(request.TLS)
		if !certificateOK || certificateHotkey != proxy.hotkey {
			writeError(response, http.StatusUnauthorized)
			return
		}
		if readiness {
			if len(request.Header.Values(codingcontrol.EnvelopeHeader)) != 0 ||
				request.Header.Get("Authorization") != "" || request.Header.Get("Cookie") != "" ||
				request.ContentLength != 0 || request.Header.Get("Content-Encoding") != "" {
				writeError(response, http.StatusBadRequest)
				return
			}
		} else {
			hotkey, envelopeOK := codingcontrol.EnvelopeValidatorHotkey(
				request.Header.Values(codingcontrol.EnvelopeHeader),
			)
			if !envelopeOK || hotkey != proxy.hotkey {
				writeError(response, http.StatusUnauthorized)
				return
			}
		}
		if request.ContentLength > maximumRequestBytes {
			writeError(response, http.StatusRequestEntityTooLarge)
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(response, request.Body, maximumRequestBytes))
		if err != nil {
			writeError(response, http.StatusRequestEntityTooLarge)
			return
		}
		upstreamContext := request.Context()
		if readiness {
			var cancel context.CancelFunc
			upstreamContext, cancel = context.WithTimeout(upstreamContext, 5*time.Second)
			defer cancel()
		}
		upstream, err := http.NewRequestWithContext(
			upstreamContext, request.Method,
			"http://coding-executor.internal"+request.URL.Path,
			bytes.NewReader(body),
		)
		if err != nil {
			writeError(response, http.StatusBadGateway)
			return
		}
		if !readiness {
			copyRequestHeader(upstream.Header, request.Header, "Content-Type")
			copyRequestHeader(upstream.Header, request.Header, "Content-Encoding")
			copyRequestHeader(upstream.Header, request.Header, codingcontrol.EnvelopeHeader)
		}
		upstream.ContentLength = int64(len(body))
		result, err := proxy.client.Do(upstream)
		if err != nil {
			writeError(response, http.StatusBadGateway)
			return
		}
		defer result.Body.Close()
		responseLimit := maximumResponseBytes
		if readiness {
			responseLimit = 4 << 10
		}
		resultBody, err := io.ReadAll(io.LimitReader(result.Body, int64(responseLimit+1)))
		if err != nil || len(resultBody) > responseLimit {
			writeError(response, http.StatusBadGateway)
			return
		}
		copyResponseHeader(response.Header(), result.Header, "Content-Type")
		copyResponseHeader(response.Header(), result.Header, "Cache-Control")
		copyResponseHeader(response.Header(), result.Header, "X-Content-Type-Options")
		response.Header().Set("Content-Length", strconv.Itoa(len(resultBody)))
		response.WriteHeader(result.StatusCode)
		_, _ = response.Write(resultBody)
	})
}

func ServerTLSConfig(certificate tls.Certificate, clientCAs *x509.CertPool) (*tls.Config, error) {
	if len(certificate.Certificate) == 0 || clientCAs == nil {
		return nil, ErrInvalidConfig
	}
	return &tls.Config{
		MinVersion: tls.VersionTLS13, MaxVersion: tls.VersionTLS13,
		Certificates: []tls.Certificate{certificate}, ClientCAs: clientCAs,
		ClientAuth: tls.RequireAndVerifyClientCert,
		NextProtos: []string{"http/1.1"}, SessionTicketsDisabled: true,
	}, nil
}

func validatorCertificateHotkey(state *tls.ConnectionState) (string, bool) {
	if state == nil || !state.HandshakeComplete || state.Version != tls.VersionTLS13 ||
		len(state.PeerCertificates) == 0 || len(state.VerifiedChains) == 0 {
		return "", false
	}
	certificate := state.PeerCertificates[0]
	if len(certificate.URIs) != 1 {
		return "", false
	}
	uri := certificate.URIs[0]
	if uri == nil || uri.Scheme != "spiffe" || uri.Host != "dittobench.ai" ||
		uri.RawPath != "" || uri.RawQuery != "" || uri.Fragment != "" || uri.User != nil {
		return "", false
	}
	hotkey := strings.TrimPrefix(uri.Path, "/validator/")
	if uri.Path != "/validator/"+hotkey || !codingcontrol.ValidValidatorHotkey(hotkey) {
		return "", false
	}
	return hotkey, true
}

func copyRequestHeader(target, source http.Header, name string) {
	values := source.Values(name)
	if len(values) == 1 && values[0] != "" {
		target.Set(name, values[0])
	}
}

func copyResponseHeader(target, source http.Header, name string) {
	if value := source.Get(name); value != "" {
		target.Set(name, value)
	}
}

func setPrivateHeaders(response http.ResponseWriter) {
	response.Header().Set("Cache-Control", "no-store")
	response.Header().Set("Content-Type", "application/json")
	response.Header().Set("X-Content-Type-Options", "nosniff")
}

func writeError(response http.ResponseWriter, status int) {
	body := []byte(`{"error":"executor_transport_rejected"}` + "\n")
	response.Header().Set("Content-Length", strconv.Itoa(len(body)))
	response.WriteHeader(status)
	_, _ = response.Write(body)
}

func ValidatorURI(hotkey string) (*url.URL, error) {
	if !codingcontrol.ValidValidatorHotkey(hotkey) {
		return nil, ErrInvalidConfig
	}
	return url.Parse(validatorURIPrefix + hotkey)
}
