package universe

import (
	"context"
	"fmt"
)

func (r *V13RecordingFactRenderer) PlanDocument(ctx context.Context, req V13FactDocumentRequest) (V13FactDocumentPlan, error) {
	impl, ok := r.Renderer.(V13FactDocumentRenderer)
	if !ok {
		return V13FactDocumentPlan{}, fmt.Errorf("fact recorder: document renderer required")
	}
	requestSHA, err := V13FactRenderDigest(req)
	if err != nil {
		return V13FactDocumentPlan{}, err
	}
	plan, err := impl.PlanDocument(ctx, cloneV13DocumentRequest(req))
	if err != nil {
		return plan, err
	}
	if _, err := BindV13FactDocument(req, plan); err != nil {
		return V13FactDocumentPlan{}, err
	}
	planSHA, err := V13FactRenderDigest(plan)
	if err != nil {
		return V13FactDocumentPlan{}, err
	}
	copy := cloneV13DocumentPlan(plan)
	r.events = append(r.events, V13FactRenderEvent{Phase: "document_plan", RequestSHA256: requestSHA, PlanSHA256: planSHA, DocumentPlan: &copy})
	return plan, nil
}

func (r *V13RecordingFactRenderer) CheckDocument(ctx context.Context, req V13FactDocumentRequest, plan V13FactDocumentPlan) error {
	impl, ok := r.Renderer.(V13FactDocumentRenderer)
	if !ok {
		return fmt.Errorf("fact recorder: document renderer required")
	}
	requestSHA, err := V13FactRenderDigest(req)
	if err != nil {
		return err
	}
	planSHA, err := V13FactRenderDigest(plan)
	if err != nil {
		return err
	}
	if err := impl.CheckDocument(ctx, cloneV13DocumentRequest(req), cloneV13DocumentPlan(plan)); err != nil {
		return err
	}
	r.events = append(r.events, V13FactRenderEvent{Phase: "document_check", RequestSHA256: requestSHA, PlanSHA256: planSHA})
	return nil
}

func (r *V13ReplayFactRenderer) PlanDocument(ctx context.Context, req V13FactDocumentRequest) (V13FactDocumentPlan, error) {
	fail := func() (V13FactDocumentPlan, error) {
		r.failed = true
		return V13FactDocumentPlan{}, fmt.Errorf("fact replay: document plan mismatch")
	}
	if ctx.Err() != nil {
		return fail()
	}
	e, err := r.take("document_plan", req)
	if err != nil {
		return V13FactDocumentPlan{}, err
	}
	if e.DocumentPlan == nil || e.Plan != nil {
		return fail()
	}
	digest, err := V13FactRenderDigest(*e.DocumentPlan)
	if err != nil || digest != e.PlanSHA256 {
		return fail()
	}
	if _, err := BindV13FactDocument(req, *e.DocumentPlan); err != nil {
		return fail()
	}
	return cloneV13DocumentPlan(*e.DocumentPlan), nil
}

func (r *V13ReplayFactRenderer) CheckDocument(ctx context.Context, req V13FactDocumentRequest, plan V13FactDocumentPlan) error {
	fail := func() error { r.failed = true; return fmt.Errorf("fact replay: document check mismatch") }
	if ctx.Err() != nil {
		return fail()
	}
	e, err := r.take("document_check", req)
	if err != nil {
		return err
	}
	digest, err := V13FactRenderDigest(plan)
	if err != nil || digest != e.PlanSHA256 || e.Plan != nil || e.DocumentPlan != nil {
		return fail()
	}
	return nil
}
