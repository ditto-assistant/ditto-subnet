package universe

import (
	"context"
	"errors"
)

// V13FactRenderEvent is private producer provenance. A transcript is not an
// independent semantic approval: replay is allowed only after the enclosing
// artifact's digest has been verified against trusted issuance authority.
// Never send this transcript or its request hashes to an untrusted harness.
type V13FactRenderEvent struct {
	Phase         string               `json:"phase"`
	RequestSHA256 string               `json:"request_sha256"`
	PlanSHA256    string               `json:"plan_sha256"`
	Plan          *V13FactRenderPlan   `json:"plan,omitempty"`
	DocumentPlan  *V13FactDocumentPlan `json:"document_plan,omitempty"`
}

// V13RecordingFactRenderer records only successful author/check operations.
// The wrapped renderer remains responsible for independent semantic checking,
// exact model/provider identity and durable spend/receipt auditing.
type V13RecordingFactRenderer struct {
	Renderer V13FactRenderer
	events   []V13FactRenderEvent
}

func (r *V13RecordingFactRenderer) Plan(ctx context.Context, req V13FactRenderRequest) (V13FactRenderPlan, error) {
	if r.Renderer == nil {
		return V13FactRenderPlan{}, errors.New("fact recorder: renderer required")
	}
	requestSHA, err := V13FactRenderDigest(req)
	if err != nil {
		return V13FactRenderPlan{}, err
	}
	plan, err := r.Renderer.Plan(ctx, req)
	if err != nil {
		return plan, err
	}
	if _, err := BindV13FactRenderPlan(req, plan); err != nil {
		return V13FactRenderPlan{}, err
	}
	planSHA, err := V13FactRenderDigest(plan)
	if err != nil {
		return V13FactRenderPlan{}, err
	}
	copy := plan
	r.events = append(r.events, V13FactRenderEvent{Phase: "plan", RequestSHA256: requestSHA, PlanSHA256: planSHA, Plan: &copy})
	return plan, nil
}

func (r *V13RecordingFactRenderer) Check(ctx context.Context, req V13FactRenderRequest, plan V13FactRenderPlan) error {
	if r.Renderer == nil {
		return errors.New("fact recorder: renderer required")
	}
	requestSHA, err := V13FactRenderDigest(req)
	if err != nil {
		return err
	}
	planSHA, err := V13FactRenderDigest(plan)
	if err != nil {
		return err
	}
	if err := r.Renderer.Check(ctx, req, plan); err != nil {
		return err
	}
	r.events = append(r.events, V13FactRenderEvent{Phase: "check", RequestSHA256: requestSHA, PlanSHA256: planSHA})
	return nil
}

func cloneV13RenderEvents(events []V13FactRenderEvent) []V13FactRenderEvent {
	out := append([]V13FactRenderEvent(nil), events...)
	for i := range out {
		if out[i].Plan != nil {
			p := *out[i].Plan
			out[i].Plan = &p
		}
		if out[i].DocumentPlan != nil {
			p := cloneV13DocumentPlan(*out[i].DocumentPlan)
			out[i].DocumentPlan = &p
		}
	}
	return out
}

func (r *V13RecordingFactRenderer) Events() []V13FactRenderEvent {
	return cloneV13RenderEvents(r.events)
}

// V13ReplayFactRenderer reconstructs a trusted pinned rendering without network
// access. Request digests bind it to the evaluator's exact regenerated world,
// including counterfactual facts. It does NOT validate untrusted prose or
// authenticate a transcript. The caller must first verify the artifact pin,
// regenerate the authoritative world, and call Complete after generation.
type V13ReplayFactRenderer struct {
	events []V13FactRenderEvent
	next   int
	failed bool
}

func NewV13ReplayFactRenderer(events []V13FactRenderEvent) *V13ReplayFactRenderer {
	return &V13ReplayFactRenderer{events: cloneV13RenderEvents(events)}
}

func (r *V13ReplayFactRenderer) take(phase string, req any) (V13FactRenderEvent, error) {
	fail := func() (V13FactRenderEvent, error) {
		r.failed = true
		return V13FactRenderEvent{}, errors.New("fact replay: transcript mismatch")
	}
	if r.failed || r.next >= len(r.events) {
		return fail()
	}
	e := r.events[r.next]
	digest, err := V13FactRenderDigest(req)
	if err != nil || e.Phase != phase || e.RequestSHA256 != digest {
		return fail()
	}
	r.next++
	return e, nil
}

func (r *V13ReplayFactRenderer) Plan(ctx context.Context, req V13FactRenderRequest) (V13FactRenderPlan, error) {
	if err := ctx.Err(); err != nil {
		r.failed = true
		return V13FactRenderPlan{}, err
	}
	e, err := r.take("plan", req)
	if err != nil {
		return V13FactRenderPlan{}, err
	}
	if e.Plan == nil || e.DocumentPlan != nil {
		r.failed = true
		return V13FactRenderPlan{}, errors.New("fact replay: missing plan")
	}
	digest, err := V13FactRenderDigest(*e.Plan)
	if err != nil || digest != e.PlanSHA256 {
		r.failed = true
		return V13FactRenderPlan{}, errors.New("fact replay: plan digest mismatch")
	}
	if _, err := BindV13FactRenderPlan(req, *e.Plan); err != nil {
		r.failed = true
		return V13FactRenderPlan{}, err
	}
	return *e.Plan, nil
}

func (r *V13ReplayFactRenderer) Check(ctx context.Context, req V13FactRenderRequest, plan V13FactRenderPlan) error {
	if err := ctx.Err(); err != nil {
		r.failed = true
		return err
	}
	e, err := r.take("check", req)
	if err != nil {
		return err
	}
	digest, err := V13FactRenderDigest(plan)
	if err != nil || e.Plan != nil || e.DocumentPlan != nil || digest != e.PlanSHA256 {
		r.failed = true
		return errors.New("fact replay: checked rendering mismatch")
	}
	return nil
}

func (r *V13ReplayFactRenderer) Complete() error {
	if r.failed || len(r.events) == 0 || r.next != len(r.events) {
		return errors.New("fact replay: incomplete transcript")
	}
	return nil
}
