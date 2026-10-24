package challengerun

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/runner"
	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/protocol"
)

const testHotkey = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"

func init() { runner.Configure(true) }

// fakeHarness answers each question through a policy the test supplies and
// records what crossed the wire.
type fakeHarness struct {
	mu       sync.Mutex
	seeded   []protocol.SeedRequest
	runs     []protocol.RunRequest
	seedFail bool
	answer   func(question string) (string, int)
}

func (f *fakeHarness) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	switch r.URL.Path {
	case "/health":
		w.WriteHeader(http.StatusOK)
	case "/seed":
		var req protocol.SeedRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		f.mu.Lock()
		f.seeded = append(f.seeded, req)
		f.mu.Unlock()
		if f.seedFail {
			http.Error(w, "boom", http.StatusInternalServerError)
			return
		}
		_ = json.NewEncoder(w).Encode(protocol.SeedResponse{Pairs: len(req.Pairs)})
	case "/run":
		var req protocol.RunRequest
		_ = json.NewDecoder(r.Body).Decode(&req)
		f.mu.Lock()
		f.runs = append(f.runs, req)
		seededWaves := len(f.seeded)
		f.mu.Unlock()
		if seededWaves == 0 {
			http.Error(w, "ran before seeding", http.StatusConflict)
			return
		}
		text, status := f.answer(req.UserInput)
		if status != http.StatusOK {
			http.Error(w, "fail", status)
			return
		}
		_ = json.NewEncoder(w).Encode(protocol.RunResponse{Answer: text, FinalText: text})
	default:
		http.NotFound(w, r)
	}
}

func answersByQuestion(p challenge.Pack, pick func(challenge.Case) string) func(string) (string, int) {
	m := map[string]string{}
	for _, c := range p.Cases {
		m[c.Question] = pick(c)
	}
	return func(q string) (string, int) { return m[q], http.StatusOK }
}

func gold(c challenge.Case) string {
	if len(c.AnswerItems) > 0 {
		return strings.Join(c.AnswerItems, " and ")
	}
	return c.ExpectedAnswer
}

func TestExecuteGradesOracleAndDistractorAgents(t *testing.T) {
	p := challenge.ReferencePack(4, testHotkey)
	for _, tc := range []struct {
		name string
		pick func(challenge.Case) string
		want float64
	}{
		{"oracle", gold, 1},
		{"distractor", func(c challenge.Case) string { return c.DistractorAnswers[0] }, 0},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := &fakeHarness{answer: answersByQuestion(p, tc.pick)}
			srv := httptest.NewServer(h)
			defer srv.Close()
			out, err := Execute(context.Background(), srv.URL, p, Options{Nonce: []byte("pinned")})
			if err != nil {
				t.Fatal(err)
			}
			if len(out) != len(p.Cases) {
				t.Fatalf("got %d outcomes for %d cases", len(out), len(p.Cases))
			}
			for i, o := range out {
				if o.CaseID != p.Cases[i].ID || o.Fault || o.Score != tc.want {
					t.Errorf("case %s: %+v want score %v", p.Cases[i].ID, o, tc.want)
				}
			}
			if len(h.seeded) != len(p.MemoryWaves) {
				t.Fatalf("seeded %d waves, want %d", len(h.seeded), len(p.MemoryWaves))
			}
		})
	}
}

func TestExecuteNeverSendsPackIdentities(t *testing.T) {
	p := challenge.ReferencePack(4, testHotkey)
	h := &fakeHarness{answer: answersByQuestion(p, gold)}
	srv := httptest.NewServer(h)
	defer srv.Close()
	if _, err := Execute(context.Background(), srv.URL, p, Options{}); err != nil {
		t.Fatal(err)
	}
	ids := map[string]bool{}
	for _, c := range p.Cases {
		ids[c.ID] = true
	}
	for _, w := range p.MemoryWaves {
		for _, pr := range w.Pairs {
			ids[pr.PairID] = true
			ids[pr.SessionID] = true
		}
	}
	for _, r := range h.runs {
		if ids[r.CaseID] || !strings.HasPrefix(r.CaseID, "c-") {
			t.Fatalf("case id %q is not opaque", r.CaseID)
		}
	}
	users := map[string]bool{}
	for _, w := range h.seeded {
		users[w.UserID] = true
		if !strings.HasPrefix(w.UserID, "u-") {
			t.Fatalf("seeded user %q is not opaque", w.UserID)
		}
	}
	for _, r := range h.runs {
		if !users[r.UserID] {
			t.Fatalf("run user %q was never seeded", r.UserID)
		}
	}
	for _, w := range h.seeded {
		for _, pr := range w.Pairs {
			if ids[pr.PairID] || ids[pr.SessionID] {
				t.Fatalf("pair identity leaked: %+v", pr)
			}
		}
	}
	// Two runs of the same pack use different opaque IDs.
	first := h.runs[0].CaseID
	h.runs, h.seeded = nil, nil
	if _, err := Execute(context.Background(), srv.URL, p, Options{}); err != nil {
		t.Fatal(err)
	}
	for _, r := range h.runs {
		if r.CaseID == first {
			t.Fatal("opaque ids repeated across runs")
		}
	}
}

func TestExecuteFaultsFailedCasesAndAbortsOnSeedFailure(t *testing.T) {
	p := challenge.ReferencePack(4, testHotkey)
	broken := p.Cases[0].Question
	h := &fakeHarness{answer: func(q string) (string, int) {
		if q == broken {
			return "", http.StatusBadGateway
		}
		return "no idea", http.StatusOK
	}}
	srv := httptest.NewServer(h)
	defer srv.Close()
	out, err := Execute(context.Background(), srv.URL, p, Options{})
	if err != nil {
		t.Fatal(err)
	}
	faults := 0
	for _, o := range out {
		if o.Fault {
			faults++
			if o.CaseID != p.Cases[0].ID {
				t.Fatalf("wrong case faulted: %s", o.CaseID)
			}
		}
	}
	if faults != 1 {
		t.Fatalf("faults=%d", faults)
	}

	h2 := &fakeHarness{seedFail: true, answer: answersByQuestion(p, gold)}
	srv2 := httptest.NewServer(h2)
	defer srv2.Close()
	if _, err := Execute(context.Background(), srv2.URL, p, Options{}); !errors.Is(err, ErrHarnessUnavailable) {
		t.Fatalf("seed failure: %v", err)
	}
	if len(h2.runs) != 0 {
		t.Fatal("cases ran after a failed seed")
	}
}

func TestExecuteRejectsInvalidPack(t *testing.T) {
	p := challenge.ReferencePack(4, testHotkey)
	p.Contract = "nope"
	if _, err := Execute(context.Background(), "http://127.0.0.1:1", p, Options{}); err == nil {
		t.Fatal("invalid pack executed")
	}
}
