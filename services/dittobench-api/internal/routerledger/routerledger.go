// Package routerledger is the dittobench-api scorer's in-process accumulator for
// the SN118 router shadow ledger. Every submission's Dispatcher.Run produces one
// shadow LedgerEntry (internal/routerscore); this store keeps the latest entry
// per agent and serves a deterministically ordered snapshot to the publish route,
// which the platform relays to validators (who only read + fold it).
//
// Everything here is SHADOW: entries are never weight-eligible and their folded
// CombinedScore is 0. The store defensively refuses a weight-eligible entry so a
// future bug can never leak a weight-bearing number through the shadow publish
// path. Ordering is by the measured ShadowComposite (the dashboard number) so the
// exposed pool is meaningful even though it carries zero emission weight.
package routerledger

import (
	"sort"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/routerscore"
)

// Store accumulates the latest shadow ledger entry per agent, concurrency-safe.
type Store struct {
	mu          sync.RWMutex
	entries     map[string]routerscore.LedgerEntry
	generatedAt time.Time
}

// New builds an empty store.
func New() *Store {
	return &Store{entries: make(map[string]routerscore.LedgerEntry)}
}

// key selects the stable identity for an entry: agent_id when present, else the
// miner hotkey (shadow entries may carry only an agent id).
func key(entry routerscore.LedgerEntry) string {
	if entry.AgentID != "" {
		return entry.AgentID
	}
	return entry.MinerHotkey
}

// Record upserts a shadow entry. A weight-eligible entry is refused (shadow
// invariant); an entry with no identity is dropped. The earliest non-zero
// FirstSeen is preserved as the lineage tie-break so a later re-score cannot
// steal an original's first-seen precedence.
func (s *Store) Record(entry routerscore.LedgerEntry) {
	if entry.WeightEligible {
		return
	}
	id := key(entry)
	if id == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if prev, ok := s.entries[id]; ok && !prev.FirstSeen.IsZero() {
		if entry.FirstSeen.IsZero() || prev.FirstSeen.Before(entry.FirstSeen) {
			entry.FirstSeen = prev.FirstSeen
		}
	}
	s.entries[id] = entry
	s.generatedAt = time.Now().UTC()
}

// Len reports how many agents are currently held.
func (s *Store) Len() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.entries)
}

// Snapshot returns a deterministically ordered ledger: highest ShadowComposite
// first, ties broken by earliest FirstSeen then agent_id. The returned slice is a
// copy, safe to marshal without holding the lock.
func (s *Store) Snapshot() routerscore.Ledger {
	s.mu.RLock()
	defer s.mu.RUnlock()
	entries := make([]routerscore.LedgerEntry, 0, len(s.entries))
	for _, e := range s.entries {
		entries = append(entries, e)
	}
	sort.Slice(entries, func(i, j int) bool {
		if entries[i].ShadowComposite != entries[j].ShadowComposite {
			return entries[i].ShadowComposite > entries[j].ShadowComposite
		}
		if !entries[i].FirstSeen.Equal(entries[j].FirstSeen) {
			return entries[i].FirstSeen.Before(entries[j].FirstSeen)
		}
		return entries[i].AgentID < entries[j].AgentID
	})
	version := routerscore.RouterContractVersion
	ledger := routerscore.Ledger{
		Entries:               entries,
		RouterContractVersion: &version,
		Count:                 len(entries),
	}
	if !s.generatedAt.IsZero() {
		g := s.generatedAt
		ledger.GeneratedAt = &g
	}
	return ledger
}
