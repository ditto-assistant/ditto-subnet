package privatesurface

import (
	"context"
	"encoding/hex"
	"errors"
	"math"

	"github.com/ditto-assistant/dittobench-datagen/gen"
)

// RecoverCompletedDiagnostics is a trusted-operator, offline finalization path
// for a complete checkpoint produced by ProduceWithDiagnostics. Diagnostics are
// NOT independently signed provider attestations: callers must authenticate and
// pin their owner-only local source. Never expose this as an untrusted upload or
// treat a partial/failed checkpoint as validated. No inference is performed.
func RecoverCompletedDiagnostics(ctx context.Context, base gen.DatasetArtifact, profile Profile, rows []Diagnostic, protected ...[]string) ([]byte, []byte, error) {
	fail := func() ([]byte, []byte, error) {
		return nil, nil, errors.New("private recovery: incomplete or inconsistent trusted checkpoint")
	}
	if _, err := profile.Digest(); err != nil {
		return nil, nil, err
	}
	requests, err := gen.PrivateSurfaceRequests(base, protected...)
	if err != nil {
		return nil, nil, err
	}
	if len(rows) != len(requests) {
		return fail()
	}
	byLocation := map[string]Diagnostic{}
	for _, row := range rows {
		if _, exists := byLocation[row.Location]; exists || row.Error != "" {
			return fail()
		}
		byLocation[row.Location] = row
	}
	results := cachedResults{}
	receipts := make([]SurfaceReceipt, len(requests))
	for i, req := range requests {
		row, ok := byLocation[req.Location]
		r := row.Receipt
		if !ok || row.Before != req.Text || r.LocationSHA256 != digest([]byte(req.Location)) || r.BeforeSHA256 != digest([]byte(row.Before)) || r.AfterSHA256 != digest([]byte(row.After)) || !validCompletion(r.Rewrite) || len(r.Rejected) > maxSurfaceAttempts-1 || len(r.Rejected) != len(r.RejectedReasons) {
			return fail()
		}
		switch r.ValidationMethod {
		case "exact-byte-identity-v1":
			if row.Before != row.After || r.Validation.ID != "" {
				return fail()
			}
		case "independent-model-v1":
			if row.Before == row.After || !validCompletion(r.Validation) || r.Rewrite.RequestSHA256 == r.Validation.RequestSHA256 {
				return fail()
			}
		default:
			return fail()
		}
		results[req.Location] = struct{ before, after string }{row.Before, row.After}
		receipts[i] = r
	}
	return finalize(ctx, base, profile, results, receipts, protected...)
}

func validCompletion(r CompletionReceipt) bool {
	validSHA := func(s string) bool { b, e := hex.DecodeString(s); return e == nil && len(b) == 32 }
	return r.ID != "" && len(r.ID) <= 256 && r.Model != "" && len(r.Model) <= 256 && r.Provider != "" && len(r.Provider) <= 256 && validSHA(r.RequestSHA256) && validSHA(r.ResponseSHA256) && r.PromptTokens >= 0 && r.OutputTokens >= 0 && r.CostUSD >= 0 && !math.IsNaN(r.CostUSD) && !math.IsInf(r.CostUSD, 0)
}
