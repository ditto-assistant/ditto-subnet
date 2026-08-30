package codingharness

import (
	"context"
	"errors"
	"net/url"

	"github.com/ditto-assistant/dittobench-api/internal/codingcertifier"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
)

func (handle *Handle) Activate(ctx context.Context) error {
	if handle == nil || ctx == nil {
		return ErrLifecycle
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	handle.mu.Lock()
	switch handle.state {
	case stateActive:
		handle.mu.Unlock()
		return nil
	case stateActivating:
		done := handle.activationDone
		handle.mu.Unlock()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-done:
			handle.mu.Lock()
			err := handle.activationErr
			active := handle.state == stateActive
			handle.mu.Unlock()
			if active {
				return nil
			}
			return errors.Join(ErrLifecycle, err)
		}
	case stateDormant:
		remaining := handle.binding.Deadline.Sub(handle.factory.now().UTC())
		if remaining <= 0 {
			handle.activationErr = ErrClosed
			handle.state = stateTerminal
			handle.mu.Unlock()
			return ErrClosed
		}
		handle.state = stateActivating
		handle.activationDone = make(chan struct{})
	default:
		err := handle.activationErr
		handle.mu.Unlock()
		return errors.Join(ErrClosed, err)
	}
	done := handle.activationDone
	remaining := handle.binding.Deadline.Sub(handle.factory.now().UTC())
	handle.mu.Unlock()

	if remaining <= 0 {
		handle.mu.Lock()
		handle.activationErr = ErrClosed
		handle.state = stateTerminal
		close(done)
		handle.mu.Unlock()
		return ErrClosed
	}
	startContext, cancelStart := context.WithTimeout(ctx, remaining)
	running, startErr := handle.factory.runtime.Start(startContext, handle.image)
	cancelStart()
	var sourceLease *codingsource.Lease
	var client *codingcertifier.HTTPHarnessClient
	if startErr == nil && !nilLike(running) && validRunning(running, handle.image) {
		sourceLease, startErr = handle.factory.sources.Register(sourceBinding(handle), running.SourceIP())
	}
	if startErr == nil && sourceLease != nil {
		client, startErr = codingcertifier.NewHTTPHarnessClient(running.BaseURL(), handle.factory.client)
	}
	if startErr != nil || nilLike(running) || sourceLease == nil || client == nil {
		if sourceLease != nil {
			startErr = errors.Join(startErr, sourceLease.Close())
		}
		if !nilLike(running) {
			startErr = errors.Join(startErr, handle.factory.runtime.Stop(context.WithoutCancel(ctx), running))
		} else {
			handle.factory.runtime.Release(context.WithoutCancel(ctx), handle.image)
		}
		if startErr == nil {
			startErr = ErrLifecycle
		}
	}
	handle.mu.Lock()
	if startErr == nil {
		handle.running = running
		handle.sourceLease = sourceLease
		handle.client = client
		handle.state = stateActive
	} else {
		handle.activationErr = startErr
		handle.state = stateTerminal
	}
	close(done)
	handle.mu.Unlock()
	if startErr != nil {
		return errors.Join(ErrLifecycle, startErr)
	}
	return nil
}

func validRunning(running Running, image string) bool {
	return validIdentifier(running.ContainerID(), 256) && running.ImageRef() == image &&
		validIdentifier(running.SourceIP(), 128) && validHarnessBaseURL(running.BaseURL())
}

func validHarnessBaseURL(value string) bool {
	parsed, err := url.ParseRequestURI(value)
	return err == nil && parsed.Scheme == "http" && parsed.Hostname() == "127.0.0.1" &&
		parsed.Port() != "" && parsed.User == nil && (parsed.Path == "" || parsed.Path == "/") &&
		parsed.RawQuery == "" && parsed.Fragment == ""
}

func (handle *Handle) Destroy(ctx context.Context) error {
	if handle == nil {
		return nil
	}
	if ctx == nil {
		return ErrLifecycle
	}
	for {
		handle.mu.Lock()
		switch handle.state {
		case stateDestroyed:
			handle.mu.Unlock()
			return nil
		case stateActivating:
			done := handle.activationDone
			handle.mu.Unlock()
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-done:
				continue
			}
		case stateDormant, stateTerminal:
			handle.state = stateStopping
			handle.destroyDone = make(chan struct{})
			done := handle.destroyDone
			handle.mu.Unlock()
			handle.factory.runtime.Release(context.WithoutCancel(ctx), handle.image)
			err := handle.finishDestroy()
			close(done)
			return err
		case stateStopping:
			done := handle.destroyDone
			handle.mu.Unlock()
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-done:
				continue
			}
		case stateActive:
			handle.state = stateStopping
			handle.destroyDone = make(chan struct{})
			done := handle.destroyDone
			running, sourceLease := handle.running, handle.sourceLease
			handle.client = nil
			handle.mu.Unlock()
			var err error
			if sourceLease != nil {
				err = sourceLease.Close()
			}
			if !nilLike(running) {
				err = errors.Join(err, handle.factory.runtime.Stop(ctx, running))
			}
			if err != nil {
				handle.mu.Lock()
				handle.state = stateActive
				close(done)
				handle.mu.Unlock()
				return errors.Join(ErrLifecycle, err)
			}
			err = handle.finishDestroy()
			close(done)
			return err
		default:
			handle.mu.Unlock()
			return ErrLifecycle
		}
	}
}

func (handle *Handle) finishDestroy() error {
	handle.factory.mu.Lock()
	if handle.factory.instances[handle.instanceID] != handle {
		handle.factory.mu.Unlock()
		return ErrLifecycle
	}
	delete(handle.factory.instances, handle.instanceID)
	handle.factory.mu.Unlock()
	handle.mu.Lock()
	handle.state = stateDestroyed
	handle.running = nil
	handle.sourceLease = nil
	handle.client = nil
	handle.image = ""
	handle.mu.Unlock()
	return nil
}
