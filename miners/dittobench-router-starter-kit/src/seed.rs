//! In-memory store for validator-seeded task memory.
//!
//! `POST /router/seed` delivers the task's memory records (possibly empty) plus
//! one canary. This reference kit stores them in process memory only — the
//! scored image runs with a read-only rootfs, so nothing is ever written to
//! disk. The default router does not read this store when forwarding upstream;
//! wiring it into a `memory_placement` lever is left as documented future work.

use std::sync::{Arc, Mutex, PoisonError};

use crate::protocol::VisibleMemoryRecord;

/// A cloneable, in-memory registry of the current task's seeded records.
#[derive(Clone, Default)]
pub struct SeedStore {
    records: Arc<Mutex<Vec<VisibleMemoryRecord>>>,
}

impl SeedStore {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    /// Replaces the stored records with a freshly seeded set and returns the
    /// new count. Seeding is idempotent per task: the latest seed wins.
    #[must_use]
    pub fn replace(&self, memories: Vec<VisibleMemoryRecord>) -> usize {
        let mut guard = self.records.lock().unwrap_or_else(PoisonError::into_inner);
        *guard = memories;
        guard.len()
    }

    /// The number of records currently stored.
    #[must_use]
    pub fn len(&self) -> usize {
        self.records
            .lock()
            .unwrap_or_else(PoisonError::into_inner)
            .len()
    }

    /// Whether the store holds no records.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(id: &str) -> VisibleMemoryRecord {
        VisibleMemoryRecord {
            memory_id: id.to_string(),
            scope: None,
            memory_type: None,
            content: "content".to_string(),
            confidence_micros: None,
        }
    }

    #[test]
    fn empty_seed_is_accepted() {
        let store = SeedStore::new();
        assert_eq!(store.replace(Vec::new()), 0);
        assert!(store.is_empty());
    }

    #[test]
    fn latest_seed_replaces_prior_records() {
        let store = SeedStore::new();
        assert_eq!(store.replace(vec![record("a"), record("b")]), 2);
        assert_eq!(store.replace(vec![record("c")]), 1);
        assert_eq!(store.len(), 1);
    }
}
