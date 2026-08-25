package main

import (
	"errors"
	"net"
	"strings"

	"github.com/ditto-assistant/dittobench-datagen/protocol"
	"github.com/ditto-assistant/dittobench-datagen/toolexec"
)

// confirmationToolEndpointRequest is the per-case identity needed to advertise
// the same validator-owned observed-execution URL scored DittoBench memory
// cases already receive.
type confirmationToolEndpointRequest struct {
	SourceIP     string
	CaseID       string
	UserID       string
	BenchVersion int
	SessionID    string
}

// confirmationToolEndpointAdvertiser returns a harness-reachable tool_endpoint
// for one confirmation /run. Memory tools stay unserved. stop must be called
// after the case container is finished. A missing advertiser is fail-closed.
type confirmationToolEndpointAdvertiser func(confirmationToolEndpointRequest) (endpoint string, stop func(), err error)

func newConfirmationToolEndpointAdvertiser(
	broker *inferenceBroker,
	allowPrivate bool,
) confirmationToolEndpointAdvertiser {
	return func(request confirmationToolEndpointRequest) (string, func(), error) {
		if broker == nil || net.ParseIP(request.SourceIP) == nil ||
			strings.TrimSpace(request.CaseID) == "" || strings.TrimSpace(request.UserID) == "" {
			return "", nil, errors.New("confirmation tool_endpoint identity is unavailable")
		}
		toolSrv := toolexec.NewServer()
		toolSrv.Register(request.CaseID, toolexec.BuildFixture(0, protocol.ToolCase{ID: request.CaseID}))
		endpoint, stop, err := startObservedToolServer(
			broker, allowPrivate, toolSrv, request.SourceIP, request.BenchVersion, request.SessionID, 1,
		)
		if err != nil || !endpoint.available() || stop == nil {
			if stop != nil {
				stop()
			}
			return "", nil, errors.New("confirmation tool_endpoint is unavailable")
		}
		url := endpoint.forCase(request.CaseID, request.UserID)
		if strings.TrimSpace(url) == "" {
			stop()
			return "", nil, errors.New("confirmation tool_endpoint is unavailable")
		}
		return url, stop, nil
	}
}

func bindConfirmationToolEndpoint(
	advertise confirmationToolEndpointAdvertiser,
	sourceIP string,
	request *protocol.RunRequest,
	benchVersion int,
	sessionID string,
) (func(), error) {
	if advertise == nil || request == nil {
		return nil, errors.New("confirmation tool_endpoint is unavailable")
	}
	if strings.TrimSpace(request.CaseID) == "" || strings.TrimSpace(request.UserID) == "" ||
		net.ParseIP(sourceIP) == nil {
		return nil, errors.New("confirmation tool_endpoint identity is unavailable")
	}
	endpoint, stop, err := advertise(confirmationToolEndpointRequest{
		SourceIP: sourceIP, CaseID: request.CaseID, UserID: request.UserID,
		BenchVersion: benchVersion, SessionID: sessionID,
	})
	if err != nil || strings.TrimSpace(endpoint) == "" || stop == nil {
		if stop != nil {
			stop()
		}
		return nil, errors.New("confirmation tool_endpoint is unavailable")
	}
	request.ToolEndpoint = endpoint
	return stop, nil
}
