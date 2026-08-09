package longmemeval

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

const (
	// CleanedDatasetSHA256 and CleanedDatasetRevision are the exact public
	// LongMemEval-S condition imported by dittobench-api#78. They are recorded
	// here for provenance; the runtime verifies the profile pin rather than a
	// filename or download URL.
	CleanedDatasetSHA256   = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
	CleanedDatasetRevision = "huggingface-98d7416c24c778c2fee6e6f3006e7a073259d48f-" +
		"longmemeval-9e0b455f4ef0e2ab8f2e582289761153549043fc"

	// The imported cleaned file is about 265 MiB. A hard ceiling prevents an
	// accidental or hostile path from turning digest verification into an
	// unbounded read before the immutable digest can reject it.
	maxDatasetBytes = 384 << 20
)

// DatasetTurn is one source history turn. HasAnswer is intentionally retained
// only inside the trusted dataset representation and never appears in a wire
// request.
type DatasetTurn struct {
	Role      string `json:"role"`
	Content   string `json:"content"`
	HasAnswer bool   `json:"has_answer,omitempty"`
}

// DatasetCase is the private cleaned LongMemEval-S row required by selection,
// projection, and the official judge boundary. Answer provenance never crosses
// the Harness interface.
type DatasetCase struct {
	QuestionID         string          `json:"question_id"`
	QuestionType       string          `json:"question_type"`
	Question           string          `json:"question"`
	Answer             string          `json:"answer"`
	QuestionDate       string          `json:"question_date"`
	HaystackSessionIDs []string        `json:"haystack_session_ids"`
	HaystackDates      []string        `json:"haystack_dates"`
	HaystackSessions   [][]DatasetTurn `json:"haystack_sessions"`
	AnswerSessionIDs   []string        `json:"answer_session_ids"`
}

// LoadedDataset contains only the selected full rows. Selection metadata is
// scanned first, keeping the 265 MiB JSON file out of the Go heap.
type LoadedDataset struct {
	Revision  string
	SHA256    string
	Selection Selection
	selected  map[string]DatasetCase
}

// IsOfficialCleanedDataset reports whether the profile names the exact source
// condition already audited in the monorepo. Shadow/unit profiles may use
// other content-addressed fixtures, but activation code should require true.
func IsOfficialCleanedDataset(profile Profile) bool {
	return profile.DatasetRevision == CleanedDatasetRevision && profile.DatasetSHA256 == CleanedDatasetSHA256
}

// LoadSelectedDataset verifies the complete raw JSON bytes against the frozen
// profile, selects the balanced case set, then performs a second streaming pass
// to retain only selected full rows. The reader must be seekable so selection
// stays bounded without rewriting the upstream dataset.
func LoadSelectedDataset(ctx context.Context, source io.ReadSeeker, profile Profile) (LoadedDataset, error) {
	if source == nil {
		return LoadedDataset{}, errors.New("LongMemEval dataset source is nil")
	}
	if err := profile.Validate(); err != nil {
		return LoadedDataset{}, err
	}
	if _, err := source.Seek(0, io.SeekStart); err != nil {
		return LoadedDataset{}, fmt.Errorf("seek LongMemEval dataset: %w", err)
	}

	metadata := make([]Case, 0, 500)
	err := scanVerifiedDataset(ctx, source, profile.DatasetSHA256, func(entry DatasetCase) error {
		metadata = append(metadata, Case{QuestionID: entry.QuestionID, QuestionType: entry.QuestionType})
		return nil
	})
	if err != nil {
		return LoadedDataset{}, err
	}
	selection, err := Select(profile, metadata)
	if err != nil {
		return LoadedDataset{}, err
	}

	wanted := make(map[string]struct{}, len(selection.Cases))
	for _, selected := range selection.Cases {
		wanted[selected.QuestionID] = struct{}{}
	}
	if _, err := source.Seek(0, io.SeekStart); err != nil {
		return LoadedDataset{}, fmt.Errorf("rewind LongMemEval dataset: %w", err)
	}
	rows := make(map[string]DatasetCase, len(wanted))
	err = scanVerifiedDataset(ctx, source, profile.DatasetSHA256, func(entry DatasetCase) error {
		if _, ok := wanted[entry.QuestionID]; ok {
			if err := validateDatasetCase(entry); err != nil {
				return err
			}
			rows[entry.QuestionID] = entry
		}
		return nil
	})
	if err != nil {
		return LoadedDataset{}, err
	}
	if len(rows) != len(wanted) {
		return LoadedDataset{}, fmt.Errorf("loaded %d of %d selected LongMemEval rows", len(rows), len(wanted))
	}
	return LoadedDataset{
		Revision:  profile.DatasetRevision,
		SHA256:    profile.DatasetSHA256,
		Selection: selection,
		selected:  rows,
	}, nil
}

// scanVerifiedDataset independently applies the source-size ceiling and frozen
// digest to each pass. In particular, the selected full rows are never returned
// under a digest computed from earlier bytes supplied by a mutable ReadSeeker.
func scanVerifiedDataset(
	ctx context.Context,
	source io.Reader,
	expectedSHA256 string,
	visit func(DatasetCase) error,
) error {
	hash := sha256.New()
	limited := &hardLimitReader{reader: source, remaining: maxDatasetBytes}
	if err := scanDataset(ctx, io.TeeReader(limited, hash), visit); err != nil {
		return err
	}
	actualSHA256 := hex.EncodeToString(hash.Sum(nil))
	if actualSHA256 != expectedSHA256 {
		return fmt.Errorf("LongMemEval dataset SHA-256 mismatch: got %s", actualSHA256)
	}
	return nil
}

func scanDataset(ctx context.Context, source io.Reader, visit func(DatasetCase) error) error {
	decoder := json.NewDecoder(&contextReader{ctx: ctx, reader: source})
	token, err := decoder.Token()
	if err != nil {
		return fmt.Errorf("decode LongMemEval dataset root: %w", err)
	}
	delim, ok := token.(json.Delim)
	if !ok || delim != '[' {
		return errors.New("LongMemEval dataset root must be an array")
	}
	index := 0
	for decoder.More() {
		var entry DatasetCase
		if err := decoder.Decode(&entry); err != nil {
			return fmt.Errorf("decode LongMemEval dataset entry %d: %w", index, err)
		}
		if entry.QuestionID == "" || entry.QuestionType == "" {
			return fmt.Errorf("LongMemEval dataset entry %d lacks selection metadata", index)
		}
		if err := visit(entry); err != nil {
			return err
		}
		index++
	}
	if _, err := decoder.Token(); err != nil {
		return fmt.Errorf("close LongMemEval dataset array: %w", err)
	}
	var trailing json.RawMessage
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return errors.New("LongMemEval dataset has trailing JSON content")
		}
		return fmt.Errorf("decode LongMemEval dataset trailer: %w", err)
	}
	if index == 0 {
		return errors.New("LongMemEval dataset contains no cases")
	}
	return nil
}

func validateDatasetCase(entry DatasetCase) error {
	if entry.QuestionID == "" || entry.QuestionType == "" || entry.Question == "" ||
		entry.QuestionDate == "" {
		return fmt.Errorf("selected LongMemEval case %q lacks required fields", entry.QuestionID)
	}
	if len(entry.HaystackSessionIDs) != len(entry.HaystackDates) ||
		len(entry.HaystackSessionIDs) != len(entry.HaystackSessions) {
		return fmt.Errorf("selected LongMemEval case %q has inconsistent history arrays", entry.QuestionID)
	}
	return nil
}

type contextReader struct {
	ctx    context.Context
	reader io.Reader
}

func (r *contextReader) Read(buffer []byte) (int, error) {
	if err := r.ctx.Err(); err != nil {
		return 0, err
	}
	return r.reader.Read(buffer)
}

type hardLimitReader struct {
	reader    io.Reader
	remaining int64
}

func (r *hardLimitReader) Read(buffer []byte) (int, error) {
	if r.remaining == 0 {
		var probe [1]byte
		if count, err := r.reader.Read(probe[:]); count > 0 || err == nil {
			return 0, errors.New("LongMemEval dataset exceeds hard size limit")
		} else {
			return 0, err
		}
	}
	if int64(len(buffer)) > r.remaining {
		buffer = buffer[:r.remaining]
	}
	count, err := r.reader.Read(buffer)
	r.remaining -= int64(count)
	return count, err
}
