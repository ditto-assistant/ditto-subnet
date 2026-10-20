package inference

import (
	"testing"
	"time"
)

func TestNonceExpiresAtCoversAFutureStamp(t *testing.T) {
	now := time.Date(2026, 10, 14, 12, 0, 0, 0, time.UTC)
	requested := now.Add(2 * time.Minute)
	got := nonceExpiresAt(now, requested, 2*time.Minute)
	want := requested.Add(2*time.Minute + 60*time.Second)
	if !got.Equal(want) {
		t.Fatalf("future stamp: got %s want %s", got, want)
	}
}

func TestNonceExpiresAtRetainsAPastStampFromReceipt(t *testing.T) {
	now := time.Date(2026, 10, 14, 12, 0, 0, 0, time.UTC)
	requested := now.Add(-time.Minute)
	got := nonceExpiresAt(now, requested, 2*time.Minute)
	want := now.Add(2*time.Minute + 60*time.Second)
	if !got.Equal(want) {
		t.Fatalf("past stamp: got %s want %s", got, want)
	}
}
