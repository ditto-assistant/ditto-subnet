package inference

import (
	"bytes"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/ditto-assistant/model-relay/internal/relayhttp"
)

const sourceReviewEventBodyBytes = 4096

type sourceReviewProviderEvent struct {
	ReviewID string `json:"review_id"`
	Status   int    `json:"status"`
	Started  string `json:"started_at"`
}

func (d *Deps) handleSourceReviewProviderEvent(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, sourceReviewEventBodyBytes+1))
	if err != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if len(body) > sourceReviewEventBodyBytes {
		relayhttp.WriteHTTPError(w, r, http.StatusRequestEntityTooLarge, "provider event is too large", nil)
		return
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.DisallowUnknownFields()
	var event sourceReviewProviderEvent
	if err := dec.Decode(&event); err != nil {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "invalid provider event", nil)
		return
	}
	var trailing any
	if err := dec.Decode(&trailing); !errors.Is(err, io.EOF) {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "invalid provider event", nil)
		return
	}
	reviewID, err := uuid.Parse(event.ReviewID)
	if err != nil || (event.Status != http.StatusOK && event.Status != http.StatusTooManyRequests) {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "invalid provider event", nil)
		return
	}
	startedAt, err := time.Parse(time.RFC3339Nano, event.Started)
	now := d.now()
	if err != nil || startedAt.After(now.Add(time.Minute)) || startedAt.Before(now.Add(-2*time.Hour)) {
		relayhttp.WriteHTTPError(w, r, http.StatusBadRequest, "invalid provider event", nil)
		return
	}
	token := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if token == "" || token == r.Header.Get("Authorization") {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid source review token", nil)
		return
	}
	auth, err := d.Queries.GetSourceReviewProviderEventAuth(r.Context(), pgUUID(reviewID))
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		relayhttp.WriteInternalError(w, r)
		return
	}
	digest := sha256.Sum256([]byte(token))
	actual := hex.EncodeToString(digest[:])
	expiresAt := auth.JobTokenExpiresAt.Time
	if !auth.JobTokenHash.Valid || !auth.JobTokenExpiresAt.Valid ||
		(auth.Status != "leased" && auth.Status != "running") ||
		expiresAt.Before(now) ||
		subtle.ConstantTimeCompare([]byte(actual), []byte(auth.JobTokenHash.String)) != 1 {
		relayhttp.WriteHTTPError(w, r, http.StatusUnauthorized, "invalid source review token", nil)
		return
	}

	var stored bool
	if event.Status == http.StatusTooManyRequests {
		stored = d.openProviderCircuit(
			r.Context(), startedAt, event.Status, "source_review_http_429",
		)
	} else {
		stored = d.closeProviderCircuit(r.Context(), startedAt)
	}
	if !stored {
		relayhttp.WriteInternalError(w, r)
		return
	}
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusNoContent)
}
