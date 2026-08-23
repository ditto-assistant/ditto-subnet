//! Overlapping scored `/run` shares one process-wide Turso `Db`.
//!
//! A single `turso::Connection` rejects concurrent `query`/`execute` with
//! `database error: concurrent use forbidden`. The kit opens the store the
//! same way `Baseline::open_store` does; `ditto-harness` must give each
//! overlapping op its own `Database::connect()`.
//!
//! These tests do not need Ollama or a chat model.

use std::sync::Arc;

use ditto_harness::db::{Db, ListRecentMemoriesParams};

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn overlapping_store_read_and_write_do_not_hit_concurrent_use_forbidden() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("overlap.db");
    let db = Arc::new(
        Db::open(path.to_str().unwrap())
            .await
            .expect("open turso db the way Baseline::open_store does"),
    );
    for i in 0..64 {
        db.upsert_user(&format!("seed-{i:02}"))
            .await
            .expect("seed user");
    }

    let reader = Arc::clone(&db);
    let writer = Arc::clone(&db);
    let started = Arc::new(tokio::sync::Notify::new());
    let started_sig = Arc::clone(&started);

    let read = tokio::spawn(async move {
        let mut rows = reader
            .connection()
            .expect("connect")
            .query("SELECT uid FROM harness_users", ())
            .await?;
        started_sig.notify_one();
        let mut n = 0usize;
        while rows.next().await?.is_some() {
            n += 1;
        }
        anyhow::Ok(n)
    });
    started.notified().await;

    let mut write_errors = Vec::new();
    for i in 0..8 {
        if let Err(err) = writer.upsert_user(&format!("writer-{i}")).await {
            write_errors.push(err.to_string());
        }
    }
    let n = read.await.expect("join").expect("reader");
    assert!(
        write_errors.is_empty(),
        "overlapping store writes failed: {write_errors:?}"
    );
    assert!(n >= 64, "reader saw {n} rows");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 8)]
async fn eight_overlapping_list_recent_match_case_concurrency() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("overlap8.db");
    let db = Arc::new(
        Db::open(path.to_str().unwrap())
            .await
            .expect("open turso db the way Baseline::open_store does"),
    );
    db.upsert_user("u1").await.expect("user");
    for i in 0..32 {
        db.upsert_user(&format!("extra-{i:02}"))
            .await
            .expect("extra user");
    }

    let barrier = Arc::new(tokio::sync::Barrier::new(8));
    let mut set = tokio::task::JoinSet::new();
    for i in 0..8 {
        let db = Arc::clone(&db);
        let barrier = Arc::clone(&barrier);
        set.spawn(async move {
            barrier.wait().await;
            db.list_recent_memories(ListRecentMemoriesParams {
                user_id: "u1".to_string(),
                kg_id: "user_memories_u1".to_string(),
                limit: 16,
                ..ListRecentMemoriesParams::default()
            })
            .await
            .map(|rows| (i, rows.len()))
        });
    }

    let mut failed = Vec::new();
    while let Some(joined) = set.join_next().await {
        if let Err(err) = joined.expect("join") {
            failed.push(err.to_string());
        }
    }
    assert!(
        failed.is_empty(),
        "8-wide overlapping list_recent failed: {failed:?}"
    );
}
