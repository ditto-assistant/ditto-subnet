package relayhttp

import (
	"encoding/json"
	"log/slog"
	"net/http"
)

// Numeric error codes. The numeric error_code in the JSON envelope is the
// AUTHORITATIVE discriminator on the wire; the HTTP status is only a coarse
// retryable(503)/terminal(429) hint. Old brokers key on these exact values —
// never change a pairing.
const (
	CodeInternalError     = 3000 // catch-all 500
	CodeRequestValidation = 3001 // 422, type-parse failures of body/headers
	CodeHTTPException     = 3002 // any endpoint-raised HTTP error
	CodeValidatorAuth     = 4000 // 401, validator authentication failed

	CodeDeclineUnattributed        = 4100
	CodeDeclineGrantRevoked        = 4101
	CodeDeclineBudgetExhausted     = 4102
	CodeDeclineAtCapacity          = 4103
	CodeDeclineTokenBudget         = 4104
	CodeDeclineLeaseExpired        = 4105
	CodeDeclineNonceReplayed       = 4106
	CodeDeclineModelNotPermitted   = 4107
	CodeDeclineGrantNotExchanged   = 4108
	CodeDeclineReservationTooLarge = 4109
)

// ErrorEnvelope is the exact JSON error body shape:
//
//	{"error_code": <int>, "message": "<string>", "request_id": "<rid>"}
type ErrorEnvelope struct {
	ErrorCode int    `json:"error_code"`
	Message   string `json:"message"`
	RequestID string `json:"request_id"`
}

// WriteError writes the error envelope with the given status/code/message and
// optional extra headers (e.g. Retry-After). X-Request-ID is set again here
// as a backup, mirroring the Python exception handlers.
func WriteError(w http.ResponseWriter, r *http.Request, status, code int, message string, headers http.Header) {
	rid := RequestID(r.Context())
	for k, vs := range headers {
		for _, v := range vs {
			w.Header().Add(k, v)
		}
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set(requestIDHeader, rid)
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(ErrorEnvelope{ErrorCode: code, Message: message, RequestID: rid})
}

// WriteInternalError is the catch-all 500 envelope.
func WriteInternalError(w http.ResponseWriter, r *http.Request) {
	WriteError(w, r, http.StatusInternalServerError, CodeInternalError, "internal server error", nil)
}

// WriteValidationError is the 422 envelope for type-parse failures. Details
// are logged server-side, never echoed.
func WriteValidationError(w http.ResponseWriter, r *http.Request) {
	WriteError(w, r, http.StatusUnprocessableEntity, CodeRequestValidation, "request validation failed", nil)
}

// WriteValidatorAuthError is the 401/4000 envelope.
func WriteValidatorAuthError(w http.ResponseWriter, r *http.Request) {
	WriteError(w, r, http.StatusUnauthorized, CodeValidatorAuth, "validator authentication failed", nil)
}

// WriteHTTPError is the generic endpoint-raised error (Python HTTPException):
// arbitrary status, code 3002, message = detail.
func WriteHTTPError(w http.ResponseWriter, r *http.Request, status int, message string, headers http.Header) {
	WriteError(w, r, status, CodeHTTPException, message, headers)
}

// Decline is the admission-refusal vocabulary of begin_inference_request.
type Decline int

const (
	DeclineUnattributed Decline = iota
	DeclineGrantRevoked
	DeclineBudgetExhausted
	DeclineAtCapacity
	DeclineTokenBudgetExhausted
	DeclineLeaseExpired
	DeclineNonceReplayed
	DeclineModelNotPermitted
	DeclineGrantNotExchanged
	DeclineReservationTooLarge
)

// Lane names used in decline messages. Note the chat lane is called
// "inference" on the wire.
const (
	LaneInference = "inference"
	LaneEmbedding = "embedding"
)

// declineWire fixes the (status, code, message-suffix) triple per decline.
var declineWire = map[Decline]struct {
	status int
	code   int
	text   string
}{
	DeclineUnattributed:         {http.StatusTooManyRequests, CodeDeclineUnattributed, "grant unavailable, and the reason is deliberately not disclosed to an unauthenticated caller"},
	DeclineGrantRevoked:         {http.StatusTooManyRequests, CodeDeclineGrantRevoked, "grant was revoked"},
	DeclineBudgetExhausted:      {http.StatusTooManyRequests, CodeDeclineBudgetExhausted, "grant has spent its request budget"},
	DeclineAtCapacity:           {http.StatusServiceUnavailable, CodeDeclineAtCapacity, "lane is at capacity"},
	DeclineTokenBudgetExhausted: {http.StatusTooManyRequests, CodeDeclineTokenBudget, "grant has spent its token budget"},
	DeclineLeaseExpired:         {http.StatusTooManyRequests, CodeDeclineLeaseExpired, "grant has expired"},
	DeclineNonceReplayed:        {http.StatusTooManyRequests, CodeDeclineNonceReplayed, "request nonce was already used"},
	DeclineModelNotPermitted:    {http.StatusTooManyRequests, CodeDeclineModelNotPermitted, "grant does not permit this model"},
	DeclineGrantNotExchanged:    {http.StatusTooManyRequests, CodeDeclineGrantNotExchanged, "grant has not been exchanged for a bearer"},
	DeclineReservationTooLarge:  {http.StatusTooManyRequests, CodeDeclineReservationTooLarge, "request exceeds the grant's entire token budget"},
}

// Retryable reports whether the decline is the one retryable member
// (AT_CAPACITY); every other decline is terminal.
func (d Decline) Retryable() bool { return d == DeclineAtCapacity }

// String is the stable snake_case label used in logs, metrics and the
// inference trace capture. It is NOT the wire message.
func (d Decline) String() string {
	switch d {
	case DeclineUnattributed:
		return "unattributed"
	case DeclineGrantRevoked:
		return "grant_revoked"
	case DeclineBudgetExhausted:
		return "budget_exhausted"
	case DeclineAtCapacity:
		return "at_capacity"
	case DeclineTokenBudgetExhausted:
		return "token_budget_exhausted"
	case DeclineLeaseExpired:
		return "lease_expired"
	case DeclineNonceReplayed:
		return "nonce_replayed"
	case DeclineModelNotPermitted:
		return "model_not_permitted"
	case DeclineGrantNotExchanged:
		return "grant_not_exchanged"
	case DeclineReservationTooLarge:
		return "reservation_too_large"
	}
	return "unknown"
}

// WriteDecline writes the decline envelope for the given lane.
// AT_CAPACITY → 503 + "Retry-After: 1"; terminal declines → 429. The message
// is "{lane} {text}" (AT_CAPACITY reads "{lane} lane is at capacity").
func WriteDecline(w http.ResponseWriter, r *http.Request, d Decline, lane string) {
	wire, ok := declineWire[d]
	if !ok {
		WriteInternalError(w, r)
		return
	}
	var headers http.Header
	if d == DeclineAtCapacity {
		headers = http.Header{"Retry-After": []string{"1"}}
	}
	WriteError(w, r, wire.status, wire.code, lane+" "+wire.text, headers)
}

// RecoverMiddleware converts panics into the catch-all 500 envelope so no
// request ever escapes without the wire shape.
func RecoverMiddleware(logger *slog.Logger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				logger.Error("panic serving request",
					slog.String("request_id", RequestID(r.Context())),
					slog.String("path", r.URL.Path),
					slog.Any("panic", rec),
				)
				WriteInternalError(w, r)
			}
		}()
		next.ServeHTTP(w, r)
	})
}
