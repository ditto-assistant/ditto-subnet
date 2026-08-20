package inference

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
)

var (
	ss58Pattern      = regexp.MustCompile(`^[1-9A-HJ-NP-Za-km-z]{47,48}$`)
	brokerKeyPattern = regexp.MustCompile(`^[A-Za-z0-9_-]{43}=?$`)
	signaturePattern = regexp.MustCompile(`^[0-9a-fA-F]{128}$`)
)

// exchangeRequestWire mirrors InferenceExchangeRequest (unknown fields ignored;
// every known field required; requested_at must be timezone-aware).
type exchangeRequestWire struct {
	ValidatorHotkey *string `json:"validator_hotkey"`
	GrantID         *string `json:"grant_id"`
	BrokerPublicKey *string `json:"broker_public_key"`
	Nonce           *string `json:"nonce"`
	RequestedAt     *string `json:"requested_at"`
	Signature       *string `json:"signature"`
}

type exchangeParsed struct {
	validatorHotkey string
	grantID         uuid.UUID
	brokerPublicKey string
	nonce           uuid.UUID
	requestedAt     time.Time
	signature       string
}

func parseExchangeRequest(body []byte) (*exchangeParsed, bool) {
	dec := json.NewDecoder(bytes.NewReader(body))
	var wire exchangeRequestWire
	if err := dec.Decode(&wire); err != nil {
		return nil, false
	}
	if dec.More() {
		return nil, false
	}
	if wire.ValidatorHotkey == nil || wire.GrantID == nil || wire.BrokerPublicKey == nil ||
		wire.Nonce == nil || wire.RequestedAt == nil || wire.Signature == nil {
		return nil, false
	}
	if !ss58Pattern.MatchString(*wire.ValidatorHotkey) ||
		!brokerKeyPattern.MatchString(*wire.BrokerPublicKey) ||
		!signaturePattern.MatchString(*wire.Signature) {
		return nil, false
	}
	grantID, err := uuid.Parse(*wire.GrantID)
	if err != nil {
		return nil, false
	}
	nonce, err := uuid.Parse(*wire.Nonce)
	if err != nil {
		return nil, false
	}
	requestedAt, aware, derr := parseHeaderDatetime(*wire.RequestedAt)
	if derr != nil || !aware {
		// requested_at MUST be timezone-aware (field validator), else 422.
		return nil, false
	}
	return &exchangeParsed{
		validatorHotkey: *wire.ValidatorHotkey,
		grantID:         grantID,
		brokerPublicKey: *wire.BrokerPublicKey,
		nonce:           nonce,
		requestedAt:     requestedAt,
		signature:       *wire.Signature,
	}, true
}

// pydanticDatetime renders a tz-aware UTC datetime the way Pydantic v2
// serializes it: "Z" suffix, microseconds only when nonzero.
func pydanticDatetime(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05") + "Z"
	}
	return t.Format("2006-01-02T15:04:05.000000") + "Z"
}

// exchangeResponse mirrors InferenceExchangeResponse field-for-field
// (provider/profile_revision/model omitted entirely when nil).
type exchangeResponse struct {
	GrantID         string  `json:"grant_id"`
	Bearer          string  `json:"bearer"`
	ProxyURL        string  `json:"proxy_url"`
	ExpiresAt       string  `json:"expires_at"`
	Generation      int32   `json:"generation"`
	Provider        *string `json:"provider,omitempty"`
	ProfileRevision *string `json:"profile_revision,omitempty"`
	Model           *string `json:"model,omitempty"`
}

// devBypassPermit mirrors _dev_bypass_permit: only when the flag is truthy
// AND the network is not finney/mainnet (refused with an ERROR log there).
func (d *Deps) devBypassPermit() bool {
	if !d.Cfg.Chain.DevAllowUnpermittedValidator {
		return false
	}
	net := strings.ToLower(d.Cfg.Chain.Network)
	if strings.HasPrefix(net, "finney") || net == "mainnet" {
		d.Logger.Error("refusing DITTO_DEV_ALLOW_UNPERMITTED_VALIDATOR on production network; enforcing the validator permit check",
			slog.String("network", d.Cfg.Chain.Network))
		return false
	}
	return true
}

// handleExchange is POST /api/v1/inference/exchange.
func (d *Deps) handleExchange(w http.ResponseWriter, r *http.Request) {
	// Body parse + Pydantic-equivalent validation precedes everything
	// (FastAPI validates the body before the endpoint runs).
	body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		relayhttp.WriteValidationError(w, r)
		return
	}
	payload, ok := parseExchangeRequest(body)
	if !ok {
		relayhttp.WriteValidationError(w, r)
		return
	}
	if !d.Cfg.Inference.Enabled {
		relayhttp.WriteHTTPError(w, r, http.StatusNotFound, "inference proxy is disabled", nil)
		return
	}
	if r.Header.Get("X-Validator-Hotkey") != payload.validatorHotkey {
		relayhttp.WriteValidatorAuthError(w, r)
		return
	}
	now := d.now()
	if absDuration(now.Sub(payload.requestedAt)) > exchangeMaxAge {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "inference exchange is stale", nil)
		return
	}
	message := exchangeMessage(payload.validatorHotkey, payload.grantID, payload.brokerPublicKey, payload.nonce, payload.requestedAt)
	if !verifySr25519(payload.validatorHotkey, message, payload.signature) {
		relayhttp.WriteValidatorAuthError(w, r)
		return
	}
	if !d.devBypassPermit() {
		permitted, perr := d.Permits.ValidatorPermit(r.Context(), payload.validatorHotkey)
		if perr != nil {
			d.Logger.Warn("chain unreachable during validator authz", slog.String("error", perr.Error()))
			relayhttp.WriteHTTPError(w, r, http.StatusServiceUnavailable, "chain unavailable; retry shortly", nil)
			return
		}
		if !permitted {
			relayhttp.WriteValidatorAuthError(w, r)
			return
		}
	} else {
		d.Logger.Warn("DEV: allowing validator request without permit",
			slog.String("hotkey", payload.validatorHotkey), slog.Int("netuid", d.Cfg.Chain.Netuid))
	}

	now = d.now()
	ctx := r.Context()
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		d.Logger.Error("exchange: begin", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	defer func() { _ = tx.Rollback(ctx) }()
	q := d.Queries.WithTx(tx)

	// Nonce consumption inside a savepoint: a replay must not poison the
	// transaction (though on this endpoint nothing else was written yet).
	sp, err := tx.Begin(ctx)
	if err != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	if err := q.WithTx(sp).ConsumeValidatorNonce(ctx, postgres.ConsumeValidatorNonceParams{
		Nonce:           pgUUID(payload.nonce),
		ValidatorHotkey: payload.validatorHotkey,
		UsedAt:          pgTime(now),
		ExpiresAt:       pgTime(now.Add(exchangeMaxAge)),
	}); err != nil {
		_ = sp.Rollback(ctx)
		if isUniqueViolation(err) {
			relayhttp.WriteHTTPError(w, r, http.StatusConflict, "inference exchange nonce was already used", nil)
			return
		}
		d.Logger.Error("exchange: consume nonce", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if err := sp.Commit(ctx); err != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}

	activated, bearer, err := d.activateInferenceGrant(ctx, q, payload, now)
	if err != nil {
		d.Logger.Error("exchange: activate grant", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	// The transaction COMMITS even when activation refused: the nonce
	// consumption and any revocation write persist (matching the Python
	// endpoint, which raises its 409 after the transaction block).
	if err := tx.Commit(ctx); err != nil {
		d.Logger.Error("exchange: commit", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	if activated == nil {
		relayhttp.WriteHTTPError(w, r, http.StatusConflict, "inference grant is not live", nil)
		return
	}

	resp := exchangeResponse{
		GrantID:    uuid.UUID(activated.GrantID.Bytes).String(),
		Bearer:     bearer,
		ProxyURL:   d.Cfg.Inference.PublicBaseURL + "/api/v1/inference/chat/completions",
		ExpiresAt:  pydanticDatetime(activated.ExpiresAt.Time),
		Generation: activated.Generation,
	}
	if activated.BenchVersion >= 7 {
		if activated.RouteProvider.Valid {
			resp.Provider = &activated.RouteProvider.String
		}
		if activated.RouteProfile.Valid {
			resp.ProfileRevision = &activated.RouteProfile.String
		}
		models := allowedModels(activated)
		if len(models) == 0 {
			// Python would IndexError -> 500; fail the same way, loudly.
			d.Logger.Error("exchange: activated grant has no allowed models",
				slog.String("grant_id", resp.GrantID))
			relayhttp.WriteInternalError(w, r)
			return
		}
		resp.Model = &models[0]
	}
	out, err := compactJSON(resp)
	if err != nil {
		relayhttp.WriteInternalError(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(out)
}

// activateInferenceGrant reproduces ditto/db/queries/inference.py::
// activate_inference_grant inside the caller's transaction. A nil grant
// means "not live" (the endpoint 409s after committing).
func (d *Deps) activateInferenceGrant(ctx context.Context, q *postgres.Queries,
	payload *exchangeParsed, now time.Time) (*postgres.InferenceGrant, string, error) {

	snapshot, err := q.GetInferenceGrant(ctx, pgUUID(payload.grantID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, "", nil
		}
		return nil, "", err
	}
	if snapshot.ValidatorHotkey != payload.validatorHotkey {
		return nil, "", nil
	}
	var ticket *postgres.ValidatorTicket
	ticketRow, err := q.GetValidatorTicketForUpdate(ctx, postgres.GetValidatorTicketForUpdateParams{
		AgentID:         snapshot.AgentID,
		BenchVersion:    snapshot.BenchVersion,
		ValidatorHotkey: snapshot.ValidatorHotkey,
	})
	if err == nil {
		ticket = &ticketRow
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return nil, "", err
	}
	grant, err := q.GetInferenceGrantForUpdate(ctx, pgUUID(payload.grantID))
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, "", nil
		}
		return nil, "", err
	}
	if grant.ValidatorHotkey != payload.validatorHotkey ||
		ticket == nil ||
		ticket.Status != postgres.TicketstatusIssued ||
		!ticket.Deadline.Time.Equal(grant.TicketDeadline.Time) ||
		!ticket.Deadline.Time.After(now) ||
		grant.Status == "revoked" || grant.Status == "exhausted" {
		// The one mid-lease revocation write that COMMITS via /exchange.
		if err := q.RevokeInferenceGrant(ctx, postgres.RevokeInferenceGrantParams{
			Now:     pgTime(now),
			GrantID: pgUUID(payload.grantID),
		}); err != nil {
			return nil, "", err
		}
		return nil, "", nil
	}
	started, err := q.ListStartedInferenceRequestsForUpdate(ctx, pgUUID(payload.grantID))
	if err != nil {
		return nil, "", err
	}
	staleCutoff := now.Add(-2 * time.Duration(d.Cfg.Inference.TimeoutSeconds) * time.Second)
	for _, req := range started {
		if !req.StartedAt.Time.Before(staleCutoff) {
			// A restart may rotate only after every previous-generation call
			// has settled or crossed the recovery window.
			return nil, "", nil
		}
	}
	for _, req := range started {
		if err := q.CancelInferenceRequestChargingReservation(ctx, postgres.CancelInferenceRequestChargingReservationParams{
			Now:     pgTime(now),
			GrantID: req.GrantID,
			Nonce:   req.Nonce,
		}); err != nil {
			return nil, "", err
		}
	}
	bearer, err := newBearer()
	if err != nil {
		return nil, "", err
	}
	if err := q.ActivateInferenceGrant(ctx, postgres.ActivateInferenceGrantParams{
		BearerDigest:    bearerDigest(bearer),
		BrokerPublicKey: trimBase64Padding(payload.brokerPublicKey),
		SlotID:          ticket.SlotID,
		ExpiresAt:       pgTime(ticket.Deadline.Time),
		Now:             pgTime(now),
		GrantID:         pgUUID(payload.grantID),
	}); err != nil {
		return nil, "", err
	}
	// Reflect the rotation on the in-memory row for the response.
	grant.BearerDigest = textValue(bearerDigest(bearer))
	grant.BrokerPublicKey = textValue(trimBase64Padding(payload.brokerPublicKey))
	grant.Generation++
	grant.Status = "active"
	grant.SlotID = ticket.SlotID
	grant.ExpiresAt = pgTime(ticket.Deadline.Time)
	grant.ActiveRequests = 0
	grant.EmbeddingActiveRequests = 0
	grant.UpdatedAt = pgTime(now)
	return &grant, bearer, nil
}

func absDuration(d time.Duration) time.Duration {
	if d < 0 {
		return -d
	}
	return d
}

func textValue(s string) pgtype.Text {
	return pgtype.Text{String: s, Valid: true}
}
