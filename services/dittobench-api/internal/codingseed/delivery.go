package codingseed

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
)

func (projector *Projector) Deliver(
	ctx context.Context,
	client SeedClient,
	projection Projection,
) (Delivery, error) {
	if projector == nil || ctx == nil || nilLike(client) || projection.deadline.IsZero() {
		return Delivery{}, errors.New("coding seed delivery dependencies are incomplete")
	}
	request := cloneSeedRequest(projection.request)
	if err := request.Validate(); err != nil {
		return Delivery{}, errors.New("coding seed delivery request is invalid")
	}
	now := projector.now().UTC()
	deadline := projection.deadline.UTC()
	if !deadline.After(now) || deadline.After(now.Add(2*time.Hour)) {
		return Delivery{}, errors.New("coding seed delivery deadline is invalid")
	}
	firstContext, cancelFirst, err := projector.callContext(ctx, deadline)
	if err != nil {
		return Delivery{}, err
	}
	first, firstErr := client.Seed(firstContext, request)
	callErr := firstContext.Err()
	cancelFirst()
	if firstErr != nil {
		return Delivery{}, fmt.Errorf("deliver coding memory seed: %w", firstErr)
	}
	if callErr != nil {
		return Delivery{}, callErr
	}
	if err := ctx.Err(); err != nil {
		return Delivery{}, err
	}
	if !deadline.After(projector.now().UTC()) {
		return Delivery{}, context.DeadlineExceeded
	}
	if err := first.ValidateIdentity(request); err != nil {
		return Delivery{}, errors.New("coding seed acknowledgement is invalid")
	}
	return Delivery{
		AlreadySeeded:      first.IdempotentReplay,
		MemoryBundleSHA256: request.MemoryBundleSHA256, MemoryCount: len(request.Memories),
	}, nil
}

func (projector *Projector) callContext(
	parent context.Context,
	deadline time.Time,
) (context.Context, context.CancelFunc, error) {
	if err := parent.Err(); err != nil {
		return nil, nil, err
	}
	now := projector.now().UTC()
	callDeadline := now.Add(projector.timeout)
	if deadline.Before(callDeadline) {
		callDeadline = deadline
	}
	if !callDeadline.After(now) {
		return nil, nil, context.DeadlineExceeded
	}
	callContext, cancel := context.WithDeadline(parent, callDeadline)
	return callContext, cancel, nil
}

func cloneSeedRequest(request codingcontract.SeedRequest) codingcontract.SeedRequest {
	request.Memories = cloneMemories(request.Memories)
	return request
}

func nilLike(value any) bool {
	if value == nil {
		return true
	}
	reflected := reflect.ValueOf(value)
	switch reflected.Kind() {
	case reflect.Chan, reflect.Func, reflect.Interface, reflect.Map, reflect.Pointer, reflect.Slice:
		return reflected.IsNil()
	default:
		return false
	}
}
