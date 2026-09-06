// Package codinghostedrelay composes native container source authority with a
// private Platform provider bridge. Nothing is mounted by application startup.
package codinghostedrelay

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"time"

	"github.com/ditto-assistant/dittobench-api/internal/codingcontract"
	"github.com/ditto-assistant/dittobench-api/internal/codingsource"
	"github.com/google/uuid"
)

const magic = "DITTO-HOSTED-INFERENCE-V2\n"
const maxRequest = 4 << 20
const maxResult = 12 << 20

var ErrRelay = errors.New("native hosted relay unavailable")
var ErrUnsettled = errors.New("native hosted inference is not drained")

type binding struct {
	Schema              string `json:"schema"`
	EvaluationID        string `json:"evaluation_id"`
	AttemptID           string `json:"attempt_id"`
	WorkerID            string `json:"worker_id"`
	GrantID             string `json:"grant_id"`
	AssignmentSHA256    string `json:"assignment_sha256"`
	PolicySHA256        string `json:"policy_sha256"`
	ArtifactSHA256      string `json:"artifact_sha256"`
	HarnessInstanceID   string `json:"harness_instance_id"`
	ProfileCapabilityID string `json:"profile_capability_id"`
	ExpiresAtUnix       int64  `json:"expires_at_unix"`
}

type client struct {
	path    string
	token   [32]byte
	binding binding
	digest  string
}

type result struct {
	Schema        string          `json:"schema"`
	BindingSHA256 string          `json:"binding_sha256"`
	RequestID     string          `json:"request_id"`
	Operation     string          `json:"operation"`
	Response      []byte          `json:"response_base64"`
	Settlement    json.RawMessage `json:"settlement"`
	LedgerDrained *bool           `json:"ledger_drained"`
}

func newClient(path string, token []byte, source codingsource.HostedBinding, grant, policy string) (*client, error) {
	parsed, err := uuid.Parse(grant)
	digestBytes, decodeErr := hex.DecodeString(policy)
	if err != nil || parsed == uuid.Nil || parsed.String() != grant || decodeErr != nil || len(digestBytes) != 32 || hex.EncodeToString(digestBytes) != policy || len(token) != 32 || bytes.Equal(token, make([]byte, 32)) || !privateSocket(path) {
		return nil, ErrRelay
	}
	value := binding{"dittobench-coding-hosted-relay-binding-v2", source.EvaluationID, source.AttemptID, source.WorkerID, grant, source.AssignmentSHA256, policy, source.AgentArtifactSHA256, source.HarnessInstanceID, source.ProfileCapabilityID, source.Deadline.Unix()}
	encoded, err := canonical(value)
	if err != nil {
		return nil, ErrRelay
	}
	sum := sha256.Sum256(encoded)
	c := &client{path: path, binding: value, digest: hex.EncodeToString(sum[:])}
	copy(c.token[:], token)
	return c, nil
}

func privateSocket(path string) bool {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return false
	}
	parent := filepath.Dir(path)
	resolved, err := filepath.EvalSymlinks(parent)
	if err != nil || resolved != parent {
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

func (c *client) call(ctx context.Context, operation, requestID string, body []byte) (result, error) {
	if ctx == nil || ctx.Err() != nil || !privateSocket(c.path) {
		return result{}, ErrRelay
	}
	command := struct {
		Schema    string `json:"schema"`
		Binding   string `json:"binding_sha256"`
		Operation string `json:"operation"`
		RequestID string `json:"request_id"`
		Request   []byte `json:"miner_request_base64,omitempty"`
	}{"dittobench-coding-hosted-relay-command-v2", c.digest, operation, requestID, body}
	encoded, err := canonical(command)
	if err != nil || len(encoded) > 6<<20 {
		return result{}, ErrRelay
	}
	timeout := 30 * time.Second
	if operation == "complete" {
		timeout = 340 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	conn, err := (&net.Dialer{}).DialContext(ctx, "unix", c.path)
	if err != nil {
		return result{}, ErrRelay
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	deadline, _ := ctx.Deadline()
	_ = conn.SetDeadline(deadline)
	frame := make([]byte, 0, len(magic)+32+4+len(encoded))
	frame = append(frame, magic...)
	frame = append(frame, c.token[:]...)
	frame = binary.BigEndian.AppendUint32(frame, uint32(len(encoded)))
	frame = append(frame, encoded...)
	if _, err = io.Copy(conn, bytes.NewReader(frame)); err != nil {
		return result{}, ErrRelay
	}
	var header [4]byte
	if _, err = io.ReadFull(conn, header[:]); err != nil {
		return result{}, ErrRelay
	}
	size := binary.BigEndian.Uint32(header[:])
	if size == 0 || size > maxResult {
		return result{}, ErrRelay
	}
	response := make([]byte, int(size))
	if _, err = io.ReadFull(conn, response); err != nil {
		return result{}, ErrRelay
	}
	// One frame then EOF; extra/truncated replies never count as success.
	var trailing [1]byte
	if n, err := conn.Read(trailing[:]); n != 0 || err != io.EOF || ctx.Err() != nil {
		return result{}, ErrRelay
	}
	if codingcontract.ValidateJSONDocument(response, maxResult) != nil {
		return result{}, ErrRelay
	}
	var output result
	if json.Unmarshal(response, &output) != nil || output.Schema != "dittobench-coding-hosted-relay-result-v2" || output.BindingSHA256 != c.digest || output.RequestID != requestID || output.Operation != operation {
		return result{}, ErrRelay
	}
	if operation != "complete" && (len(output.Response) != 0 || len(output.Settlement) != 0) {
		return result{}, ErrRelay
	}
	if operation == "revoke" && output.LedgerDrained == nil {
		return result{}, ErrRelay
	}
	return output, nil
}

func canonical(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var projected any
	if err = decoder.Decode(&projected); err != nil {
		return nil, err
	}
	var buffer bytes.Buffer
	encoder := json.NewEncoder(&buffer)
	encoder.SetEscapeHTML(false)
	err = encoder.Encode(projected)
	return buffer.Bytes(), err
}
