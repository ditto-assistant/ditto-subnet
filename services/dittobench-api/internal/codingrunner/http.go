package codingrunner

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

// Handler returns a task-scoped HTTP handler for POST /tool. The caller must
// mount it behind an unguessable, source-bound outer capability route.
func (session *Session) Handler() http.Handler {
	return http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		response.Header().Set("Cache-Control", "no-store")
		response.Header().Set("Content-Type", "application/json")
		if request.URL.Path != "/tool" {
			writeJSON(response, http.StatusNotFound, map[string]string{"error": "not found"})
			return
		}
		if request.Method != http.MethodPost {
			response.Header().Set("Allow", http.MethodPost)
			writeJSON(response, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
			return
		}
		body, err := io.ReadAll(http.MaxBytesReader(response, request.Body, MaxToolRequestBytes))
		if err != nil {
			writeJSON(response, http.StatusRequestEntityTooLarge, map[string]string{"error": "workspace tool request exceeds body limit"})
			return
		}
		toolRequest, err := parseToolRequest(body)
		if err != nil {
			writeJSON(response, http.StatusBadRequest, map[string]string{"error": boundedText(err.Error(), 512)})
			return
		}
		result, err := session.Invoke(request.Context(), toolRequest)
		if err != nil {
			status := http.StatusBadRequest
			switch {
			case errors.Is(err, errSessionClosed):
				status = http.StatusGone
			case errors.Is(err, errCapabilityRevoked) || errors.Is(err, errCapabilityExpired):
				status = http.StatusGone
			case errors.Is(err, errCapabilityIdentity):
				status = http.StatusForbidden
			case errors.Is(err, errToolBudget):
				status = http.StatusTooManyRequests
			}
			writeJSON(response, status, map[string]string{"error": boundedText(err.Error(), 512)})
			return
		}
		body, err = json.Marshal(result)
		if err != nil || len(body) > session.manifest.Limits.MaxResponseBytes {
			writeJSON(response, http.StatusInternalServerError, map[string]string{"error": "workspace response encoding failed"})
			return
		}
		body = append(body, '\n')
		response.Header().Set("Content-Length", integerString(len(body)))
		response.WriteHeader(http.StatusOK)
		_, _ = response.Write(body)
	})
}

func writeJSON(response http.ResponseWriter, status int, value any) {
	body, err := json.Marshal(value)
	if err != nil {
		body = []byte(`{"error":"response encoding failed"}`)
		status = http.StatusInternalServerError
	}
	body = append(body, '\n')
	response.Header().Set("Content-Length", integerString(len(body)))
	response.WriteHeader(status)
	_, _ = response.Write(body)
}

func integerString(value int) string {
	if value == 0 {
		return "0"
	}
	var buffer [24]byte
	position := len(buffer)
	for value > 0 {
		position--
		buffer[position] = byte('0' + value%10)
		value /= 10
	}
	return string(buffer[position:])
}
