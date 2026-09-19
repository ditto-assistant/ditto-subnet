package universe

import (
	"context"
	"reflect"
	"testing"
)

func TestV13FactReplayRestoresWorldAuthority(t *testing.T) {
	for _, generate := range []func(context.Context, int64, int64, int, V13FactRenderer) ([]V10GeneratedCase, error){GenerateV13RenderedFactPrograms, GenerateV13RenderedPersonalFactPrograms} {
		recorder := &V13RecordingFactRenderer{Renderer: &factRenderFixture{}}
		want, err := generate(context.Background(), 731, 92713, 28, recorder)
		if err != nil {
			t.Fatal(err)
		}
		events := recorder.Events()
		replay := NewV13ReplayFactRenderer(events)
		// Neither exported events nor their plan pointers alias replay storage.
		events[0].Plan.Question = "mutated"
		got, err := generate(context.Background(), 731, 92713, 28, replay)
		if err != nil {
			t.Fatal(err)
		}
		if err := replay.Complete(); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, want) {
			t.Fatal("replay lost surfaces or grading authority")
		}
	}
}

func TestV13FactReplayRejectsAlteredTranscript(t *testing.T) {
	recorder := &V13RecordingFactRenderer{Renderer: &factRenderFixture{}}
	_, err := GenerateV13RenderedFactPrograms(context.Background(), 731, 92713, 4, recorder)
	if err != nil {
		t.Fatal(err)
	}
	tests := map[string]func([]V13FactRenderEvent) []V13FactRenderEvent{
		"missing": func(e []V13FactRenderEvent) []V13FactRenderEvent { return e[:len(e)-1] },
		"extra":   func(e []V13FactRenderEvent) []V13FactRenderEvent { return append(e, e[0]) },
		"request": func(e []V13FactRenderEvent) []V13FactRenderEvent { e[0].RequestSHA256 = "wrong"; return e },
		"plan":    func(e []V13FactRenderEvent) []V13FactRenderEvent { e[0].Plan.Question = "changed"; return e },
		"check":   func(e []V13FactRenderEvent) []V13FactRenderEvent { e[1].PlanSHA256 = "wrong"; return e },
		"order":   func(e []V13FactRenderEvent) []V13FactRenderEvent { e[0], e[1] = e[1], e[0]; return e },
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			replay := NewV13ReplayFactRenderer(mutate(recorder.Events()))
			_, err := GenerateV13RenderedFactPrograms(context.Background(), 731, 92713, 4, replay)
			if err == nil && replay.Complete() == nil {
				t.Fatal("altered transcript accepted")
			}
		})
	}
	replay := NewV13ReplayFactRenderer(recorder.Events())
	if _, err := GenerateV13RenderedFactPrograms(context.Background(), 732, 92713, 4, replay); err == nil {
		t.Fatal("different world accepted")
	}
	if replay.Complete() == nil {
		t.Fatal("failed replay completed")
	}
}
