package codingrelay

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"net/http"
	"strconv"
)

// Handler returns the unwired OpenAI-compatible endpoint for POST
// /chat/completions. The future gateway must mount it behind an unguessable,
// source-bound outer capability route.
func (relay *Relay) Handler() http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Cache-Control", "no-store")
		response.Header().Set("Content-Type", "application/json")
		response.Header().Set("X-Content-Type-Options", "nosniff")
		if relay == nil {
			writeError(response, http.StatusBadGateway, "relay_unavailable", "relay unavailable")
			return
		}
		if request.URL.Path != "/chat/completions" || request.URL.RawQuery != "" {
			writeError(response, http.StatusNotFound, "not_found", "not found")
			return
		}
		if request.Method != http.MethodPost {
			response.Header().Set("Allow", http.MethodPost)
			writeError(response, http.StatusMethodNotAllowed, "method_not_allowed", "method not allowed")
			return
		}
		mediaType, _, err := mime.ParseMediaType(request.Header.Get("Content-Type"))
		if err != nil || mediaType != "application/json" || request.Header.Get("Content-Encoding") != "" {
			writeError(response, http.StatusUnsupportedMediaType, "unsupported_media_type", "application json required")
			return
		}
		if request.ContentLength > int64(relay.policy.MaxRequestBytes) {
			writeError(response, http.StatusRequestEntityTooLarge, "request_too_large", "request exceeds limit")
			return
		}
		if !relay.acquireRequest() {
			writeError(response, http.StatusConflict, "concurrent_request", "concurrent request")
			return
		}
		defer relay.releaseRequest()
		body, err := io.ReadAll(http.MaxBytesReader(response, request.Body, int64(relay.policy.MaxRequestBytes)))
		if err != nil {
			writeError(response, http.StatusRequestEntityTooLarge, "request_too_large", "request exceeds limit")
			return
		}
		body, err = relay.completeRequest(request.Context(), body)
		if err != nil {
			writeError(response, statusForError(err), errorCode(err), safeErrorText(err))
			return
		}
		response.Header().Set("Content-Length", strconv.Itoa(len(body)))
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(body)
	})
}

func statusForError(err error) int {
	switch {
	case errors.Is(err, ErrInvalidRequest):
		return http.StatusBadRequest
	case errors.Is(err, ErrCapabilityRevoked), errors.Is(err, ErrCapabilityExpired):
		return http.StatusGone
	case errors.Is(err, ErrConcurrentRequest):
		return http.StatusConflict
	case errors.Is(err, ErrBudgetExhausted):
		return http.StatusTooManyRequests
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return http.StatusRequestTimeout
	default:
		return http.StatusBadGateway
	}
}

func writeError(response http.ResponseWriter, status int, code, message string) {
	body, err := json.Marshal(map[string]any{
		"error": map[string]string{
			"message": message,
			"type":    "coding_relay_error",
			"code":    code,
		},
	})
	if err != nil {
		body = []byte(`{"error":{"message":"relay unavailable","type":"coding_relay_error","code":"relay_unavailable"}}`)
		status = http.StatusBadGateway
	}
	body = append(body, '\n')
	response.Header().Set("Content-Length", strconv.Itoa(len(body)))
	response.WriteHeader(status)
	_, _ = response.Write(body)
}
