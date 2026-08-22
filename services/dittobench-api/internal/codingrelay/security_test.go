package codingrelay

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
)

type typedNilUpstream struct{}

func (*typedNilUpstream) Complete(context.Context, UpstreamRequest) (UpstreamResult, error) {
	return UpstreamResult{}, nil
}

func TestRelayConfigurationFailsClosed(t *testing.T) {
	fixture := newRelayFixture(t)
	validUpstream := upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
		return UpstreamResult{}, errors.New("unused")
	})
	tests := map[string]func(*Config){
		"nil upstream": func(value *Config) { value.Upstream = nil },
		"typed nil upstream": func(value *Config) {
			var upstream *typedNilUpstream
			value.Upstream = upstream
		},
		"nil journal":           func(value *Config) { value.Journal = nil },
		"attempt":               func(value *Config) { value.Binding.AttemptID = "" },
		"artifact":              func(value *Config) { value.Binding.AgentArtifactSHA256 = "not-a-digest" },
		"harness":               func(value *Config) { value.Binding.HarnessInstanceID = "bad identity" },
		"ticket alias":          func(value *Config) { value.Binding.TicketID = "{33333333-3333-4333-8333-333333333333}" },
		"grant":                 func(value *Config) { value.Binding.GrantID = "00000000-0000-0000-0000-000000000000" },
		"generation":            func(value *Config) { value.Binding.Generation = 0 },
		"policy digest":         func(value *Config) { value.Binding.InferenceGrantSHA256 = strings.Repeat("f", 64) },
		"deadline before issue": func(value *Config) { value.Binding.Deadline = value.Binding.IssuedAt },
		"future issue":          func(value *Config) { value.Binding.IssuedAt = fixture.clock.Now().Add(time.Second) },
		"lifetime":              func(value *Config) { value.Binding.Deadline = value.Binding.IssuedAt.Add(2*time.Hour + time.Second) },
		"requests":              func(value *Config) { value.Binding.RequestBudget = 0 },
		"prompt":                func(value *Config) { value.Binding.PromptTokenBudget = 0 },
		"completion":            func(value *Config) { value.Binding.CompletionTokenBudget = 0 },
		"operation timeout":     func(value *Config) { value.OperationTimeout = maximumOperationTimeout + time.Second },
		"weight eligible":       func(value *Config) { value.Policy.WeightEligible = true },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			config := fixture.config(validUpstream)
			mutate(&config)
			if _, err := New(t.Context(), config); !errors.Is(err, ErrInvalidConfig) {
				t.Fatalf("config err=%v", err)
			}
		})
	}

	config := fixture.config(validUpstream)
	config.NewRequestID = func() string { return "not-a-uuid" }
	relay, err := New(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("invalid request ID err=%v", err)
	}
}

func TestRelayRejectsDuplicateGeneratedRequestIDBeforeProviderDispatch(t *testing.T) {
	fixture := newRelayFixture(t)
	duplicate := "55555555-5555-4555-8555-555555555555"
	fixture.ids = &requestIDQueue{values: []string{duplicate, duplicate}}
	var calls atomic.Int32
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		index := int(calls.Add(1)) - 1
		return fixture.completeResult(t, request, index), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[0]); err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[1]); !errors.Is(err, ErrInvalidConfig) {
		t.Fatalf("duplicate request ID err=%v", err)
	}
	if calls.Load() != 1 || fixture.journal.begins != 1 {
		t.Fatalf("duplicate ID dispatched: calls=%d begins=%d", calls.Load(), fixture.journal.begins)
	}
}

func TestRelayJournalFailuresNeverDispatchOrReturnUnjournaledResults(t *testing.T) {
	t.Run("load", func(t *testing.T) {
		fixture := newRelayFixture(t)
		fixture.journal.loadErr = errors.New("private load detail")
		upstream := upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
			return UpstreamResult{}, errors.New("unused")
		})
		if _, err := New(t.Context(), fixture.config(upstream)); !errors.Is(err, ErrJournalUnavailable) || strings.Contains(err.Error(), "private") {
			t.Fatalf("load err=%v", err)
		}
	})

	t.Run("begin", func(t *testing.T) {
		fixture := newRelayFixture(t)
		fixture.journal.beginErr = errors.New("private begin detail")
		var calls atomic.Int32
		upstream := upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
			calls.Add(1)
			return UpstreamResult{}, nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrJournalUnavailable) || calls.Load() != 0 || strings.Contains(err.Error(), "private") {
			t.Fatalf("begin err=%v calls=%d", err, calls.Load())
		}
	})

	t.Run("complete", func(t *testing.T) {
		fixture := newRelayFixture(t)
		fixture.journal.completeErr = errors.New("private completion detail")
		upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
			return fixture.completeResult(t, request, 0), nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		if body, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrJournalUnavailable) || body != nil || strings.Contains(err.Error(), "private") {
			t.Fatalf("complete body=%q err=%v", body, err)
		}
		fixture.journal.completeErr = nil
		if _, err := New(t.Context(), fixture.config(upstream)); !errors.Is(err, ErrAmbiguousDispatch) {
			t.Fatalf("ambiguous restart err=%v", err)
		}
	})

	t.Run("revoke retry", func(t *testing.T) {
		fixture := newRelayFixture(t)
		fixture.journal.revokeErr = errors.New("private revoke detail")
		relay, err := New(t.Context(), fixture.config(upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
			return UpstreamResult{}, errors.New("unused")
		})))
		if err != nil {
			t.Fatal(err)
		}
		if err := relay.Revoke(t.Context()); !errors.Is(err, ErrJournalUnavailable) {
			t.Fatalf("revoke err=%v", err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrCapabilityRevoked) {
			t.Fatalf("failed revoke reopened admission: %v", err)
		}
		fixture.journal.revokeErr = nil
		if err := relay.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
	})
}

func TestRelayUnsettledProviderAttemptIsNonRerunnableAndSecretSafe(t *testing.T) {
	fixture := newRelayFixture(t)
	secret := "sk-provider-private-value"
	upstream := upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
		return UpstreamResult{}, errors.New(secret)
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrUpstreamUnsettled) || strings.Contains(err.Error(), secret) {
		t.Fatalf("upstream err=%v", err)
	}
	if len(fixture.journal.Snapshot().Entries) != 1 || fixture.journal.Snapshot().Entries[0].Completed {
		t.Fatal("unsettled provider attempt lost its pre-dispatch marker")
	}
	if _, err := New(t.Context(), fixture.config(upstream)); !errors.Is(err, ErrAmbiguousDispatch) {
		t.Fatalf("unsettled restart err=%v", err)
	}
	if err := relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	adapter, err := NewCertifierEvidenceAdapter(relay)
	if err != nil {
		t.Fatal(err)
	}
	binding := codingcertifierBinding(fixture)
	if _, err := adapter.Evidence(t.Context(), binding); !errors.Is(err, ErrUpstreamUnsettled) ||
		errors.Is(err, codingcertifier.ErrInferenceNotObserved) {
		t.Fatalf("unsettled evidence classification err=%v", err)
	}
}

func TestRelayRejectsForeignOrIncoherentSettlements(t *testing.T) {
	tests := map[string]func(*UpstreamResult){
		"ticket":        func(value *UpstreamResult) { value.Settlement.TicketID = "77777777-7777-4777-8777-777777777777" },
		"case":          func(value *UpstreamResult) { value.Settlement.CaseID = "different" },
		"profile":       func(value *UpstreamResult) { value.Settlement.ProfileCapabilityID = "different" },
		"grant":         func(value *UpstreamResult) { value.Settlement.GrantID = "77777777-7777-4777-8777-777777777777" },
		"generation":    func(value *UpstreamResult) { value.Settlement.Generation++ },
		"request":       func(value *UpstreamResult) { value.Settlement.RequestID = "77777777-7777-4777-8777-777777777777" },
		"sequence":      func(value *UpstreamResult) { value.Settlement.RequestSequence++ },
		"attempt":       func(value *UpstreamResult) { value.Settlement.Attempt++ },
		"locked digest": func(value *UpstreamResult) { value.Settlement.LockedRequestSHA256 = strings.Repeat("f", 64) },
		"fallback":      func(value *UpstreamResult) { value.Settlement.FallbackUsed = true },
		"route":         func(value *UpstreamResult) { value.Settlement.ProviderRoute = "different" },
		"response":      func(value *UpstreamResult) { value.NormalizedResponse = []byte(`{"invalid":true}`) },
		"unexpected body": func(value *UpstreamResult) {
			value.Settlement.Outcome = "provider_failure"
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			fixture := newRelayFixture(t)
			upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
				result := fixture.completeResult(t, request, 0)
				mutate(&result)
				return result, nil
			})
			relay, err := New(t.Context(), fixture.config(upstream))
			if err != nil {
				t.Fatal(err)
			}
			if body, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrUpstreamUnsettled) || body != nil {
				t.Fatalf("body=%q err=%v", body, err)
			}
		})
	}
}

func TestRelayFailureResponseProjectionMustMatchSettlementDigest(t *testing.T) {
	for name, mutate := range map[string]func(*UpstreamResult){
		"missing":                 func(value *UpstreamResult) { value.FailureResponseProjection = nil },
		"tampered":                func(value *UpstreamResult) { value.FailureResponseProjection = []byte(`{"different":true}`) },
		"normalized also present": func(value *UpstreamResult) { value.NormalizedResponse = []byte(`{}`) },
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newRelayFixture(t)
			upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
				result := fixture.invalidResponseResult(t, request)
				mutate(&result)
				return result, nil
			})
			relay, err := New(t.Context(), fixture.config(upstream))
			if err != nil {
				t.Fatal(err)
			}
			if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrUpstreamUnsettled) {
				t.Fatalf("projection err=%v", err)
			}
		})
	}
}

func TestRelayRejectsOversizedUpstreamBytesBeforeProjection(t *testing.T) {
	fixture := newRelayFixture(t)
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		result := fixture.completeResult(t, request, 0)
		result.NormalizedResponse = make([]byte, fixture.policy.MaxResponseBytes+1)
		return result, nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrUpstreamUnsettled) {
		t.Fatalf("oversized upstream err=%v", err)
	}
	if snapshot := fixture.journal.Snapshot(); len(snapshot.Entries) != 1 || snapshot.Entries[0].Completed {
		t.Fatalf("oversized upstream journal=%#v", snapshot)
	}
}

func TestRelayEnforcesEffectiveRequestAndTokenBudgets(t *testing.T) {
	t.Run("completion cap and request budget", func(t *testing.T) {
		fixture := newRelayFixture(t)
		fixture.binding.RequestBudget = 1
		fixture.binding.PromptTokenBudget = 1_000
		fixture.binding.CompletionTokenBudget = 100
		upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
			if request.LockedRequest.MaxCompletionTokens != 100 {
				t.Fatalf("max completion=%d", request.LockedRequest.MaxCompletionTokens)
			}
			return fixture.customCompleteResult(t, request, 500, 50, 100), nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[1]); !errors.Is(err, ErrBudgetExhausted) {
			t.Fatalf("request budget err=%v", err)
		}
	})

	t.Run("exhausted prompt budget blocks pre-dispatch", func(t *testing.T) {
		fixture := newRelayFixture(t)
		fixture.binding.PromptTokenBudget = 500
		var calls atomic.Int32
		upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
			calls.Add(1)
			return fixture.customCompleteResult(t, request, 500, 50, 100), nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[1]); !errors.Is(err, ErrBudgetExhausted) || calls.Load() != 1 {
			t.Fatalf("prompt budget err=%v calls=%d", err, calls.Load())
		}
	})

	for name, configure := range map[string]func(*relayFixture){
		"prompt": func(value *relayFixture) { value.binding.PromptTokenBudget = 499 },
	} {
		t.Run(name, func(t *testing.T) {
			fixture := newRelayFixture(t)
			configure(&fixture)
			upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
				return fixture.customCompleteResult(t, request, 500, 50, 100), nil
			})
			relay, err := New(t.Context(), fixture.config(upstream))
			if err != nil {
				t.Fatal(err)
			}
			if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrBudgetExhausted) {
				t.Fatalf("budget err=%v", err)
			}
			if len(fixture.journal.Snapshot().Entries) != 1 || !fixture.journal.Snapshot().Entries[0].Completed {
				t.Fatal("over-budget provider settlement was not retained")
			}
		})
	}
}

func TestRelayEnforcesAggregatePolicyCostBudget(t *testing.T) {
	fixture := newRelayFixture(t)
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		return fixture.customCompleteResult(t, request, 500, 50, 6_000_000), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[0]); err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[1]); !errors.Is(err, ErrBudgetExhausted) {
		t.Fatalf("aggregate cost err=%v", err)
	}
	if len(fixture.journal.Snapshot().Entries) != 2 {
		t.Fatal("over-cost settlement was not retained")
	}
}

func TestRelayRetryExhaustionCannotBecomeEvidenceOrCleanRestart(t *testing.T) {
	fixture := newRelayFixture(t)
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		return fixture.retryResult(request), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrEvidenceUnavailable) {
		t.Fatalf("retry exhaustion err=%v", err)
	}
	if len(fixture.journal.Snapshot().Entries) != int(fixture.policy.MaxAttemptsPerRequest) {
		t.Fatalf("retry journal=%#v", fixture.journal.Snapshot())
	}
	if _, err := New(t.Context(), fixture.config(upstream)); !errors.Is(err, ErrAmbiguousDispatch) {
		t.Fatalf("retry restart err=%v", err)
	}
}

func TestRelayDeadlineAndClockRollbackFailClosed(t *testing.T) {
	t.Run("post-deadline recovery", func(t *testing.T) {
		fixture := newRelayFixture(t)
		fixture.binding.IssuedAt = fixture.clock.Now().Add(-2 * time.Hour)
		fixture.binding.Deadline = fixture.clock.Now().Add(-time.Hour)
		relay, err := New(t.Context(), fixture.config(upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
			return UpstreamResult{}, errors.New("unused")
		})))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrCapabilityExpired) {
			t.Fatalf("expired recovery admitted request: %v", err)
		}
		if err := relay.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
		if evidence, err := relay.Evidence(t.Context(), fixture.evidenceBinding()); err != nil ||
			evidence.UsageStatus != "not_invoked" {
			t.Fatalf("evidence=%#v err=%v", evidence, err)
		}
	})

	t.Run("deadline", func(t *testing.T) {
		fixture := newRelayFixture(t)
		relay, err := New(t.Context(), fixture.config(upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
			return UpstreamResult{}, errors.New("unused")
		})))
		if err != nil {
			t.Fatal(err)
		}
		fixture.clock.Advance(time.Hour)
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrCapabilityExpired) {
			t.Fatalf("deadline err=%v", err)
		}
	})

	t.Run("rollback", func(t *testing.T) {
		fixture := newRelayFixture(t)
		relay, err := New(t.Context(), fixture.config(upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
			return UpstreamResult{}, errors.New("unused")
		})))
		if err != nil {
			t.Fatal(err)
		}
		fixture.clock.Rewind(time.Second)
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); !errors.Is(err, ErrClockRollback) {
			t.Fatalf("rollback err=%v", err)
		}
	})

	t.Run("rollback does not hide already-revoked evidence", func(t *testing.T) {
		fixture := newRelayFixture(t)
		upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
			return fixture.completeResult(t, request, 0), nil
		})
		relay, err := New(t.Context(), fixture.config(upstream))
		if err != nil {
			t.Fatal(err)
		}
		if _, err := relay.Complete(t.Context(), fixture.requests[0]); err != nil {
			t.Fatal(err)
		}
		if err := relay.Revoke(t.Context()); err != nil {
			t.Fatal(err)
		}
		fixture.clock.Rewind(time.Second)
		if _, err := relay.Evidence(t.Context(), fixture.evidenceBinding()); err != nil {
			t.Fatalf("durable evidence depended on wall clock: %v", err)
		}
	})
}

func TestRelayRecoveryRejectsCorruptOrMutableJournalState(t *testing.T) {
	fixture := newRelayFixture(t)
	upstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		request.LockedRequest.Messages[0][0] = 'x'
		return fixture.completeResult(t, request, 0), nil
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := relay.Complete(t.Context(), fixture.requests[0]); err != nil {
		t.Fatalf("upstream-owned request mutation affected relay state: %v", err)
	}
	snapshot := fixture.journal.Snapshot()
	if snapshot.Entries[0].Dispatch.LockedRequest.Messages[0][0] == 'x' {
		t.Fatal("upstream mutation aliased journal state")
	}

	valid := newRelayFixture(t)
	validUpstream := upstreamFunc(func(_ context.Context, request UpstreamRequest) (UpstreamResult, error) {
		return valid.completeResult(t, request, 0), nil
	})
	validRelay, err := New(t.Context(), valid.config(validUpstream))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := validRelay.Complete(t.Context(), valid.requests[0]); err != nil {
		t.Fatal(err)
	}
	base := valid.journal.Snapshot()
	mutations := map[string]func(*JournalSnapshot){
		"incomplete": func(value *JournalSnapshot) { value.Entries[0].Completed = false },
		"receipt":    func(value *JournalSnapshot) { value.Entries[0].Receipt.TotalTokens++ },
		"settlement": func(value *JournalSnapshot) { value.Entries[0].Settlement.ProviderRoute = "different" },
		"normalized": func(value *JournalSnapshot) { value.Entries[0].NormalizedResponse[0] = 'x' },
		"miner":      func(value *JournalSnapshot) { value.Entries[0].MinerResponse[0] = 'x' },
		"dispatch":   func(value *JournalSnapshot) { value.Entries[0].Dispatch.LockedRequestSHA256 = strings.Repeat("f", 64) },
		"miner request": func(value *JournalSnapshot) {
			value.Entries[0].Dispatch.MinerRequest.MaxCompletionTokens--
		},
	}
	for name, mutate := range mutations {
		t.Run(name, func(t *testing.T) {
			journal := &fakeJournal{snapshot: cloneSnapshot(base)}
			mutate(&journal.snapshot)
			changed := valid
			changed.journal = journal
			if _, err := New(t.Context(), changed.config(validUpstream)); err == nil {
				t.Fatal("corrupt journal was accepted")
			}
		})
	}
}

func TestRelayRecoveryRejectsForeignOrUnboundSnapshots(t *testing.T) {
	fixture := newRelayFixture(t)
	upstream := upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
		return UpstreamResult{}, errors.New("unused")
	})
	relay, err := New(t.Context(), fixture.config(upstream))
	if err != nil {
		t.Fatal(err)
	}
	if err := relay.Revoke(t.Context()); err != nil {
		t.Fatal(err)
	}
	base := fixture.journal.Snapshot()
	if base.Binding == nil || !base.Revoked || len(base.Entries) != 0 {
		t.Fatalf("snapshot=%#v", base)
	}

	foreign := cloneSnapshot(base)
	foreign.Binding.TicketID = "77777777-7777-4777-8777-777777777777"
	journal := &fakeJournal{snapshot: foreign, skipBindingCheck: true}
	changed := fixture
	changed.journal = journal
	if _, err := New(t.Context(), changed.config(upstream)); !errors.Is(err, ErrEvidenceBinding) {
		t.Fatalf("foreign snapshot err=%v", err)
	}

	unbound := cloneSnapshot(base)
	unbound.Binding = nil
	journal = &fakeJournal{snapshot: unbound, skipBindingCheck: true}
	changed.journal = journal
	if _, err := New(t.Context(), changed.config(upstream)); !errors.Is(err, ErrEvidenceBinding) {
		t.Fatalf("unbound snapshot err=%v", err)
	}
}

func TestRelayRecoveryRejectsOversizedJournalCardinality(t *testing.T) {
	fixture := newRelayFixture(t)
	binding := cloneBinding(fixture.binding)
	fixture.journal.snapshot = JournalSnapshot{
		Binding: &binding,
		Entries: make([]JournalEntry, int(fixture.policy.MaxRequests)+int(fixture.policy.MaxRetries)+1),
	}
	upstream := upstreamFunc(func(context.Context, UpstreamRequest) (UpstreamResult, error) {
		return UpstreamResult{}, errors.New("unused")
	})
	if _, err := New(t.Context(), fixture.config(upstream)); !errors.Is(err, ErrEvidenceUnavailable) {
		t.Fatalf("oversized journal err=%v", err)
	}
}
