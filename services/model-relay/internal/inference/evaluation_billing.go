package inference

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/ditto-assistant/model-relay/internal/config"
	"github.com/jackc/pgx/v5/pgtype"
)

type evaluationBill struct {
	cfg               config.DittoRouterConfig
	client            *http.Client
	uid, id, provider string
}

func evaluationPost(ctx context.Context, client *http.Client, cfg config.DittoRouterConfig, uid, path string, payload any, out any) *httpError {
	body, err := json.Marshal(payload)
	if err != nil {
		return httpErrorf(503, "evaluation billing unavailable")
	}
	bounded, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(bounded, http.MethodPost, cfg.EvaluationBillingURL+path, bytes.NewReader(body))
	if err != nil {
		return httpErrorf(503, "evaluation billing unavailable")
	}
	req.Header.Set("Authorization", "Bearer "+cfg.APIKey)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Ditto-On-Behalf-Of", uid)
	req.Header.Set("X-Ditto-Require-User-Billing", "true")
	safeClient := *client
	safeClient.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	rsp, err := safeClient.Do(req)
	if err != nil {
		return httpErrorf(503, "evaluation billing unavailable")
	}
	defer rsp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(rsp.Body, 65537))
	if err != nil || len(raw) > 65536 {
		return httpErrorf(503, "evaluation billing unavailable")
	}
	if rsp.StatusCode != 200 {
		status := 503
		if rsp.StatusCode == 402 || rsp.StatusCode == 409 {
			status = rsp.StatusCode
		}
		return httpErrorf(status, "Approve the evaluation budget in Ditto and check available credits")
	}
	if out != nil && json.Unmarshal(raw, out) != nil {
		return httpErrorf(503, "evaluation billing unavailable")
	}
	return nil
}

// Every identity comes from the verified grant/link. Caller-supplied billing
// headers are ignored. Billing-only requests cannot dispatch Router inference.
func (d *Deps) reserveEvaluation(ctx context.Context, agentID pgtype.UUID, h *proxyHeaders, stage, model, provider string, body []byte) (*evaluationBill, *httpError) {
	cfg := d.Cfg.DittoRouter
	if !cfg.EvaluationBillingEnabled {
		return nil, nil
	}
	link, err := d.Queries.GetActiveMinerDittoLinkForAgent(ctx, agentID)
	if err != nil || link.DittoUserID == "" {
		return nil, httpErrorf(402, "Sign in with Ditto and link your miner account before evaluation")
	}
	var runID, payer string
	err = d.Pool.QueryRow(ctx, `SELECT p.run_id::text,p.payer_uid FROM paid_evaluation_grant_bindings b JOIN paid_evaluation_plans p ON p.run_id=b.run_id WHERE b.grant_id=$1 AND p.agent_id=$2 AND p.phase='competition' AND p.state='approved' AND p.expires_at>now()`, h.grant.String(), agentID).Scan(&runID, &payer)
	if err != nil || payer != link.DittoUserID {
		return nil, httpErrorf(402, "This evaluation needs its approved spending budget")
	}
	sum := sha256.Sum256(body)
	payload := map[string]any{"runId": runID, "id": h.nonce.String(), "grantId": h.grant.String(), "stage": stage, "model": model, "provider": provider, "requestSha256": hex.EncodeToString(sum[:]), "maxCostMicrousd": cfg.EvaluationRequestMicrousd}
	if e := evaluationPost(ctx, d.Upstream, cfg, link.DittoUserID, "/reservations", payload, nil); e != nil {
		return nil, e
	}
	return &evaluationBill{cfg: cfg, client: d.Upstream, uid: link.DittoUserID, id: h.nonce.String(), provider: provider}, nil
}
func (b *evaluationBill) finish(parent context.Context, o *settleOutcome, raw []byte) error {
	if b == nil {
		return nil
	}
	// Detached bounded settlement also runs after a canceled broker request.
	ctx, cancel := context.WithTimeout(context.WithoutCancel(parent), 20*time.Second)
	defer cancel()
	state := "unknown"
	receipt := ""
	if o.usageAvailable && len(raw) > 0 {
		var decoded map[string]any
		if json.Unmarshal(raw, &decoded) == nil {
			receipt, _ = decoded["id"].(string)
		}
		if receipt == "" {
			sum := sha256.Sum256(raw)
			receipt = "usage:" + b.id + ":" + hex.EncodeToString(sum[:])
		}
		state = "settled"
	}
	payload := map[string]any{"state": state}
	if state == "settled" {
		payload["actualMicrousd"] = o.costMicrousd
		if o.upstreamProvider.Valid {
			payload["provider"] = o.upstreamProvider.String
		}
		payload["providerReceiptId"] = b.provider + ":" + receipt
		payload["inputTokens"] = o.promptTokens
		payload["outputTokens"] = o.completionTokens
	}
	if e := evaluationPost(ctx, b.client, b.cfg, b.uid, "/operations/"+b.id+"/settle", payload, nil); e != nil {
		return fmt.Errorf("evaluation settlement unavailable; hold retained")
	}
	return nil
}
