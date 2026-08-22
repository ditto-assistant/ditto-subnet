// Package codingattempt composes verified artifacts, authoring sessions, and
// pristine grading without exposing an endpoint or activating a worker.
package codingattempt

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingartifacts"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingseed"
)

// ArtifactSource opens one already audience-projected and verified capability.
// codingartifacts.Fetcher satisfies this interface.
type ArtifactSource interface {
	Open(context.Context, codingartifacts.Capability) (io.ReadCloser, error)
}

// Executor is the sandbox-attested adapter shared by authoring and grading.
type Executor interface {
	codingrunner.CommandExecutor
	codinggrader.Executor
}

// CapabilityRevoker closes the source-bound outer workspace route before the
// internal runner freezes its immutable submission.
type CapabilityRevoker interface {
	Revoke(context.Context) error
}

type SeedProjector interface {
	Project(io.Reader, codingseed.Binding) (codingseed.Projection, error)
}

// Binding is the common immutable identity for both attempt phases.
type Binding struct {
	TicketID            string
	CaseID              string
	ProfileCapabilityID string
	Deadline            time.Time
}

// AuthoringSpec contains only authoring-phase capabilities. Grader material is
// structurally absent.
type AuthoringSpec struct {
	Binding               Binding
	VisibleBundle         codingartifacts.Capability
	MemoryBundle          codingartifacts.Capability
	ResourceProfile       codingartifacts.Capability
	RunnerManifest        codingrunner.Manifest
	ResourcePolicy        codinggrader.ResourcePolicy
	MemoryBundleSHA256    string
	ResourceProfileSHA256 string
}

// GradingSpec contains only grading-phase capabilities. Memory is structurally
// absent and the immutable freeze identity is required.
type GradingSpec struct {
	Binding                 Binding
	FreezeID                string
	AuthoringEvidenceSHA256 string
	FrozenSubmissionKey     string
	FrozenPatchSHA256       string
	VisibleBundle           codingartifacts.Capability
	ResourceProfile         codingartifacts.Capability
	GraderBundle            codingartifacts.Capability
	GraderManifest          codinggrader.Manifest
}

// RuntimeConfig supplies the concrete artifact source and sandbox executor.
type RuntimeConfig struct {
	Artifacts     ArtifactSource
	Executor      Executor
	SeedProjector SeedProjector
	Now           func() time.Time
}

// Runtime composes the existing reviewed coding primitives. It owns no
// Platform, miner-harness, inference-relay, scheduler, or score client.
type Runtime struct {
	artifacts     ArtifactSource
	executor      Executor
	seedProjector SeedProjector
	now           func() time.Time
}

// AuthoringSession holds one deep-owned scoped seed projection and one runner
// session. Raw memory artifact readers are closed before it is returned.
type AuthoringSession struct {
	runner *codingrunner.Session
	seed   codingseed.Projection

	frozen       bool
	freezing     bool
	closed       bool
	freezeResult codingrunner.FreezeResult
	freezeErr    error
	closeErr     error
	mu           sync.Mutex
}

// Handler returns the internal runner tool handler. A later trusted publisher
// must mount it behind a source-bound unguessable outer capability.
func (session *AuthoringSession) Handler() http.Handler {
	return session.runner.Handler()
}

// SeedProjection returns an immutable, deep-owned task-scoped seed projection.
func (session *AuthoringSession) SeedProjection() codingseed.Projection {
	session.mu.Lock()
	defer session.mu.Unlock()
	return session.seed
}

// MarshalJSON fails closed because a session owns a private seed projection and
// validator-local workspace.
func (*AuthoringSession) MarshalJSON() ([]byte, error) {
	return nil, errors.New("coding authoring sessions cannot be serialized")
}

// String exposes lifecycle state only, never readers or workspace paths.
func (session *AuthoringSession) String() string {
	session.mu.Lock()
	defer session.mu.Unlock()
	return "CodingAuthoringSession{frozen=" + strconv.FormatBool(session.frozen) +
		" freezing=" + strconv.FormatBool(session.freezing) +
		" closed=" + strconv.FormatBool(session.closed) + "}"
}

// GoString keeps %#v diagnostics on the redacted projection.
func (session *AuthoringSession) GoString() string {
	return session.String()
}

// LogValue keeps structured logging on the redacted projection.
func (session *AuthoringSession) LogValue() slog.Value {
	session.mu.Lock()
	defer session.mu.Unlock()
	return slog.GroupValue(
		slog.Bool("frozen", session.frozen),
		slog.Bool("freezing", session.freezing),
		slog.Bool("closed", session.closed),
	)
}
