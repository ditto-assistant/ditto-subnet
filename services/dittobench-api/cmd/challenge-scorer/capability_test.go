package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"testing"

	"github.com/ditto-assistant/dittobench-datagen/challenge"
	"github.com/ditto-assistant/dittobench-datagen/genrun"
)

func TestWorkPollsAdvertiseCapability(t *testing.T) {
	var queries []url.Values
	fp := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		queries = append(queries, r.URL.Query())
		_, _ = w.Write([]byte(`{"work":null}`))
	}))
	defer fp.Close()
	client := &platformClient{base: fp.URL, token: "t", http: fp.Client(), advertise: advertisement("scorer-prod.1", genrun.Docker{})}
	if busy, err := processOne(context.Background(), client, genrun.Docker{}); busy || err != nil {
		t.Fatalf("processOne: busy=%v err=%v", busy, err)
	}
	if busy, err := processRound(context.Background(), client, genrun.Docker{}); busy || err != nil {
		t.Fatalf("processRound: busy=%v err=%v", busy, err)
	}
	if len(queries) != 2 {
		t.Fatalf("polls = %d", len(queries))
	}
	for _, q := range queries {
		if q.Get("scorer_id") != "scorer-prod.1" || q.Get("scorer_version") != ScorerVersion ||
			q.Get("min_cases") != strconv.Itoa(challenge.MinCases) || q.Get("max_cases") != strconv.Itoa(challenge.MaxCases) ||
			q.Get("sandbox") != "docker" {
			t.Fatalf("advertisement = %v", q)
		}
	}
	if got := advertisement("bad id/with slash", nil).Get("scorer_id"); got != "default" {
		t.Fatalf("invalid scorer id advertised as %q", got)
	}
}
