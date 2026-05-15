package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/heyditto/ditto-subnet/go/bittensor"
	"github.com/heyditto/ditto-subnet/go/chain"
)

// finaliseAndCommit groups Scores by mechanism, applies the discount
// helper, writes an aggregate report, and (when configured) commits the
// resulting weight vector via chain.Client.PutWeights.
//
// One AggregateWeights file is written per mechanism so dispute/replay
// can correlate a published weight set with the underlying scores.
func finaliseAndCommit(
	ctx context.Context,
	cfg Config,
	allScores []scoreWithMech,
	hkByChallenge map[string]string,
	logf func(string, ...any),
) error {
	mechs := []bittensor.Mechanism{bittensor.MechanismCore, bittensor.MechanismRetrieval}
	for _, mech := range mechs {
		subset := make([]bittensor.Score, 0, len(allScores))
		for _, sm := range allScores {
			if sm.Mechanism != mech {
				continue
			}
			subset = append(subset, sm.Score)
		}
		if len(subset) == 0 {
			continue
		}
		agg, details := bittensor.AggregateWithDiscount(subset, hkByChallenge, mech, bittensor.DiscountOpts{})
		logf("aggregate %s: %d miners, %d scores", mech, len(details), len(subset))

		if cfg.ReportDir != "" {
			out := filepath.Join(cfg.ReportDir, "weights-"+string(mech)+".json")
			if err := writeAggregateReport(out, mech, agg, details); err != nil {
				logf("write %s aggregate failed: %v", mech, err)
			}
		}

		if shouldCommit(cfg) {
			if err := commitWeights(ctx, cfg, agg); err != nil {
				return fmt.Errorf("commit %s weights: %w", mech, err)
			}
			logf("committed %s weights to netuid %d (%d entries)", mech, cfg.Netuid, len(agg.Weights))
		}
	}
	return nil
}

type aggregateReport struct {
	SchemaVersion string                     `json:"schema_version"`
	Mechanism     bittensor.Mechanism        `json:"mechanism"`
	Weights       map[string]float64         `json:"weights"`
	Details       []bittensor.MinerAggregate `json:"details"`
}

func writeAggregateReport(path string, mech bittensor.Mechanism, agg bittensor.AggregateWeights, details []bittensor.MinerAggregate) error {
	rep := aggregateReport{
		SchemaVersion: bittensor.SchemaVersion,
		Mechanism:     mech,
		Weights:       agg.Weights,
		Details:       details,
	}
	buf, err := json.MarshalIndent(rep, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(buf, '\n'), 0o644)
}

func shouldCommit(cfg Config) bool {
	if cfg.DryRun {
		return false
	}
	if cfg.PylonURL == "" {
		return false
	}
	if cfg.PylonIdentityName == "" || cfg.PylonIdentityToken == "" {
		return false
	}
	return true
}

func commitWeights(ctx context.Context, cfg Config, agg bittensor.AggregateWeights) error {
	c, err := chain.New(chain.Config{
		PylonURL:      cfg.PylonURL,
		IdentityName:  cfg.PylonIdentityName,
		IdentityToken: cfg.PylonIdentityToken,
		Netuid:        cfg.Netuid,
	}, chain.DefaultHTTPClient())
	if err != nil {
		return err
	}
	defer c.Close()
	return c.PutWeights(ctx, agg.Weights)
}
