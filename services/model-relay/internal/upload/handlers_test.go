package upload

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	schnorrkel "github.com/ChainSafe/go-schnorrkel"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/mr-tron/base58"
	"golang.org/x/crypto/blake2b"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/ditto-assistant/model-relay/internal/postgres"
	"github.com/ditto-assistant/model-relay/internal/relayhttp"
	"github.com/ditto-assistant/model-relay/internal/testutil"
)

const fixtureAddress = "5NotARea1SS58AddressTestFixtureDoNotSendTaoHere"

type stubRegistration struct {
	coldkey string
	err     error
}

func (s stubRegistration) RegisteredColdkey(context.Context, string) (string, error) {
	return s.coldkey, s.err
}

func testLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func validCheckJSON(extra string) string {
	if extra != "" {
		extra = "," + extra
	}
	return `{"hotkey":"` + fixtureAddress + `","sha256":"` + strings.Repeat("a", 64) +
		`","file_size_bytes":1,"signature":"` + strings.Repeat("ab", 64) + `"` + extra + `}`
}

func testSS58(pub [32]byte) string {
	data := append([]byte{42}, pub[:]...)
	hasher, _ := blake2b.New512(nil)
	_, _ = hasher.Write([]byte("SS58PRE"))
	_, _ = hasher.Write(data)
	sum := hasher.Sum(nil)
	return base58.Encode(append(data, sum[0], sum[1]))
}

func signedCheckJSON(t *testing.T, secret *schnorrkel.SecretKey, hotkey, sha string, extra map[string]any) []byte {
	t.Helper()
	signature, err := secret.Sign(schnorrkel.NewSigningContext([]byte("substrate"), []byte(hotkey+":"+sha)))
	if err != nil {
		t.Fatal(err)
	}
	encodedSignature := signature.Encode()
	body := map[string]any{
		"hotkey": hotkey, "sha256": sha, "file_size_bytes": 1024,
		"signature": hex.EncodeToString(encodedSignature[:]),
	}
	for key, value := range extra {
		body[key] = value
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	return encoded
}

func newSignedHotkey(t *testing.T) (*schnorrkel.SecretKey, string) {
	t.Helper()
	secret, public, err := schnorrkel.GenerateKeypair()
	if err != nil {
		t.Fatal(err)
	}
	return secret, testSS58(public.Encode())
}

func serveCheck(t *testing.T, d *Deps, body []byte) checkResponse {
	t.Helper()
	r := httptest.NewRequest(http.MethodPost, "/api/v1/upload/check", strings.NewReader(string(body)))
	w := httptest.NewRecorder()
	d.handleCheck(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	var got checkResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	return got
}

func uploadDeps(pool *pgxpool.Pool, coldkey string, now time.Time) *Deps {
	return &Deps{
		Cfg: &config.Config{Chain: config.ChainConfig{Netuid: 118}, Upload: config.UploadConfig{
			PaymentAddress: fixtureAddress, MaxTarballSizeBytes: 20 * 1024 * 1024,
		}},
		Logger: testLogger(), Pool: pool, Queries: postgres.New(pool),
		Registration: stubRegistration{coldkey: coldkey}, Now: func() time.Time { return now },
	}
}

func insertPaidAgent(t *testing.T, pool *pgxpool.Pool, hotkey, coldkey, sha string, createdAt time.Time) uuid.UUID {
	t.Helper()
	agentID := uuid.New()
	if _, err := pool.Exec(t.Context(), `
		INSERT INTO agents (agent_id, miner_hotkey, name, sha256, version, created_at)
		VALUES ($1, $2, 'upload parity fixture', $3, 1, $4)`, agentID, hotkey, sha, createdAt); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(t.Context(), `
		INSERT INTO evaluation_payments (
			block_hash, extrinsic_index, agent_id, miner_hotkey, miner_coldkey,
			amount_rao, dest_address, "timestamp", accepted_under_legacy_fee_amnesty
		) VALUES ($1, 0, $2, $3, $4, 40000000, $5, $6, false)`,
		"0x"+strings.Repeat("1", 64), agentID, hotkey, coldkey, fixtureAddress, createdAt); err != nil {
		t.Fatal(err)
	}
	return agentID
}

func TestPaymentRecoveryForwardsToPythonUnchanged(t *testing.T) {
	want := validCheckJSON(`"payment_block_hash":"0x` + strings.Repeat("1", 64) + `","payment_block_number":7,"payment_extrinsic_index":2`)
	called := false
	legacy := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called = true
		got, _ := io.ReadAll(r.Body)
		if string(got) != want {
			t.Fatalf("forwarded body changed:\nwant %s\n got %s", want, got)
		}
		w.WriteHeader(http.StatusTeapot)
	})
	d := &Deps{Legacy: legacy}
	r := httptest.NewRequest(http.MethodPost, "/api/v1/upload/check", strings.NewReader(want))
	w := httptest.NewRecorder()
	d.handleCheck(w, r)
	if !called || w.Code != http.StatusTeapot {
		t.Fatalf("legacy called=%v status=%d", called, w.Code)
	}
}

func TestCheckValidationRejectsPartialPaymentProof(t *testing.T) {
	d := &Deps{}
	r := httptest.NewRequest(http.MethodPost, "/api/v1/upload/check", strings.NewReader(validCheckJSON(`"payment_block_number":7`)))
	w := httptest.NewRecorder()
	relayhttp.RequestIDMiddleware(testLogger(), http.HandlerFunc(d.handleCheck)).ServeHTTP(w, r)
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	var envelope relayhttp.ErrorEnvelope
	if err := json.Unmarshal(w.Body.Bytes(), &envelope); err != nil || envelope.ErrorCode != relayhttp.CodeRequestValidation {
		t.Fatalf("envelope=%+v err=%v", envelope, err)
	}
}

func TestEvalPricingWireShape(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	d := uploadDeps(pool, "", time.Time{})
	r := httptest.NewRequest(http.MethodGet, "/api/v1/upload/eval-pricing", nil)
	w := httptest.NewRecorder()
	d.handleEvalPricing(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(w.Body.Bytes(), &fields); err != nil {
		t.Fatal(err)
	}
	if len(fields) != 2 {
		t.Fatalf("fields=%v", fields)
	}
	var amount int64
	if err := json.Unmarshal(fields["amount_rao"], &amount); err != nil || amount != defaultFeeAmountRao {
		t.Fatalf("amount_rao=%d err=%v", amount, err)
	}
	var address string
	if err := json.Unmarshal(fields["send_address"], &address); err != nil || address != fixtureAddress {
		t.Fatalf("send_address=%q err=%v", address, err)
	}
}

func TestCheckAggregatesCheapFailuresAgainstRealSchema(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	d := &Deps{
		Cfg: &config.Config{Chain: config.ChainConfig{Netuid: 118}, Upload: config.UploadConfig{
			PaymentAddress: fixtureAddress, MaxTarballSizeBytes: 20,
		}},
		Logger: testLogger(), Pool: pool, Queries: postgres.New(pool),
		Registration: stubRegistration{},
	}
	body := strings.Replace(validCheckJSON(""), `"file_size_bytes":1`, `"file_size_bytes":21`, 1)
	r := httptest.NewRequest(http.MethodPost, "/api/v1/upload/check", strings.NewReader(body))
	w := httptest.NewRecorder()
	d.handleCheck(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	var got checkResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	want := []int{errorBadSignature, errorHotkeyUnregistered, errorTarballTooLarge}
	if len(got.ErrorCodes) != len(want) {
		t.Fatalf("codes=%v", got.ErrorCodes)
	}
	for i := range want {
		if got.ErrorCodes[i] != want[i] {
			t.Fatalf("codes=%v want=%v", got.ErrorCodes, want)
		}
	}
	if got.OK || got.PaymentRequired || got.CooldownSeconds == nil || *got.CooldownSeconds != defaultCooldownSeconds {
		t.Fatalf("response=%+v", got)
	}
}

func TestValidCheckReservesBoundPaymentInstructions(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	secret, public, err := schnorrkel.GenerateKeypair()
	if err != nil {
		t.Fatal(err)
	}
	hotkey := testSS58(public.Encode())
	sha := strings.Repeat("c", 64)
	signature, err := secret.Sign(schnorrkel.NewSigningContext([]byte("substrate"), []byte(hotkey+":"+sha)))
	if err != nil {
		t.Fatal(err)
	}
	encodedSignature := signature.Encode()
	body, _ := json.Marshal(map[string]any{
		"hotkey": hotkey, "sha256": sha, "file_size_bytes": 1024,
		"signature": hex.EncodeToString(encodedSignature[:]), "reserve_submission_slot": true,
	})
	now := time.Date(2026, 8, 14, 14, 0, 0, 0, time.UTC)
	d := &Deps{
		Cfg: &config.Config{Chain: config.ChainConfig{Netuid: 118}, Upload: config.UploadConfig{
			PaymentAddress: fixtureAddress, MaxTarballSizeBytes: 20 * 1024 * 1024,
		}},
		Logger: testLogger(), Pool: pool, Queries: postgres.New(pool),
		Registration: stubRegistration{coldkey: "5Coldkey"}, Now: func() time.Time { return now },
	}
	r := httptest.NewRequest(http.MethodPost, "/api/v1/upload/check", strings.NewReader(string(body)))
	w := httptest.NewRecorder()
	d.handleCheck(w, r)
	if w.Code != http.StatusOK {
		t.Fatalf("status=%d body=%s", w.Code, w.Body.String())
	}
	var got checkResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if !got.OK || !got.PaymentRequired || got.AdmissionToken == nil || got.PaymentAmountRao == nil || *got.PaymentAmountRao != defaultFeeAmountRao {
		t.Fatalf("response=%+v body=%s", got, w.Body.String())
	}
	if got.PaymentSendAddress == nil || *got.PaymentSendAddress != fixtureAddress || got.AdmissionExpiresAt == nil || *got.AdmissionExpiresAt != "2026-08-15T14:00:00Z" {
		t.Fatalf("payment instructions not bound: %+v", got)
	}
}

func TestCheckReturnsBannedHotkeyParity(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	secret, hotkey := newSignedHotkey(t)
	if _, err := pool.Exec(t.Context(), `INSERT INTO banned_hotkeys (hotkey, reason) VALUES ($1, 'upload parity test')`, hotkey); err != nil {
		t.Fatal(err)
	}
	d := uploadDeps(pool, "5BannedColdkey", time.Date(2026, 8, 14, 14, 0, 0, 0, time.UTC))
	got := serveCheck(t, d, signedCheckJSON(t, secret, hotkey, strings.Repeat("b", 64), nil))
	if got.OK || got.PaymentRequired || len(got.ErrorCodes) != 1 || got.ErrorCodes[0] != errorHotkeyBanned {
		t.Fatalf("response=%+v", got)
	}
}

func TestCheckReturnsIdenticalArtifactParity(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	secret, hotkey := newSignedHotkey(t)
	sha := strings.Repeat("d", 64)
	createdAt := time.Date(2026, 8, 14, 12, 0, 0, 0, time.UTC)
	agentID := insertPaidAgent(t, pool, hotkey, "5DuplicateColdkey", sha, createdAt)
	d := uploadDeps(pool, "5DuplicateColdkey", createdAt.Add(30*time.Minute))
	got := serveCheck(t, d, signedCheckJSON(t, secret, hotkey, sha, nil))
	if got.OK || got.PaymentRequired || len(got.ErrorCodes) != 1 || got.ErrorCodes[0] != errorIdentical {
		t.Fatalf("response=%+v", got)
	}
	if got.IdenticalAgentID == nil || *got.IdenticalAgentID != agentID.String() || got.IdenticalAgentStatus == nil || *got.IdenticalAgentStatus != "uploaded" {
		t.Fatalf("identical fields=%+v", got)
	}
	if got.RetryAt != nil {
		t.Fatalf("duplicate should not also return cooldown: %+v", got)
	}
}

func TestCheckReturnsCooldownWireParityWithoutReservation(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	secret, hotkey := newSignedHotkey(t)
	coldkey := "5CooldownColdkey"
	createdAt := time.Date(2026, 8, 14, 13, 30, 0, 0, time.UTC)
	insertPaidAgent(t, pool, hotkey, coldkey, strings.Repeat("e", 64), createdAt)
	d := uploadDeps(pool, coldkey, time.Date(2026, 8, 14, 14, 0, 0, 0, time.UTC))
	got := serveCheck(t, d, signedCheckJSON(t, secret, hotkey, strings.Repeat("f", 64), nil))
	if got.OK || got.PaymentRequired || len(got.ErrorCodes) != 1 || got.ErrorCodes[0] != errorCooldown {
		t.Fatalf("response=%+v", got)
	}
	if got.RetryAt == nil || *got.RetryAt != "2026-08-14T14:30:00Z" {
		t.Fatalf("retry_at=%v", got.RetryAt)
	}
	if len(got.Messages) != 1 || got.Messages[0] != "owner coldkey may submit again at 2026-08-14T14:30:00+00:00" {
		t.Fatalf("messages=%v", got.Messages)
	}
}

func TestReusedReservationReturnsCurrentCooldownAndBoundPayment(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	secret, hotkey := newSignedHotkey(t)
	coldkey := "5ReusableColdkey"
	sha := strings.Repeat("7", 64)
	now := time.Date(2026, 8, 14, 14, 0, 0, 0, time.UTC)
	d := uploadDeps(pool, coldkey, now)
	var firstRevision int32
	if err := pool.QueryRow(t.Context(), `
		INSERT INTO submission_settings_revisions
		(parent_revision, cooldown_seconds, fee_amount_rao, reason, actor)
		VALUES (0, 1800, 40000000, 'initial upload parity settings', 'go-test')
		RETURNING revision`).Scan(&firstRevision); err != nil {
		t.Fatal(err)
	}
	first, _, err := d.reserve(t.Context(), coldkey, hotkey, sha, now)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(t.Context(), `
		INSERT INTO submission_settings_revisions
		(parent_revision, cooldown_seconds, fee_amount_rao, reason, actor)
		VALUES ($1, 3600, 50000000, 'updated upload parity settings', 'go-test')`, firstRevision); err != nil {
		t.Fatal(err)
	}
	got := serveCheck(t, d, signedCheckJSON(t, secret, hotkey, sha, map[string]any{"reserve_submission_slot": true}))
	if !got.OK || !got.PaymentRequired || got.AdmissionToken == nil || *got.AdmissionToken != first.token {
		t.Fatalf("response=%+v", got)
	}
	if got.CooldownSeconds == nil || *got.CooldownSeconds != 3600 {
		t.Fatalf("response used stale bound cooldown: %+v", got)
	}
	if got.PaymentAmountRao == nil || *got.PaymentAmountRao != 40_000_000 {
		t.Fatalf("response did not preserve bound payment: %+v", got)
	}
}

func TestReservationIsAtomicReusableAndBlocksCompetingArchive(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	q := postgres.New(pool)
	now := time.Date(2026, 8, 14, 12, 0, 0, 0, time.UTC)
	d := &Deps{Pool: pool, Queries: q, Cfg: &config.Config{Upload: config.UploadConfig{PaymentAddress: fixtureAddress}}}
	if _, err := pool.Exec(t.Context(), `
		INSERT INTO submission_settings_revisions
		(parent_revision, cooldown_seconds, fee_amount_rao, reason, actor)
		VALUES (0, 1800, 40000000, 'upload admission test', 'go-test')`); err != nil {
		t.Fatal(err)
	}
	first, _, err := d.reserve(t.Context(), "5Coldkey", fixtureAddress, strings.Repeat("a", 64), now)
	if err != nil {
		t.Fatalf("first reserve: %v", err)
	}
	again, _, err := d.reserve(t.Context(), "5Coldkey", fixtureAddress, strings.Repeat("a", 64), now.Add(time.Minute))
	if err != nil {
		t.Fatalf("exact reserve: %v", err)
	}
	if first.token != again.token || !first.expiresAt.Equal(again.expiresAt) {
		t.Fatalf("exact retry changed admission: first=%+v again=%+v", first, again)
	}
	if first.feeAmount != 40_000_000 {
		t.Fatalf("reservation did not bind live settings: %+v", first)
	}
	_, _, err = d.reserve(t.Context(), "5Coldkey", fixtureAddress, strings.Repeat("b", 64), now.Add(2*time.Minute))
	var blocked *cooldownError
	if !errors.As(err, &blocked) {
		t.Fatalf("competing archive error=%v", err)
	}
	if !blocked.retryAt.Equal(now.Add(admissionBlockTTL)) {
		t.Fatalf("retry_at=%s", blocked.retryAt)
	}
	row, err := q.GetUploadAdmissionForColdkey(t.Context(), "5Coldkey")
	if err != nil && err != pgx.ErrNoRows {
		t.Fatal(err)
	}
	if uuidString(row.Token) != first.token {
		t.Fatal("competing reservation replaced the original")
	}
	if row.CooldownSeconds != 1800 {
		t.Fatalf("reservation did not bind live cooldown: %+v", row)
	}
}

func TestConcurrentReservationsSerializeFirstSubmission(t *testing.T) {
	pool := testutil.NewTestPGPool(t)
	d := &Deps{
		Pool: pool, Queries: postgres.New(pool),
		Cfg: &config.Config{Upload: config.UploadConfig{PaymentAddress: fixtureAddress}},
	}
	start := make(chan struct{})
	results := make(chan error, 2)
	for _, sha := range []string{strings.Repeat("d", 64), strings.Repeat("e", 64)} {
		go func(artifactSHA string) {
			<-start
			_, _, err := d.reserve(t.Context(), "5ConcurrentColdkey", fixtureAddress, artifactSHA, time.Now().UTC())
			results <- err
		}(sha)
	}
	close(start)
	succeeded, blocked := 0, 0
	for range 2 {
		err := <-results
		if err == nil {
			succeeded++
			continue
		}
		var cooldown *cooldownError
		if errors.As(err, &cooldown) {
			blocked++
			continue
		}
		t.Fatalf("unexpected reservation error: %v", err)
	}
	if succeeded != 1 || blocked != 1 {
		t.Fatalf("succeeded=%d blocked=%d", succeeded, blocked)
	}
}
