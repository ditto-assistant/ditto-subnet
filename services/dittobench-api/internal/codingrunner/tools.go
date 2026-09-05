package codingrunner

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

var toolNames = map[string]struct{}{
	"build.run":        {},
	"git.diff":         {},
	"git.status":       {},
	"repo.apply_patch": {},
	"repo.create_file": {},
	"repo.delete_file": {},
	"repo.list_tree":   {},
	"repo.read_file":   {},
	"repo.read_range":  {},
	"repo.search":      {},
	"tests.run":        {},
}

type requestProjection struct {
	CodingContractVersion int             `json:"coding_contract_version"`
	CaseID                string          `json:"case_id"`
	ProfileCapabilityID   string          `json:"profile_capability_id"`
	CallID                string          `json:"call_id"`
	Name                  string          `json:"name"`
	Arguments             json.RawMessage `json:"arguments"`
}

type eventRecord struct {
	Arguments             json.RawMessage `json:"arguments"`
	CallID                string          `json:"call_id"`
	CaseID                string          `json:"case_id"`
	CodingContractVersion int             `json:"coding_contract_version"`
	Error                 *ToolError      `json:"error"`
	Name                  string          `json:"name"`
	PreviousEventSHA256   string          `json:"previous_event_sha256"`
	ProfileCapabilityID   string          `json:"profile_capability_id"`
	Result                json.RawMessage `json:"result"`
	Sequence              uint64          `json:"sequence"`
}

// Invoke executes one serialized task-scoped tool call. Tool-level failures
// are recorded and returned as OK=false; envelope, identity, and lifecycle
// failures return an error without consuming an event sequence.
func (session *Session) Invoke(ctx context.Context, request ToolRequest) (ToolResponse, error) {
	if ctx == nil {
		return ToolResponse{}, errors.New("workspace tool context is required")
	}
	if err := request.validateVersion(session.manifest.CodingContractVersion); err != nil {
		return ToolResponse{}, err
	}
	operationContext, cancel := context.WithCancel(session.ctx)
	stopRequestCancellation := context.AfterFunc(ctx, cancel)
	defer func() {
		stopRequestCancellation()
		cancel()
	}()
	session.mu.Lock()
	defer session.mu.Unlock()
	if session.closed {
		return ToolResponse{}, errSessionClosed
	}
	if session.frozen != nil {
		return ToolResponse{}, errCapabilityRevoked
	}
	if !session.now().Before(session.manifest.Deadline) {
		return ToolResponse{}, errCapabilityExpired
	}
	if err := operationContext.Err(); err != nil {
		if !session.now().Before(session.manifest.Deadline) {
			return ToolResponse{}, errCapabilityExpired
		}
		return ToolResponse{}, errCapabilityRevoked
	}
	if request.CaseID != session.manifest.CaseID || request.ProfileCapabilityID != session.manifest.ProfileCapabilityID {
		session.latchFailure("candidate_integrity", "capability_identity")
		return ToolResponse{}, errCapabilityIdentity
	}
	requestBody, err := canonicalStruct(requestProjection(request))
	if err != nil {
		return ToolResponse{}, err
	}
	requestSHA := sha256Hex(requestBody)
	if cached, exists := session.calls[request.CallID]; exists {
		if cached.requestSHA256 != requestSHA {
			session.latchFailure("candidate_integrity", "call_id_conflict")
			return ToolResponse{}, errCallIDConflict
		}
		return cloneToolResponse(cached.response), nil
	}
	if uint32(len(session.calls)) >= session.manifest.Limits.MaxToolCalls {
		return ToolResponse{}, errToolBudget
	}
	if _, known := toolNames[request.Name]; !known {
		return session.record(request, requestSHA, nil, &ToolError{Code: "unknown_tool", Message: "unknown coding workspace tool"})
	}
	result, toolErr := session.dispatch(operationContext, request.Name, request.Arguments)
	if toolErr != nil {
		return session.record(request, requestSHA, nil, toolErr)
	}
	resultBody, err := marshalResult(result)
	if err != nil {
		return session.record(request, requestSHA, nil, &ToolError{Code: "result_encoding", Message: "workspace result could not be encoded"})
	}
	return session.record(request, requestSHA, resultBody, nil)
}

func (session *Session) record(
	request ToolRequest,
	requestSHA string,
	result json.RawMessage,
	toolErr *ToolError,
) (ToolResponse, error) {
	nextSequence := session.sequence + 1
	if result == nil {
		result = json.RawMessage("null")
	}
	event := eventRecord{
		Arguments:             append(json.RawMessage(nil), request.Arguments...),
		CallID:                request.CallID,
		CaseID:                request.CaseID,
		CodingContractVersion: request.CodingContractVersion,
		Error:                 toolErr,
		Name:                  request.Name,
		PreviousEventSHA256:   session.eventRoot,
		ProfileCapabilityID:   request.ProfileCapabilityID,
		Result:                result,
		Sequence:              nextSequence,
	}
	body, err := canonicalStruct(event)
	if err != nil {
		return ToolResponse{}, err
	}
	nextEventRoot := sha256Hex(body)
	response := ToolResponse{
		CallID:      request.CallID,
		Sequence:    nextSequence,
		OK:          toolErr == nil,
		Result:      append(json.RawMessage(nil), result...),
		Error:       cloneToolError(toolErr),
		EventSHA256: nextEventRoot,
	}
	responseBody, err := json.Marshal(response)
	if err != nil {
		return ToolResponse{}, err
	}
	if len(responseBody)+1 > session.manifest.Limits.MaxResponseBytes {
		if toolErr == nil {
			return session.record(request, requestSHA, nil, &ToolError{Code: "result_limit", Message: "workspace result exceeds its signed output limit"})
		}
		session.latchFailure("control_plane_integrity", "response_limit")
		return ToolResponse{}, errors.New("workspace error response exceeds its signed output limit")
	}
	encodedResponseBytes := int64(len(responseBody) + 1)
	if session.replayCacheBytes > session.manifest.Limits.MaxReplayCacheBytes-encodedResponseBytes {
		session.latchFailure("validator_infrastructure", "replay_cache_limit")
		return ToolResponse{}, errors.New("workspace replay cache exhausted")
	}
	if session.transcriptBytes > session.manifest.Limits.MaxTranscriptBytes-int64(len(body)) {
		session.latchFailure("control_plane_integrity", "transcript_limit")
		return ToolResponse{}, errors.New("workspace transcript exhausted")
	}
	written, writeErr := session.transcript.Write(body)
	if writeErr != nil || written != len(body) {
		session.latchFailure("control_plane_integrity", "transcript_write")
		return ToolResponse{}, errors.New("workspace transcript write failed")
	}
	if err := session.transcript.Sync(); err != nil {
		session.latchFailure("control_plane_integrity", "transcript_sync")
		return ToolResponse{}, errors.New("workspace transcript sync failed")
	}
	_, _ = session.transcriptHash.Write(body)
	session.transcriptBytes += int64(len(body))
	session.sequence = nextSequence
	session.eventRoot = nextEventRoot
	session.calls[request.CallID] = cachedCall{requestSHA256: requestSHA, response: response}
	session.replayCacheBytes += encodedResponseBytes
	return cloneToolResponse(response), nil
}

func cloneToolError(value *ToolError) *ToolError {
	if value == nil {
		return nil
	}
	copyValue := *value
	return &copyValue
}

func cloneToolResponse(value ToolResponse) ToolResponse {
	value.Result = append(json.RawMessage(nil), value.Result...)
	value.Error = cloneToolError(value.Error)
	return value
}

func invalidTool(errorValue error) *ToolError {
	message := boundedText(errorValue.Error(), 1_024)
	code := "invalid_tool_request"
	var policy *workspaceViolation
	if errors.As(errorValue, &policy) {
		code = policy.code
	}
	return &ToolError{Code: code, Message: message}
}

func (session *Session) dispatch(ctx context.Context, name string, raw json.RawMessage) (any, *ToolError) {
	var result any
	var err error
	switch name {
	case "repo.list_tree":
		result, err = session.listTree(ctx, raw)
	case "repo.search":
		result, err = session.search(ctx, raw)
	case "repo.read_file":
		result, err = session.readWholeFile(ctx, raw)
	case "repo.read_range":
		result, err = session.readRange(ctx, raw)
	case "repo.apply_patch":
		result, err = session.applyPatch(ctx, raw)
	case "repo.create_file":
		result, err = session.createFile(ctx, raw)
	case "repo.delete_file":
		result, err = session.deleteFile(ctx, raw)
	case "tests.run":
		result, err = session.runCommand(ctx, raw, session.tests)
	case "build.run":
		result, err = session.runCommand(ctx, raw, session.builds)
	case "git.status":
		result, err = session.status(ctx, raw)
	case "git.diff":
		result, err = session.diff(ctx, raw)
	default:
		err = errors.New("unknown coding workspace tool")
	}
	if err != nil {
		return nil, invalidTool(err)
	}
	return result, nil
}

type listTreeArguments struct {
	Path  string `json:"path"`
	Depth int    `json:"depth"`
}

type treeResultEntry struct {
	Path      string `json:"path"`
	Type      string `json:"type"`
	SHA256    string `json:"sha256,omitempty"`
	SizeBytes int64  `json:"size_bytes,omitempty"`
}

func (session *Session) listTree(ctx context.Context, raw json.RawMessage) (any, error) {
	arguments, err := decodeArguments[listTreeArguments](raw)
	if err != nil || arguments.Depth < 0 || arguments.Depth > 8 {
		return nil, errors.New("repo.list_tree requires path and depth 0..8")
	}
	relative, target, err := session.target(arguments.Path, true)
	if err != nil {
		return nil, err
	}
	info, err := os.Lstat(target)
	if err != nil || !info.IsDir() {
		return nil, errors.New("tree path is not a directory")
	}
	entries := make([]treeResultEntry, 0)
	baseDepth := 0
	if relative != "." {
		baseDepth = strings.Count(relative, "/") + 1
	}
	err = filepath.WalkDir(target, func(current string, entry fs.DirEntry, walkErr error) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		if walkErr != nil {
			return walkErr
		}
		if current == target {
			return nil
		}
		relativeOS, relErr := filepath.Rel(session.root, current)
		if relErr != nil {
			return relErr
		}
		itemPath := filepath.ToSlash(relativeOS)
		depth := strings.Count(itemPath, "/") + 1 - baseDepth
		if depth > arguments.Depth {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		entryInfo, infoErr := os.Lstat(current)
		if infoErr != nil {
			return infoErr
		}
		if entryInfo.Mode()&fs.ModeSymlink != 0 {
			return violation("symlink", "workspace contains a symlink")
		}
		if entryInfo.IsDir() {
			entries = append(entries, treeResultEntry{Path: itemPath, Type: "directory"})
		} else if entryInfo.Mode().IsRegular() {
			digest, readErr := hashFile(ctx, current, entryInfo.Size())
			if readErr != nil {
				return readErr
			}
			entries = append(entries, treeResultEntry{Path: itemPath, Type: "file", SHA256: digest, SizeBytes: entryInfo.Size()})
		} else {
			return violation("special_file", "workspace contains a special file")
		}
		if len(entries) > 512 {
			return errors.New("tree result exceeds 512 entries")
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return struct {
		Path    string            `json:"path"`
		Entries []treeResultEntry `json:"entries"`
	}{Path: relative, Entries: entries}, nil
}

type searchArguments struct {
	Query      string `json:"query"`
	Path       string `json:"path"`
	MaxResults int    `json:"max_results"`
}

type searchMatch struct {
	Path   string `json:"path"`
	Line   int    `json:"line"`
	Column int    `json:"column"`
	Text   string `json:"text"`
}

func (session *Session) search(ctx context.Context, raw json.RawMessage) (any, error) {
	arguments, err := decodeArguments[searchArguments](raw)
	if err != nil || arguments.Query == "" || len(arguments.Query) > 256 ||
		arguments.MaxResults < 1 || arguments.MaxResults > session.manifest.Limits.MaxSearchResults {
		return nil, errors.New("repo.search arguments are outside contract bounds")
	}
	relative, target, err := session.target(arguments.Path, true)
	if err != nil {
		return nil, err
	}
	info, err := os.Lstat(target)
	if err != nil || (!info.IsDir() && !info.Mode().IsRegular()) {
		return nil, errors.New("search path is not a regular entry")
	}
	paths := []string{target}
	if info.IsDir() {
		paths = nil
		err = filepath.WalkDir(target, func(current string, entry fs.DirEntry, walkErr error) error {
			if err := ctx.Err(); err != nil {
				return err
			}
			if walkErr != nil {
				return walkErr
			}
			entryInfo, infoErr := os.Lstat(current)
			if infoErr != nil {
				return infoErr
			}
			if entryInfo.Mode()&fs.ModeSymlink != 0 {
				return violation("symlink", "workspace contains a symlink")
			}
			if entryInfo.Mode().IsRegular() {
				paths = append(paths, current)
			} else if !entryInfo.IsDir() {
				return violation("special_file", "workspace contains a special file")
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	sort.Strings(paths)
	matches := make([]searchMatch, 0)
	for _, filePath := range paths {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if len(matches) >= arguments.MaxResults {
			break
		}
		body, readErr := readBoundedFile(ctx, filePath, session.manifest.Limits.MaxFileBytes)
		if readErr != nil || !utf8.Valid(body) || int64(len(body)) > session.manifest.Limits.MaxFileBytes {
			continue
		}
		itemPathOS, relErr := filepath.Rel(session.root, filePath)
		if relErr != nil {
			return nil, errors.New("search result path is unavailable")
		}
		itemPath := filepath.ToSlash(itemPathOS)
		for lineNumber, line := range strings.Split(string(body), "\n") {
			if err := ctx.Err(); err != nil {
				return nil, err
			}
			for start := 0; start < len(line) && len(matches) < arguments.MaxResults; {
				column := strings.Index(line[start:], arguments.Query)
				if column < 0 {
					break
				}
				absolute := start + column
				matches = append(matches, searchMatch{Path: itemPath, Line: lineNumber + 1, Column: absolute + 1, Text: boundedText(line, 500)})
				start = absolute + len(arguments.Query)
			}
		}
	}
	return struct {
		Path    string        `json:"path"`
		Query   string        `json:"query"`
		Matches []searchMatch `json:"matches"`
	}{Path: relative, Query: arguments.Query, Matches: matches}, nil
}

type pathArguments struct {
	Path string `json:"path"`
}

func (session *Session) readWholeFile(ctx context.Context, raw json.RawMessage) (any, error) {
	arguments, err := decodeArguments[pathArguments](raw)
	if err != nil {
		return nil, err
	}
	relative, text, body, _, err := session.readFile(ctx, arguments.Path)
	if err != nil {
		return nil, err
	}
	if len(body) > session.manifest.Limits.MaxReadBytes {
		return nil, errors.New("file exceeds repo.read_file output limit")
	}
	return struct {
		Path       string `json:"path"`
		Content    string `json:"content"`
		SHA256     string `json:"sha256"`
		TotalLines int    `json:"total_lines"`
	}{relative, text, sha256Hex(body), lineCount(text)}, nil
}

type readRangeArguments struct {
	Path      string `json:"path"`
	StartLine int    `json:"start_line"`
	EndLine   int    `json:"end_line"`
}

func (session *Session) readRange(ctx context.Context, raw json.RawMessage) (any, error) {
	arguments, err := decodeArguments[readRangeArguments](raw)
	if err != nil || arguments.StartLine < 1 || arguments.EndLine < arguments.StartLine || arguments.EndLine-arguments.StartLine+1 > 400 {
		return nil, errors.New("repo.read_range requires an inclusive range of at most 400 lines")
	}
	relative, text, body, _, err := session.readFile(ctx, arguments.Path)
	if err != nil {
		return nil, err
	}
	lines := splitLinesKeepEnds(text)
	start := min(arguments.StartLine-1, len(lines))
	end := min(arguments.EndLine, len(lines))
	content := strings.Join(lines[start:end], "")
	if len(content) > session.manifest.Limits.MaxReadBytes {
		return nil, errors.New("repo.read_range output exceeds its signed limit")
	}
	return struct {
		Path       string `json:"path"`
		Content    string `json:"content"`
		SHA256     string `json:"sha256"`
		StartLine  int    `json:"start_line"`
		EndLine    int    `json:"end_line"`
		TotalLines int    `json:"total_lines"`
	}{relative, content, sha256Hex(body), arguments.StartLine, end, lineCount(text)}, nil
}

func splitLinesKeepEnds(value string) []string {
	if value == "" {
		return []string{}
	}
	lines := strings.SplitAfter(value, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	return lines
}

func lineCount(value string) int {
	if value == "" {
		return 0
	}
	count := strings.Count(value, "\n")
	if !strings.HasSuffix(value, "\n") {
		count++
	}
	return count
}

type replacement struct {
	OldText string `json:"old_text"`
	NewText string `json:"new_text"`
}

type applyPatchArguments struct {
	Path           string        `json:"path"`
	ExpectedSHA256 string        `json:"expected_sha256"`
	Replacements   []replacement `json:"replacements"`
}

func (session *Session) applyPatch(ctx context.Context, raw json.RawMessage) (any, error) {
	arguments, err := decodeArguments[applyPatchArguments](raw)
	if err != nil || !isLowerSHA256(arguments.ExpectedSHA256) || len(arguments.Replacements) == 0 || len(arguments.Replacements) > 16 {
		return nil, errors.New("repo.apply_patch arguments are outside contract bounds")
	}
	relative, text, body, mode, err := session.readFile(ctx, arguments.Path)
	if err != nil {
		return nil, err
	}
	if _, allowed := session.editable[relative]; !allowed {
		return nil, violation("protected_path", "path is not editable")
	}
	if sha256Hex(body) != arguments.ExpectedSHA256 {
		return nil, errors.New("expected_sha256 does not match the current file")
	}
	updated := text
	for _, item := range arguments.Replacements {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if item.OldText == "" || len(item.OldText) > MaxEditTextBytes || len(item.NewText) > MaxEditTextBytes ||
			strings.Count(updated, item.OldText) != 1 {
			return nil, errors.New("each old_text must be non-empty and occur exactly once")
		}
		updated = strings.Replace(updated, item.OldText, item.NewText, 1)
	}
	updatedBody := []byte(updated)
	if int64(len(updatedBody)) > session.manifest.Limits.MaxFileBytes {
		return nil, errors.New("patched file exceeds its signed limit")
	}
	_, target, _ := session.target(relative, false)
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := atomicWrite(target, updatedBody, mode); err != nil {
		return nil, errors.New("atomic workspace edit failed")
	}
	return struct {
		Path             string `json:"path"`
		SHA256           string `json:"sha256"`
		SizeBytes        int    `json:"size_bytes"`
		ReplacementCount int    `json:"replacement_count"`
	}{relative, sha256Hex(updatedBody), len(updatedBody), len(arguments.Replacements)}, nil
}

type createArguments struct {
	Path    string `json:"path"`
	Content string `json:"content"`
}

func (session *Session) createFile(ctx context.Context, raw json.RawMessage) (any, error) {
	arguments, err := decodeArguments[createArguments](raw)
	if err != nil || len(arguments.Content) > MaxEditTextBytes {
		return nil, errors.New("repo.create_file arguments are outside contract bounds")
	}
	relative, target, err := session.target(arguments.Path, false)
	if err != nil {
		return nil, err
	}
	if _, allowed := session.creatable[relative]; !allowed {
		return nil, violation("undeclared_add", "path is not creatable")
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	parentInfo, err := os.Lstat(filepath.Dir(target))
	if err != nil || !parentInfo.IsDir() || parentInfo.Mode()&fs.ModeSymlink != 0 {
		return nil, errors.New("create path parent is unavailable")
	}
	handle, err := os.OpenFile(target, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return nil, errors.New("create path already exists or is unavailable")
	}
	if err := handle.Chmod(0o644); err != nil {
		handle.Close()
		_ = os.Remove(target)
		return nil, errors.New("create file mode failed")
	}
	_, writeErr := handle.WriteString(arguments.Content)
	closeErr := handle.Close()
	if writeErr != nil || closeErr != nil {
		_ = os.Remove(target)
		return nil, errors.New("create file write failed")
	}
	body := []byte(arguments.Content)
	return struct {
		Path      string `json:"path"`
		SHA256    string `json:"sha256"`
		SizeBytes int    `json:"size_bytes"`
	}{relative, sha256Hex(body), len(body)}, nil
}

type deleteArguments struct {
	Path           string `json:"path"`
	ExpectedSHA256 string `json:"expected_sha256"`
}

func (session *Session) deleteFile(ctx context.Context, raw json.RawMessage) (any, error) {
	arguments, err := decodeArguments[deleteArguments](raw)
	if err != nil || !isLowerSHA256(arguments.ExpectedSHA256) {
		return nil, errors.New("repo.delete_file arguments are outside contract bounds")
	}
	relative, _, body, _, err := session.readFile(ctx, arguments.Path)
	if err != nil {
		return nil, err
	}
	if _, allowed := session.deletable[relative]; !allowed {
		return nil, violation("undeclared_delete", "path is not deletable")
	}
	if sha256Hex(body) != arguments.ExpectedSHA256 {
		return nil, errors.New("expected_sha256 does not match the current file")
	}
	_, target, _ := session.target(relative, false)
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := os.Remove(target); err != nil {
		return nil, errors.New("delete file failed")
	}
	return struct {
		Path    string `json:"path"`
		Deleted bool   `json:"deleted"`
	}{relative, true}, nil
}

type commandArguments struct {
	CommandID string `json:"command_id"`
}

func (session *Session) runCommand(ctx context.Context, raw json.RawMessage, commands map[string]CommandSpec) (any, error) {
	arguments, err := decodeArguments[commandArguments](raw)
	if err != nil {
		return nil, err
	}
	command, exists := commands[arguments.CommandID]
	if !exists {
		return nil, errors.New("command_id is not allowed by the runner manifest")
	}
	if session.executor == nil {
		return nil, errors.New("trusted command executor is unavailable")
	}
	command.Argv = append([]string(nil), command.Argv...)
	before, err := snapshot(ctx, session.root, session.manifest.Limits)
	if err != nil {
		return nil, err
	}
	commandContext, cancel := context.WithTimeout(ctx, command.Timeout)
	defer cancel()
	result, executeErr := session.executor.Execute(commandContext, session.root, command)
	afterContext, afterCancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer afterCancel()
	after, snapshotErr := snapshot(afterContext, session.root, session.manifest.Limits)
	if snapshotErr != nil {
		session.latchFailure(classifyFreezeError(snapshotErr), freezeErrorCode(snapshotErr))
		return nil, snapshotErr
	}
	if result.WorkspaceMutated || !equalSnapshots(before, after) {
		session.latchFailure("candidate_integrity", "command_mutation")
		return nil, violation("command_mutation", "manifest command mutated the authoring workspace")
	}
	if executeErr != nil {
		return nil, errors.New("trusted command executor failed")
	}
	return struct {
		CommandID      string `json:"command_id"`
		ReturnCode     int    `json:"returncode"`
		Stdout         string `json:"stdout"`
		Stderr         string `json:"stderr"`
		TimedOut       bool   `json:"timed_out"`
		DurationMillis int64  `json:"duration_ms"`
	}{
		CommandID:      command.ID,
		ReturnCode:     result.ReturnCode,
		Stdout:         session.scrubOutput(result.Stdout),
		Stderr:         session.scrubOutput(result.Stderr),
		TimedOut:       result.TimedOut,
		DurationMillis: max(0, result.Duration.Milliseconds()),
	}, nil
}

func (session *Session) latchFailure(kind, code string) {
	if session.latchedCode == "" {
		session.latchedKind = kind
		session.latchedCode = code
		session.cancel()
	}
}

func (session *Session) scrubOutput(value string) string {
	value = strings.ReplaceAll(value, session.root, "<workspace>")
	value = strings.ReplaceAll(value, filepath.Clean(session.root), "<workspace>")
	return boundedText(value, session.manifest.Limits.MaxReadBytes)
}

type noArguments struct{}

func (session *Session) status(ctx context.Context, raw json.RawMessage) (any, error) {
	if _, err := decodeArguments[noArguments](raw); err != nil {
		return nil, err
	}
	current, err := snapshot(ctx, session.root, session.manifest.Limits)
	if err != nil {
		return nil, err
	}
	paths, _, err := session.validateChanges(ctx, current)
	if err != nil {
		return nil, err
	}
	return struct {
		Clean        bool     `json:"clean"`
		ChangedPaths []string `json:"changed_paths"`
	}{len(paths) == 0, paths}, nil
}

type diffChange struct {
	Path         string  `json:"path"`
	Kind         string  `json:"kind"`
	BeforeSHA256 *string `json:"before_sha256"`
	AfterSHA256  *string `json:"after_sha256"`
	AfterContent *string `json:"after_content"`
}

func (session *Session) diff(ctx context.Context, raw json.RawMessage) (any, error) {
	if _, err := decodeArguments[noArguments](raw); err != nil {
		return nil, err
	}
	current, err := snapshot(ctx, session.root, session.manifest.Limits)
	if err != nil {
		return nil, err
	}
	paths, changes, err := session.validateChanges(ctx, current)
	if err != nil {
		return nil, err
	}
	resultChanges := make([]diffChange, 0, len(changes))
	for _, change := range changes {
		var content *string
		if change.Kind != "deleted" {
			if utf8.Valid(change.AfterContent) {
				text := string(change.AfterContent)
				content = &text
			}
		}
		resultChanges = append(resultChanges, diffChange{
			Path: change.Path, Kind: change.Kind, BeforeSHA256: change.BeforeSHA256,
			AfterSHA256: change.AfterSHA256, AfterContent: content,
		})
	}
	return struct {
		ChangedPaths []string     `json:"changed_paths"`
		Changes      []diffChange `json:"changes"`
	}{paths, resultChanges}, nil
}

func boundedText(value string, maximum int) string {
	if len(value) <= maximum {
		return value
	}
	boundary := maximum
	for boundary > 0 && !utf8.RuneStart(value[boundary]) {
		boundary--
	}
	return value[:boundary]
}

func equalSnapshots(left, right map[string]fileState) bool {
	if len(left) != len(right) {
		return false
	}
	for filePath, leftState := range left {
		rightState, exists := right[filePath]
		if !exists || leftState.kind != rightState.kind || leftState.mode != rightState.mode ||
			leftState.size != rightState.size || leftState.sha256 != rightState.sha256 {
			return false
		}
	}
	return true
}
