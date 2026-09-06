package codinghostedworker

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codinggrader"
	"github.com/ditto-assistant/dittobench-api/internal/codinghostedinput"
	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/google/uuid"
)

const controlMagic = "DITTO-HOSTED-CONTROL-V2\n"

// ControlConfig is explicit trusted Platform runtime authority. Expected and
// Profile come from the approved assignment, never the incoming input frame.
type ControlConfig struct {
	SocketPath           string
	Token                []byte
	Expected             codinghostedinput.Expected
	Profile              codinghostedinput.Profile
	GradingProfile       []byte
	GradingProfileSHA256 string
}
type ControlClient struct {
	config         ControlConfig
	profile        []byte
	gradingProfile []byte
	testGrader     func(context.Context, codinggrader.HostedManifest) (codinggrader.Executor, error)
}

func NewControlClient(config ControlConfig) (*ControlClient, error) {
	if !controlSocket(config.SocketPath) || len(config.Token) != 32 || bytes.Equal(config.Token, make([]byte, 32)) {
		return nil, ErrAttempt
	}
	digest, err := codinghostedinput.ProfileDigest(config.Profile)
	if err != nil || digest != config.Expected.ExecutionProfileSHA256 {
		return nil, ErrAttempt
	}
	body, err := json.Marshal(config.Profile)
	if err != nil {
		return nil, ErrAttempt
	}
	config.Token = bytes.Clone(config.Token)
	return &ControlClient{config: config, profile: body, gradingProfile: bytes.Clone(config.GradingProfile)}, nil
}

func controlSocket(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return false
	}
	parent := filepath.Dir(path)
	real, err := filepath.EvalSymlinks(parent)
	if err != nil || real != parent {
		return false
	}
	for _, item := range []struct {
		path string
		mode os.FileMode
	}{{parent, 0700}, {path, 0600}} {
		info, err := os.Lstat(item.path)
		if err != nil || info.Mode().Perm() != item.mode {
			return false
		}
		owner, ok := info.Sys().(*syscall.Stat_t)
		if !ok || owner.Uid != uint32(os.Geteuid()) {
			return false
		}
		if item.path == parent {
			if !info.IsDir() {
				return false
			}
		} else if info.Mode()&os.ModeSocket == 0 {
			return false
		}
	}
	return true
}

func sourceProjection(s codingsource.HostedBinding) map[string]any {
	return map[string]any{
		"evaluation_id": s.EvaluationID, "attempt_id": s.AttemptID, "worker_id": s.WorkerID, "assignment_sha256": s.AssignmentSHA256,
		"artifact_sha256": s.AgentArtifactSHA256, "harness_instance_id": s.HarnessInstanceID, "profile_capability_id": s.ProfileCapabilityID, "deadline_unix": s.Deadline.Unix(),
	}
}

type privateControlConn struct {
	net.Conn
	cancel context.CancelFunc
	stop   func() bool
}

func (c *privateControlConn) Close() error { c.stop(); c.cancel(); return c.Conn.Close() }

type controlReply struct {
	Schema      string `json:"schema"`
	RequestID   string `json:"request_id"`
	Operation   string `json:"operation"`
	SourceSHA   string `json:"source_sha256"`
	OK          *bool  `json:"ok"`
	PayloadKind string `json:"payload_kind"`
	PayloadSize int64  `json:"payload_size"`
	EvidenceSHA string `json:"evidence_sha256"`
	Bridge      struct {
		GrantID   string `json:"grant_id"`
		PolicySHA string `json:"policy_sha256"`
		Path      string `json:"socket_path"`
		Token     []byte `json:"token_base64"`
		Expires   int64  `json:"expires_at_unix"`
	} `json:"bridge"`
	Freeze struct {
		EvaluationID  string `json:"evaluation_id"`
		AttemptID     string `json:"attempt_id"`
		AssignmentSHA string `json:"assignment_sha256"`
		PatchSHA      string `json:"frozen_patch_sha256"`
	} `json:"freeze"`
}

func (c *ControlClient) exchange(ctx context.Context, s codingsource.HostedBinding, op string, extra map[string]any, body func(io.Writer) error) (*privateControlConn, controlReply, error) {
	fail := func() (*privateControlConn, controlReply, error) { return nil, controlReply{}, ErrAttempt }
	if c == nil || ctx == nil || ctx.Err() != nil || !controlSocket(c.config.SocketPath) {
		return fail()
	}
	expected := c.config.Expected
	if s.EvaluationID != expected.EvaluationID || s.AttemptID != expected.AttemptID || s.WorkerID != expected.WorkerID || s.AssignmentSHA256 != expected.AssignmentSHA256 || s.Deadline.Unix() != expected.DeadlineUnix {
		return fail()
	}
	if s.Deadline.Nanosecond() != 0 {
		return fail()
	}
	source := sourceProjection(s)
	encodedSource, _ := json.Marshal(source)
	encodedSource = append(encodedSource, '\n')
	sourceSHA := fmt.Sprintf("%x", sha256.Sum256(encodedSource))
	requestID := uuid.NewString()
	command := map[string]any{"schema": "dittobench-coding-hosted-control-command-v2", "request_id": requestID, "operation": op, "source": source}
	for k, v := range extra {
		command[k] = v
	}
	encoded, err := json.Marshal(command)
	if err != nil || len(encoded) > 16384 {
		return fail()
	}
	timeout := 30 * time.Second
	if op == "authoring" || op == "grading" || op == "grading_bundle" {
		timeout = 180 * time.Second
	}
	if op == "retain" || op == "terminal" {
		timeout = 600 * time.Second
	}
	call, cancel := context.WithTimeout(ctx, timeout)
	conn, err := (&net.Dialer{}).DialContext(call, "unix", c.config.SocketPath)
	if err != nil {
		cancel()
		return fail()
	}
	result := &privateControlConn{Conn: conn, cancel: cancel}
	result.stop = context.AfterFunc(call, func() { _ = conn.Close() })
	deadline, _ := call.Deadline()
	_ = conn.SetDeadline(deadline)
	success := false
	defer func() {
		if !success {
			_ = result.Close()
		}
	}()
	frame := append([]byte(controlMagic), c.config.Token...)
	frame = binary.BigEndian.AppendUint32(frame, uint32(len(encoded)))
	frame = append(frame, encoded...)
	if _, err := io.Copy(conn, bytes.NewReader(frame)); err != nil {
		return fail()
	}
	if body != nil {
		if err := body(conn); err != nil {
			return fail()
		}
	}
	unix, ok := conn.(*net.UnixConn)
	if !ok || unix.CloseWrite() != nil {
		return fail()
	}
	var length [4]byte
	if _, err := io.ReadFull(conn, length[:]); err != nil {
		return fail()
	}
	n := binary.BigEndian.Uint32(length[:])
	if n == 0 || n > 16384 {
		return fail()
	}
	response := make([]byte, n)
	if _, err := io.ReadFull(conn, response); err != nil || codingcontract.ValidateJSONDocument(response, 16384) != nil {
		return fail()
	}
	var reply controlReply
	if json.Unmarshal(response, &reply) != nil || reply.Schema != "dittobench-coding-hosted-control-result-v2" || reply.RequestID != requestID || reply.Operation != op || reply.SourceSHA != sourceSHA || reply.OK == nil || !*reply.OK || call.Err() != nil {
		return fail()
	}
	if op != "authoring" && op != "grading" && op != "grading_bundle" {
		var end [1]byte
		if n, err := conn.Read(end[:]); n != 0 || err != io.EOF || call.Err() != nil {
			return fail()
		}
	}
	success = true
	return result, reply, nil
}

func (c *ControlClient) Authoring(ctx context.Context, s codingsource.HostedBinding) (*codinghostedinput.Prepared, error) {
	conn, reply, err := c.exchange(ctx, s, "authoring", nil, nil)
	if err != nil {
		return nil, ErrAttempt
	}
	if reply.PayloadKind != "authoring" {
		_ = conn.Close()
		return nil, ErrAttempt
	}
	var profile codinghostedinput.Profile
	if json.Unmarshal(c.profile, &profile) != nil {
		_ = conn.Close()
		return nil, ErrAttempt
	}
	return codinghostedinput.Prepare(ctx, c.config.Expected, profile, conn)
}
func (c *ControlClient) simple(ctx context.Context, s codingsource.HostedBinding, op string) error {
	conn, _, err := c.exchange(ctx, s, op, nil, nil)
	if conn != nil {
		_ = conn.Close()
	}
	return err
}
func (c *ControlClient) CheckAuthoring(ctx context.Context, s codingsource.HostedBinding) error {
	return c.simple(ctx, s, "check")
}
func (c *ControlClient) Abort(ctx context.Context, s codingsource.HostedBinding) error {
	return c.simple(ctx, s, "abort")
}
func (c *ControlClient) Inference(ctx context.Context, s codingsource.HostedBinding) (Bridge, error) {
	bridge := Bridge{Revoke: func(ctx context.Context) error { return c.simple(ctx, s, "revoke") }, Close: func(ctx context.Context) error { return c.simple(ctx, s, "close") }}
	conn, reply, err := c.exchange(ctx, s, "inference", nil, nil)
	if conn != nil {
		_ = conn.Close()
	}
	if err != nil {
		return bridge, ErrAttempt
	}
	b := reply.Bridge
	id, err := uuid.Parse(b.GrantID)
	if err != nil || id == uuid.Nil || id.String() != b.GrantID || !digest(b.PolicySHA) || !controlSocket(b.Path) || len(b.Token) != 32 || bytes.Equal(b.Token, make([]byte, 32)) || b.Expires <= time.Now().Unix() || b.Expires > s.Deadline.Unix() {
		return bridge, ErrAttempt
	}
	bridge.GrantID, bridge.PolicySHA256, bridge.SocketPath, bridge.Token, bridge.ExpiresAt = b.GrantID, b.PolicySHA, b.Path, b.Token, time.Unix(b.Expires, 0)
	return bridge, nil
}

type controlBoundWriter struct {
	w          io.Writer
	n, maximum int64
}

func (w *controlBoundWriter) Write(b []byte) (int, error) {
	if int64(len(b)) > w.maximum-w.n {
		return 0, ErrAttempt
	}
	n, err := w.w.Write(b)
	w.n += int64(n)
	return n, err
}

func (c *ControlClient) Retain(ctx context.Context, s codingsource.HostedBinding, e Evidence) (string, error) {
	if c == nil || e.WriteTranscript == nil {
		return "", ErrAttempt
	}
	body, err := encodePrivateFreeze(e.Freeze)
	if err != nil {
		return "", ErrAttempt
	}
	hash := sha256.New()
	count := &controlBoundWriter{w: hash, maximum: 512 << 20}
	identity, err := e.WriteTranscript(count)
	transcriptSHA := fmt.Sprintf("%x", hash.Sum(nil))
	if err != nil || identity.SizeBytes != count.n || identity.SHA256 != transcriptSHA {
		return "", ErrAttempt
	}
	header := map[string]any{"completed_run": e.CompletedRun, "freeze_sha256": fmt.Sprintf("%x", sha256.Sum256(body)), "freeze_size": len(body), "transcript_sha256": transcriptSHA, "transcript_size": count.n}
	conn, reply, err := c.exchange(ctx, s, "retain", map[string]any{"retention": header}, func(w io.Writer) error {
		if _, err := io.Copy(w, bytes.NewReader(body)); err != nil {
			return err
		}
		secondHash := sha256.New()
		second := &controlBoundWriter{w: io.MultiWriter(w, secondHash), maximum: count.n}
		again, err := e.WriteTranscript(second)
		if err != nil || again != identity || second.n != count.n || fmt.Sprintf("%x", secondHash.Sum(nil)) != transcriptSHA {
			return ErrAttempt
		}
		return nil
	})
	if conn != nil {
		_ = conn.Close()
	}
	if err != nil || !digest(reply.EvidenceSHA) {
		return "", ErrAttempt
	}
	return reply.EvidenceSHA, nil
}
func (c *ControlClient) CommitFreeze(ctx context.Context, s codingsource.HostedBinding, patch []byte, evidence string) (codingrunner.HostedReplayAuthority, error) {
	if len(patch) > 128<<20 || !digest(evidence) {
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	conn, reply, err := c.exchange(ctx, s, "freeze", map[string]any{"patch_size": len(patch), "evidence_sha256": evidence}, func(w io.Writer) error { _, err := io.Copy(w, bytes.NewReader(patch)); return err })
	if conn != nil {
		_ = conn.Close()
	}
	f := reply.Freeze
	if err != nil || f.EvaluationID != s.EvaluationID || f.AttemptID != s.AttemptID || f.AssignmentSHA != s.AssignmentSHA256 || f.PatchSHA != fmt.Sprintf("%x", sha256.Sum256(patch)) {
		return codingrunner.HostedReplayAuthority{}, ErrAttempt
	}
	return codingrunner.HostedReplayAuthority{HostedAuthority: codingrunner.HostedAuthority{EvaluationID: f.EvaluationID, AttemptID: f.AttemptID, AssignmentSHA256: f.AssignmentSHA}, FrozenPatchSHA256: f.PatchSHA}, nil
}

func encodePrivateFreeze(value codingrunner.FreezeResult) ([]byte, error) {
	body, err := json.Marshal(value)
	if err != nil {
		return nil, ErrAttempt
	}
	var freeze map[string]json.RawMessage
	if json.Unmarshal(body, &freeze) != nil {
		return nil, ErrAttempt
	}
	if value.Submission != nil {
		var sub map[string]json.RawMessage
		if json.Unmarshal(freeze["submission"], &sub) != nil {
			return nil, ErrAttempt
		}
		sub["patch"], err = json.Marshal(base64.StdEncoding.EncodeToString(value.Submission.Patch))
		if err != nil {
			return nil, ErrAttempt
		}
		freeze["submission"], err = json.Marshal(sub)
		if err != nil {
			return nil, ErrAttempt
		}
	}
	body, err = json.Marshal(freeze)
	if err != nil || len(body) > 520<<20 {
		return nil, ErrAttempt
	}
	return body, nil
}
func (ControlConfig) String() string                { return "HostedControlConfig{private}" }
func (c ControlConfig) GoString() string            { return c.String() }
func (ControlConfig) MarshalJSON() ([]byte, error)  { return nil, ErrAttempt }
func (*ControlClient) String() string               { return "HostedControlClient{private}" }
func (c *ControlClient) GoString() string           { return c.String() }
func (*ControlClient) MarshalJSON() ([]byte, error) { return nil, ErrAttempt }

var _ Control = (*ControlClient)(nil)
