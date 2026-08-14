// Package upload implements the first strangler slice of the upload plane:
// fee pricing and the ordinary pre-payment admission check. Finalized-payment
// recovery and multipart archive commit remain on Python until their chain,
// object-storage, and fingerprinting contracts have Go parity coverage.
package upload

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"regexp"
	"strconv"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/ditto-assistant/model-relay/internal/chain"
	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/server"
)

const (
	errorBadSignature       = 1100
	errorHotkeyUnregistered = 1101
	errorTarballTooLarge    = 1102
	errorHotkeyBanned       = 1103
	errorIdentical          = 1104
	errorCooldown           = 1105

	defaultCooldownSeconds = 3600
	defaultFeeAmountRao    = int64(40_000_000)
	admissionTTL           = 24 * time.Hour
	admissionBlockTTL      = 15 * time.Minute
)

var (
	ss58Pattern      = regexp.MustCompile(`^[1-9A-HJ-NP-Za-km-z]{47,48}$`)
	sha256Pattern    = regexp.MustCompile(`^[0-9a-f]{64}$`)
	signaturePattern = regexp.MustCompile(`^[0-9a-fA-F]{128}$`)
	blockHashPattern = regexp.MustCompile(`^0x[0-9a-fA-F]{64}$`)
)

type Deps struct {
	Cfg          *config.Config
	Logger       *slog.Logger
	Pool         *pgxpool.Pool
	Queries      *postgres.Queries
	Registration chain.RegistrationChecker
	Legacy       http.Handler
	Now          func() time.Time
}

func (d *Deps) now() time.Time {
	if d.Now != nil {
		return d.Now().UTC()
	}
	return time.Now().UTC()
}

func NewHandlers(deps *Deps) *server.UploadHandlers {
	return &server.UploadHandlers{
		EvalPricing: http.HandlerFunc(deps.handleEvalPricing),
		Check:       http.HandlerFunc(deps.handleCheck),
	}
}

type settings struct {
	revision        int32
	cooldownSeconds int32
	feeAmountRao    int64
	paymentAddress  string
}

func (d *Deps) effectiveSettings(ctx context.Context, q *postgres.Queries) (settings, error) {
	out := settings{
		cooldownSeconds: defaultCooldownSeconds,
		feeAmountRao:    defaultFeeAmountRao,
		paymentAddress:  d.Cfg.Upload.PaymentAddress,
	}
	row, err := q.GetLatestSubmissionSettings(ctx)
	if err == nil {
		out.revision = row.Revision
		out.cooldownSeconds = row.CooldownSeconds
		out.feeAmountRao = row.FeeAmountRao
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return settings{}, err
	}
	address, err := q.GetLatestSubmissionDepositAddress(ctx)
	if err == nil {
		out.paymentAddress = address
	} else if !errors.Is(err, pgx.ErrNoRows) {
		return settings{}, err
	}
	if out.paymentAddress == "" {
		return settings{}, errors.New("no effective upload payment address")
	}
	return out, nil
}

type evalPricingResponse struct {
	AmountRao   int64  `json:"amount_rao"`
	SendAddress string `json:"send_address"`
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func (d *Deps) handleEvalPricing(w http.ResponseWriter, r *http.Request) {
	value, err := d.effectiveSettings(r.Context(), d.Queries)
	if err != nil {
		d.Logger.Error("upload pricing query failed", slog.String("error", err.Error()))
		relayhttp.WriteInternalError(w, r)
		return
	}
	writeJSON(w, http.StatusOK, evalPricingResponse{AmountRao: value.feeAmountRao, SendAddress: value.paymentAddress})
}

type checkRequestWire struct {
	Hotkey                *string `json:"hotkey"`
	SHA256                *string `json:"sha256"`
	FileSizeBytes         *int64  `json:"file_size_bytes"`
	Signature             *string `json:"signature"`
	AllowIdenticalRescore bool    `json:"allow_identical_rescore"`
	ReserveSubmissionSlot bool    `json:"reserve_submission_slot"`
	PaymentBlockHash      *string `json:"payment_block_hash"`
	PaymentBlockNumber    *int64  `json:"payment_block_number"`
	PaymentExtrinsicIndex *int64  `json:"payment_extrinsic_index"`
}

func parseCheckRequest(body []byte) (*checkRequestWire, bool, bool) {
	dec := json.NewDecoder(bytes.NewReader(body))
	var wire checkRequestWire
	if err := dec.Decode(&wire); err != nil {
		return nil, false, false
	}
	if err := dec.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return nil, false, false
	}
	if wire.Hotkey == nil || wire.SHA256 == nil || wire.FileSizeBytes == nil || wire.Signature == nil ||
		!ss58Pattern.MatchString(*wire.Hotkey) || !sha256Pattern.MatchString(*wire.SHA256) ||
		*wire.FileSizeBytes < 1 || !signaturePattern.MatchString(*wire.Signature) {
		return nil, false, false
	}
	proofFields := 0
	if wire.PaymentBlockHash != nil {
		proofFields++
		if !blockHashPattern.MatchString(*wire.PaymentBlockHash) {
			return nil, false, false
		}
	}
	if wire.PaymentBlockNumber != nil {
		proofFields++
		if *wire.PaymentBlockNumber < 1 {
			return nil, false, false
		}
	}
	if wire.PaymentExtrinsicIndex != nil {
		proofFields++
		if *wire.PaymentExtrinsicIndex < 0 {
			return nil, false, false
		}
	}
	if proofFields != 0 && proofFields != 3 {
		return nil, false, false
	}
	return &wire, true, proofFields == 3
}

type checkResponse struct {
	OK                   bool     `json:"ok"`
	ErrorCodes           []int    `json:"error_codes"`
	Messages             []string `json:"messages"`
	PaymentRequired      bool     `json:"payment_required"`
	IdenticalAgentID     *string  `json:"identical_agent_id"`
	IdenticalAgentStatus *string  `json:"identical_agent_status"`
	RetryAt              *string  `json:"retry_at"`
	AdmissionToken       *string  `json:"admission_token"`
	AdmissionExpiresAt   *string  `json:"admission_expires_at"`
	CooldownSeconds      *int32   `json:"cooldown_seconds"`
	PaymentAmountRao     *int64   `json:"payment_amount_rao"`
	PaymentSendAddress   *string  `json:"payment_send_address"`
}

func pydanticTime(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05") + "Z"
	}
	return t.Format("2006-01-02T15:04:05.000000") + "Z"
}

func pythonISOTime(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05") + "+00:00"
	}
	return t.Format("2006-01-02T15:04:05.000000") + "+00:00"
}

func pgUUID(value uuid.UUID) pgtype.UUID {
	return pgtype.UUID{Bytes: value, Valid: true}
}

func uuidString(value pgtype.UUID) string {
	return uuid.UUID(value.Bytes).String()
}

type admission struct {
	token       string
	expiresAt   time.Time
	feeAmount   int64
	sendAddress string
}

type cooldownError struct{ retryAt time.Time }

func (e *cooldownError) Error() string {
	return "submission cooldown active until " + e.retryAt.String()
}

func retryAt(ctx context.Context, q *postgres.Queries, coldkey string, cooldown int32, now time.Time) (*time.Time, error) {
	latest, err := q.GetSubmissionLatestPaidCreatedAt(ctx, coldkey)
	if err != nil {
		return nil, err
	}
	if !latest.Valid {
		return nil, nil
	}
	retry := latest.Time.UTC().Add(time.Duration(cooldown) * time.Second)
	if retry.After(now) {
		return &retry, nil
	}
	return nil, nil
}

func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}

func (d *Deps) reserve(ctx context.Context, coldkey, hotkey, sha string, now time.Time) (*admission, settings, error) {
	tx, err := d.Pool.Begin(ctx)
	if err != nil {
		return nil, settings{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	q := d.Queries.WithTx(tx)
	// Match Python's transaction boundary: bind the reservation to the latest
	// append-only settings revision inside the transaction that writes it.
	current, err := d.effectiveSettings(ctx, q)
	if err != nil {
		return nil, settings{}, err
	}
	if err := q.LockUploadAdmissionColdkey(ctx, coldkey); err != nil {
		return nil, current, err
	}
	existing, err := q.GetUploadAdmissionForColdkey(ctx, coldkey)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, current, err
	}
	hasExisting := err == nil
	if hasExisting && !existing.ExpiresAt.Time.After(now) {
		if err := q.DeleteUploadAdmission(ctx, coldkey); err != nil {
			return nil, current, err
		}
		hasExisting = false
	}
	if hasExisting {
		address := current.paymentAddress
		if existing.PaymentSendAddress.Valid {
			address = existing.PaymentSendAddress.String
		}
		if existing.MinerHotkey == hotkey && existing.Sha256 == sha {
			if err := tx.Commit(ctx); err != nil {
				return nil, current, err
			}
			return &admission{token: uuidString(existing.Token), expiresAt: existing.ExpiresAt.Time, feeAmount: existing.FeeAmountRao, sendAddress: address}, current, nil
		}
		blockedUntil := minTime(existing.ExpiresAt.Time.UTC(), existing.CreatedAt.Time.UTC().Add(admissionBlockTTL))
		if blockedUntil.After(now) {
			return nil, current, &cooldownError{retryAt: blockedUntil}
		}
		if err := q.DeleteUploadAdmission(ctx, coldkey); err != nil {
			return nil, current, err
		}
	}
	blocked, err := retryAt(ctx, q, coldkey, current.cooldownSeconds, now)
	if err != nil {
		return nil, current, err
	}
	if blocked != nil {
		return nil, current, &cooldownError{retryAt: *blocked}
	}
	token := uuid.New()
	expires := now.Add(admissionTTL)
	row, err := q.InsertUploadAdmission(ctx, postgres.InsertUploadAdmissionParams{
		MinerColdkey: coldkey, Token: pgUUID(token), MinerHotkey: hotkey, Sha256: sha,
		SettingsRevision: current.revision, CooldownSeconds: current.cooldownSeconds,
		FeeAmountRao: current.feeAmountRao, PaymentSendAddress: pgtype.Text{String: current.paymentAddress, Valid: true},
		CreatedAt: pgtype.Timestamptz{Time: now, Valid: true}, ExpiresAt: pgtype.Timestamptz{Time: expires, Valid: true},
	})
	if err != nil {
		return nil, current, err
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, current, err
	}
	return &admission{token: uuidString(row.Token), expiresAt: row.ExpiresAt.Time, feeAmount: row.FeeAmountRao, sendAddress: row.PaymentSendAddress.String}, current, nil
}

func appendFailure(codes *[]int, messages *[]string, code int, message string) {
	*codes = append(*codes, code)
	*messages = append(*messages, message)
}

func (d *Deps) handleCheck(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(io.LimitReader(r.Body, (1<<20)+1))
	if err != nil {
		relayhttp.WriteValidationError(w, r)
		return
	}
	if len(body) > 1<<20 {
		relayhttp.WriteValidationError(w, r)
		return
	}
	payload, valid, recovery := parseCheckRequest(body)
	if !valid {
		relayhttp.WriteValidationError(w, r)
		return
	}
	if recovery {
		if d.Legacy == nil {
			relayhttp.WriteHTTPError(w, r, http.StatusServiceUnavailable, "upload payment recovery unavailable; retry shortly", nil)
			return
		}
		r.Body = io.NopCloser(bytes.NewReader(body))
		r.ContentLength = int64(len(body))
		d.Legacy.ServeHTTP(w, r)
		return
	}

	codes := []int{}
	messages := []string{}
	signatureValid := verifySr25519(*payload.Hotkey, []byte(*payload.Hotkey+":"+*payload.SHA256), *payload.Signature)
	if !signatureValid {
		appendFailure(&codes, &messages, errorBadSignature, "signature did not verify against the hotkey")
	}
	ownerColdkey, err := d.Registration.RegisteredColdkey(r.Context(), *payload.Hotkey)
	if err != nil {
		d.Logger.Warn("chain unreachable during upload check", slog.String("error", err.Error()))
		relayhttp.WriteHTTPError(w, r, http.StatusServiceUnavailable, "chain unavailable; retry shortly", nil)
		return
	}
	registered := ownerColdkey != ""
	if !registered {
		appendFailure(&codes, &messages, errorHotkeyUnregistered, "hotkey is not registered on netuid "+itoa(d.Cfg.Chain.Netuid))
	}
	if *payload.FileSizeBytes > d.Cfg.Upload.MaxTarballSizeBytes {
		appendFailure(&codes, &messages, errorTarballTooLarge, "tarball exceeds "+itoa64(d.Cfg.Upload.MaxTarballSizeBytes)+" bytes")
	}
	banned, err := d.Queries.IsHotkeyBanned(r.Context(), *payload.Hotkey)
	if err != nil {
		d.dbError(w, r, "ban lookup", err)
		return
	}
	if banned {
		appendFailure(&codes, &messages, errorHotkeyBanned, "hotkey is banned from submitting")
	}

	var identicalID, identicalStatus *string
	duplicate := false
	if signatureValid && registered && !banned && !payload.AllowIdenticalRescore {
		row, qerr := d.Queries.GetSameHotkeyAgentBySHA(r.Context(), postgres.GetSameHotkeyAgentBySHAParams{MinerHotkey: *payload.Hotkey, Sha256: *payload.SHA256})
		if qerr == nil {
			duplicate = true
			id, status := uuidString(row.AgentID), row.Status
			identicalID, identicalStatus = &id, &status
			appendFailure(&codes, &messages, errorIdentical, "identical artifact already submitted; no payment is required. Set allow_identical_rescore=true only to purchase another seed.")
		} else if !errors.Is(qerr, pgx.ErrNoRows) {
			d.dbError(w, r, "duplicate lookup", qerr)
			return
		}
	}
	current, err := d.effectiveSettings(r.Context(), d.Queries)
	if err != nil {
		d.dbError(w, r, "settings lookup", err)
		return
	}
	now := d.now()
	var retryString *string
	if !duplicate && registered {
		blocked, qerr := retryAt(r.Context(), d.Queries, ownerColdkey, current.cooldownSeconds, now)
		if qerr != nil {
			d.dbError(w, r, "cooldown lookup", qerr)
			return
		}
		if blocked != nil {
			wireValue := pydanticTime(*blocked)
			retryString = &wireValue
			appendFailure(&codes, &messages, errorCooldown, "owner coldkey may submit again at "+pythonISOTime(*blocked))
		}
	}

	var reserved *admission
	if len(codes) == 0 && payload.ReserveSubmissionSlot {
		var boundSettings settings
		reserved, boundSettings, err = d.reserve(r.Context(), ownerColdkey, *payload.Hotkey, *payload.SHA256, now)
		// reserve resolves settings before every response-producing path. On an
		// earlier infrastructure error the handler returns below, so assignment is
		// unconditional and does not encode that invariant as a magic sentinel.
		current = boundSettings
		if err != nil {
			var cooldown *cooldownError
			if errors.As(err, &cooldown) {
				wireValue := pydanticTime(cooldown.retryAt)
				retryString = &wireValue
				appendFailure(&codes, &messages, errorCooldown, "owner coldkey may submit again at "+pythonISOTime(cooldown.retryAt))
			} else {
				d.dbError(w, r, "admission reservation", err)
				return
			}
		}
	}

	response := checkResponse{
		OK: len(codes) == 0, ErrorCodes: codes, Messages: messages,
		PaymentRequired: len(codes) == 0, IdenticalAgentID: identicalID,
		IdenticalAgentStatus: identicalStatus, RetryAt: retryString,
		CooldownSeconds: &current.cooldownSeconds,
	}
	if reserved != nil && response.PaymentRequired {
		expires := pydanticTime(reserved.expiresAt)
		response.AdmissionToken = &reserved.token
		response.AdmissionExpiresAt = &expires
		response.PaymentAmountRao = &reserved.feeAmount
		response.PaymentSendAddress = &reserved.sendAddress
	}
	writeJSON(w, http.StatusOK, response)
}

func (d *Deps) dbError(w http.ResponseWriter, r *http.Request, operation string, err error) {
	d.Logger.Error("upload database operation failed", slog.String("operation", operation), slog.String("error", err.Error()))
	relayhttp.WriteInternalError(w, r)
}

func itoa(value int) string     { return fmtInt(int64(value)) }
func itoa64(value int64) string { return fmtInt(value) }
func fmtInt(value int64) string {
	return strconv.FormatInt(value, 10)
}
