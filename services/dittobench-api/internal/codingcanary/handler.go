package codingcanary

import (
	"context"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"io"
	"mime"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/google/uuid"
)

func validUUID(value string) bool {
	parsed, err := uuid.Parse(value)
	return err == nil && parsed != uuid.Nil
}

type Config struct {
	ControlToken     string
	Backend          Backend
	OperationTimeout time.Duration
	Now              func() time.Time
}

type Service struct {
	mu      sync.Mutex
	backend Backend
	now     func() time.Time
	timeout time.Duration
	token   [sha256.Size]byte
	active  map[string]struct{}
	lastNow time.Time
	closed  bool
}

func New(config Config) (*Service, error) {
	if nilLike(config.Backend) || !validControlToken(config.ControlToken) ||
		config.OperationTimeout < 0 || config.OperationTimeout > maximumTimeout {
		return nil, ErrInvalidConfig
	}
	if config.OperationTimeout == 0 {
		config.OperationTimeout = defaultTimeout
	}
	if config.Now == nil {
		config.Now = time.Now
	}
	now := config.Now().UTC()
	if now.IsZero() {
		return nil, ErrInvalidConfig
	}
	return &Service{
		backend: config.Backend, now: config.Now, timeout: config.OperationTimeout,
		token: sha256.Sum256([]byte(config.ControlToken)), lastNow: now,
		active: make(map[string]struct{}),
	}, nil
}

func (service *Service) Handler() http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		setPrivateHeaders(response)
		if service == nil {
			writeError(response, http.StatusServiceUnavailable, "unavailable")
			return
		}
		if request.URL.Path != "/v1/coding/certifier/canary" || request.URL.RawQuery != "" {
			writeError(response, http.StatusNotFound, "not_found")
			return
		}
		if request.Method != http.MethodPost {
			response.Header().Set("Allow", http.MethodPost)
			writeError(response, http.StatusMethodNotAllowed, "method_not_allowed")
			return
		}
		if !service.authorized(request) {
			writeError(response, http.StatusUnauthorized, "unauthorized")
			return
		}
		mediaType, _, err := mime.ParseMediaType(request.Header.Get("Content-Type"))
		if err != nil || mediaType != "application/json" || request.Header.Get("Content-Encoding") != "" {
			writeError(response, http.StatusUnsupportedMediaType, "unsupported_media_type")
			return
		}
		if request.ContentLength > maximumRequestBytes {
			writeError(response, http.StatusRequestEntityTooLarge, "request_too_large")
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(response, request.Body, maximumRequestBytes))
		if err != nil {
			writeError(response, http.StatusRequestEntityTooLarge, "request_too_large")
			return
		}
		now, err := service.trustedNow()
		if err != nil {
			writeError(response, http.StatusServiceUnavailable, "clock")
			return
		}
		value, err := parseRequest(body, now)
		if err != nil {
			writeError(response, http.StatusBadRequest, "invalid")
			return
		}
		backend, beginErr := service.begin(value.LeaseID)
		if beginErr != nil {
			status := http.StatusConflict
			code := "conflict"
			if beginErr == ErrClosed || beginErr == ErrUnavailable {
				status = http.StatusServiceUnavailable
				code = "unavailable"
			}
			writeError(response, status, code)
			return
		}
		defer service.release(value.LeaseID)
		operationContext, cancel := context.WithTimeout(request.Context(), remainingTimeout(value.Deadline, now, service.timeout))
		outcome, backendErr := backend.Certify(operationContext, value)
		callErr := operationContext.Err()
		cancel()
		if backendErr != nil || callErr != nil {
			writeError(response, http.StatusBadGateway, "backend")
			return
		}
		if !outcome.CapabilitiesRevoked || !outcome.HarnessDestroyed || outcome.LeaseID != value.LeaseID {
			writeError(response, http.StatusBadGateway, "backend_invalid")
			return
		}
		receipt, err := json.Marshal(outcome.Receipt)
		if err != nil {
			writeError(response, http.StatusBadGateway, "backend_invalid")
			return
		}
		encoded, err := json.Marshal(Response{
			Schema: ResponseSchema, LeaseID: outcome.LeaseID,
			CapabilitiesRevoked: true, HarnessDestroyed: true, Receipt: receipt,
		})
		if err != nil || len(encoded)+1 > maximumResponseBytes {
			writeError(response, http.StatusBadGateway, "backend_invalid")
			return
		}
		encoded = append(encoded, '\n')
		response.Header().Set("Content-Length", strconv.Itoa(len(encoded)))
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(encoded)
	})
}

func (service *Service) Close() error {
	if service == nil {
		return nil
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	if len(service.active) != 0 {
		return ErrConcurrent
	}
	service.closed = true
	service.backend = nil
	clear(service.active)
	service.token = [sha256.Size]byte{}
	return nil
}

func (service *Service) authorized(request *http.Request) bool {
	values := request.Header.Values("Authorization")
	if len(values) != 1 || !strings.HasPrefix(values[0], "Bearer ") {
		return false
	}
	token := strings.TrimPrefix(values[0], "Bearer ")
	digest := sha256.Sum256([]byte(token))
	service.mu.Lock()
	expected := service.token
	closed := service.closed
	service.mu.Unlock()
	return !closed && subtle.ConstantTimeCompare(digest[:], expected[:]) == 1
}

func (service *Service) begin(key string) (Backend, error) {
	service.mu.Lock()
	defer service.mu.Unlock()
	if service.closed {
		return nil, ErrClosed
	}
	if service.backend == nil {
		return nil, ErrUnavailable
	}
	if _, exists := service.active[key]; exists {
		return nil, ErrConcurrent
	}
	service.active[key] = struct{}{}
	return service.backend, nil
}

func (service *Service) release(key string) {
	service.mu.Lock()
	delete(service.active, key)
	service.mu.Unlock()
}

func (service *Service) trustedNow() (time.Time, error) {
	now := service.now().UTC()
	if now.IsZero() {
		return time.Time{}, ErrUnavailable
	}
	service.mu.Lock()
	defer service.mu.Unlock()
	if now.Before(service.lastNow) {
		return time.Time{}, ErrUnavailable
	}
	service.lastNow = now
	return now, nil
}

func parseRequest(body []byte, now time.Time) (Request, error) {
	if len(body) == 0 {
		return Request{}, ErrInvalid
	}
	var request Request
	if err := json.Unmarshal(body, &request); err != nil {
		return Request{}, ErrInvalid
	}
	if request.Schema != RequestSchema || request.WeightEligible ||
		request.CodingContractVersion != 1 || !validSHA256(request.AgentArtifactSHA256) ||
		!validSHA256(request.ScreenedImageSHA256) || !validSHA256(request.CanaryManifestSHA256) ||
		!validSHA256(request.RunnerPlanSHA256) || !validSHA256(request.GraderPlanSHA256) ||
		!validSHA256(request.ResourceProfileSHA256) || !validSHA256(request.InferencePolicySHA256) ||
		!validOCIDigest(request.ScreenedImageID) ||
		request.ScreenedImageRef != "ditto-screen/"+request.AgentID+":latest" ||
		!validUUID(request.OperationID) || !validUUID(request.LeaseID) ||
		!validUUID(request.AgentID) || !validUUID(request.ScreenedImageUploadID) ||
		request.ScreenedImageSizeBytes <= 0 || request.ScreenedImageSizeBytes > 8<<30 ||
		request.ScreeningPolicyVersion < 9 || request.ScreeningPolicyVersion > 1_000_000 ||
		request.BenchVersion < 7 || request.BenchVersion > 1_000_000 ||
		!validImageURL(request.ImageURL) || !request.Deadline.After(now) ||
		request.Deadline.After(now.Add(2*time.Hour)) || !request.ImageExpiresAt.After(now) ||
		request.ImageExpiresAt.After(request.Deadline) || request.ImageExpiresAt.After(now.Add(6*time.Minute)) {
		return Request{}, ErrInvalid
	}
	return request, nil
}

func validOCIDigest(value string) bool {
	return strings.HasPrefix(value, "sha256:") && validSHA256(strings.TrimPrefix(value, "sha256:"))
}

func validImageURL(value string) bool {
	if len(value) == 0 || len(value) > 16<<10 {
		return false
	}
	for _, character := range value {
		if character < 32 || character > 126 {
			return false
		}
	}
	parsed, err := url.ParseRequestURI(value)
	return err == nil && parsed.Scheme == "https" && parsed.Hostname() != "" && parsed.User == nil &&
		parsed.Path != "" && parsed.RawQuery != "" && parsed.Fragment == "" &&
		(parsed.Port() == "" || parsed.Port() == "443")
}

func remainingTimeout(deadline time.Time, now time.Time, configured time.Duration) time.Duration {
	remaining := deadline.Sub(now)
	if remaining > configured {
		return configured
	}
	if remaining < time.Second {
		return time.Second
	}
	return remaining
}

func validControlToken(value string) bool {
	if len(value) < 32 || len(value) > 256 {
		return false
	}
	for _, character := range value {
		if character > unicode.MaxASCII || !(unicode.IsLetter(character) || unicode.IsDigit(character) || character == '_' || character == '-') {
			return false
		}
	}
	return true
}

func validSHA256(value string) bool {
	if len(value) != sha256Length {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil && value == strings.ToLower(value)
}

func validIdentifier(value string, maximum int) bool {
	if value == "" || len(value) > maximum || !utf8.ValidString(value) {
		return false
	}
	for _, character := range value {
		if unicode.IsSpace(character) || unicode.IsControl(character) {
			return false
		}
	}
	return true
}

func setPrivateHeaders(response http.ResponseWriter) {
	response.Header().Set("Cache-Control", "no-store")
	response.Header().Set("Content-Type", "application/json")
}

func writeError(response http.ResponseWriter, status int, code string) {
	encoded, _ := json.Marshal(map[string]string{"error": code})
	response.Header().Set("Content-Length", strconv.Itoa(len(encoded)))
	response.WriteHeader(status)
	_, _ = response.Write(encoded)
}

func nilLike(value any) bool {
	return value == nil
}
