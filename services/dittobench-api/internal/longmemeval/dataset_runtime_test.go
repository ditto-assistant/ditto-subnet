package longmemeval

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"strings"
	"testing"
)

func TestLoadSelectedDatasetVerifiesDigestAndBalancedSelection(t *testing.T) {
	profile, raw, dataset := runtimeFixture(t)
	if dataset.Revision != profile.DatasetRevision || dataset.SHA256 != profile.DatasetSHA256 {
		t.Fatal("loaded dataset did not retain frozen provenance")
	}
	if len(dataset.Selection.Cases) != 12 || len(dataset.selected) != 12 {
		t.Fatalf("selected=%d loaded=%d", len(dataset.Selection.Cases), len(dataset.selected))
	}
	for _, capability := range Capabilities() {
		count := 0
		for _, selected := range dataset.Selection.Cases {
			if selected.Capability == capability {
				count++
			}
		}
		if count != 2 {
			t.Fatalf("capability %q selected %d cases", capability, count)
		}
	}

	loadedAgain, err := LoadSelectedDataset(context.Background(), bytes.NewReader(raw), profile)
	if err != nil {
		t.Fatal(err)
	}
	if loadedAgain.Selection.CaseSetDigest != dataset.Selection.CaseSetDigest {
		t.Fatal("same pinned bytes produced a different case set")
	}
}

func TestLoadSelectedDatasetRejectsDigestMismatchBeforeReturningRows(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	raw = append(raw, '\n')
	_, err := LoadSelectedDataset(context.Background(), bytes.NewReader(raw), profile)
	requireErrorContains(t, err, "SHA-256 mismatch")
}

func TestLoadSelectedDatasetRejectsMutableSecondPassBytes(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	mutatedCases := runtimeDatasetCases()
	mutatedCases[0].Answer = "substituted-private-answer"
	mutated, err := json.Marshal(mutatedCases)
	if err != nil {
		t.Fatal(err)
	}
	source := &twoPassReadSeeker{
		first: raw,
		second: func() io.Reader {
			return bytes.NewReader(mutated)
		},
	}
	_, err = LoadSelectedDataset(context.Background(), source, profile)
	requireErrorContains(t, err, "SHA-256 mismatch")
	if source.seeks != 2 {
		t.Fatalf("dataset source seek count=%d, want 2", source.seeks)
	}
}

func TestLoadSelectedDatasetRejectsOversizedSecondPass(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	source := &twoPassReadSeeker{
		first: raw,
		second: func() io.Reader {
			return io.MultiReader(
				bytes.NewReader(raw),
				&paddingReader{remaining: maxDatasetBytes + 1 - int64(len(raw)), value: ' '},
			)
		},
	}
	_, err := LoadSelectedDataset(context.Background(), source, profile)
	requireErrorContains(t, err, "hard size limit")
	if source.seeks != 2 {
		t.Fatalf("dataset source seek count=%d, want 2", source.seeks)
	}
}

func TestLoadSelectedDatasetRejectsMalformedJSONShapes(t *testing.T) {
	profile := loadTestProfile(t)
	profile.CasesPerCapability = 2
	for name, raw := range map[string][]byte{
		"object root":    []byte(`{"question_id":"q"}`),
		"empty array":    []byte(`[]`),
		"trailing value": []byte(`[] {}`),
		"broken entry":   []byte(`[{"question_id":1}]`),
	} {
		t.Run(name, func(t *testing.T) {
			digest := sha256.Sum256(raw)
			profile.DatasetSHA256 = hex.EncodeToString(digest[:])
			_, err := LoadSelectedDataset(context.Background(), bytes.NewReader(raw), profile)
			if err == nil {
				t.Fatal("malformed dataset accepted")
			}
		})
	}
}

func TestLoadSelectedDatasetNormalizesOfficialNumericAnswer(t *testing.T) {
	values := runtimeDatasetCases()
	raw, err := json.Marshal(values)
	if err != nil {
		t.Fatal(err)
	}
	from := []byte(`"answer":"passphrase-00-00"`)
	to := []byte(`"answer":3`)
	raw = bytes.Replace(raw, from, to, 1)
	if bytes.Contains(raw, from) {
		t.Fatal("numeric answer fixture replacement did not apply")
	}
	profile := loadTestProfile(t)
	profile.CasesPerCapability = 2
	digest := sha256.Sum256(raw)
	profile.DatasetSHA256 = hex.EncodeToString(digest[:])
	dataset, err := LoadSelectedDataset(context.Background(), bytes.NewReader(raw), profile)
	if err != nil {
		t.Fatal(err)
	}
	if answer := dataset.selected["private-extract-00"].Answer; answer != "3" {
		t.Fatalf("normalized answer=%q, want 3", answer)
	}
	canonical, err := json.Marshal(dataset.selected["private-extract-00"])
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(canonical, []byte(`"answer":"3"`)) {
		t.Fatalf("canonical numeric answer was not normalized as text: %s", canonical)
	}
}

func TestLoadSelectedDatasetRejectsNonTextAnswerShapes(t *testing.T) {
	for name, replacement := range map[string]string{
		"null":    `null`,
		"boolean": `true`,
		"array":   `[]`,
		"object":  `{}`,
	} {
		t.Run(name, func(t *testing.T) {
			values := runtimeDatasetCases()
			raw, err := json.Marshal(values)
			if err != nil {
				t.Fatal(err)
			}
			raw = bytes.Replace(
				raw,
				[]byte(`"answer":"passphrase-00-00"`),
				[]byte(`"answer":`+replacement),
				1,
			)
			profile := loadTestProfile(t)
			profile.CasesPerCapability = 2
			digest := sha256.Sum256(raw)
			profile.DatasetSHA256 = hex.EncodeToString(digest[:])
			_, err = LoadSelectedDataset(context.Background(), bytes.NewReader(raw), profile)
			requireErrorContains(t, err, "answer must be a string or number")
		})
	}
}

func TestLoadSelectedDatasetRejectsDuplicateAndUnderfilledStrata(t *testing.T) {
	base := runtimeDatasetCases()
	for name, mutate := range map[string]func([]DatasetCase) []DatasetCase{
		"duplicate id": func(values []DatasetCase) []DatasetCase {
			values[1].QuestionID = values[0].QuestionID
			return values
		},
		"underfilled": func(values []DatasetCase) []DatasetCase {
			return values[1:]
		},
	} {
		t.Run(name, func(t *testing.T) {
			values := append([]DatasetCase(nil), base...)
			values = mutate(values)
			raw, err := json.Marshal(values)
			if err != nil {
				t.Fatal(err)
			}
			profile := loadTestProfile(t)
			profile.CasesPerCapability = 2
			digest := sha256.Sum256(raw)
			profile.DatasetSHA256 = hex.EncodeToString(digest[:])
			if _, err := LoadSelectedDataset(context.Background(), bytes.NewReader(raw), profile); err == nil {
				t.Fatal("invalid selection population accepted")
			}
		})
	}
}

func TestLoadSelectedDatasetValidatesSelectedFullRows(t *testing.T) {
	for name, mutate := range map[string]func(*DatasetCase){
		"empty question":      func(value *DatasetCase) { value.Question = "" },
		"empty date":          func(value *DatasetCase) { value.QuestionDate = "" },
		"misaligned sessions": func(value *DatasetCase) { value.HaystackDates = nil },
	} {
		t.Run(name, func(t *testing.T) {
			values := runtimeDatasetCases()
			mutate(&values[0])
			raw, err := json.Marshal(values)
			if err != nil {
				t.Fatal(err)
			}
			profile := loadTestProfile(t)
			profile.CasesPerCapability = 2
			digest := sha256.Sum256(raw)
			profile.DatasetSHA256 = hex.EncodeToString(digest[:])
			if _, err := LoadSelectedDataset(context.Background(), bytes.NewReader(raw), profile); err == nil {
				t.Fatal("malformed selected row accepted")
			}
		})
	}
}

func TestLoadSelectedDatasetHonorsCancellation(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err := LoadSelectedDataset(ctx, bytes.NewReader(raw), profile)
	if err == nil || !strings.Contains(err.Error(), context.Canceled.Error()) {
		t.Fatalf("cancelled load error=%v", err)
	}
}

func TestLoadSelectedDatasetRequiresSeekableSourceAndValidProfile(t *testing.T) {
	profile, raw, _ := runtimeFixture(t)
	if _, err := LoadSelectedDataset(context.Background(), nil, profile); err == nil {
		t.Fatal("nil source accepted")
	}
	broken := profile
	broken.DatasetSHA256 = "bad"
	if _, err := LoadSelectedDataset(context.Background(), bytes.NewReader(raw), broken); err == nil {
		t.Fatal("invalid profile accepted")
	}
	reader := &failingSeeker{Reader: bytes.NewReader(raw)}
	if _, err := LoadSelectedDataset(context.Background(), reader, profile); err == nil {
		t.Fatal("seek failure accepted")
	}
}

func TestOfficialCleanedDatasetPinIsExact(t *testing.T) {
	profile := loadTestProfile(t)
	if !IsOfficialCleanedDataset(profile) {
		t.Fatal("shipped shadow profile lost exact cleaned dataset pin")
	}
	profile.DatasetRevision += "-changed"
	if IsOfficialCleanedDataset(profile) {
		t.Fatal("changed dataset revision retained official identity")
	}
}

func TestHardLimitReaderAllowsExactBoundaryAndRejectsExcess(t *testing.T) {
	exact := &hardLimitReader{reader: strings.NewReader("abcd"), remaining: 4}
	raw, err := io.ReadAll(exact)
	if err != nil || string(raw) != "abcd" {
		t.Fatalf("exact boundary raw=%q err=%v", raw, err)
	}
	over := &hardLimitReader{reader: strings.NewReader("abcde"), remaining: 4}
	if _, err := io.ReadAll(over); err == nil {
		t.Fatal("oversized stream accepted")
	}
}

type failingSeeker struct{ io.Reader }

func (*failingSeeker) Seek(int64, int) (int64, error) { return 0, io.ErrUnexpectedEOF }

type twoPassReadSeeker struct {
	first   []byte
	second  func() io.Reader
	current io.Reader
	seeks   int
}

func (r *twoPassReadSeeker) Read(buffer []byte) (int, error) {
	if r.current == nil {
		return 0, errors.New("two-pass test reader was not positioned")
	}
	return r.current.Read(buffer)
}

func (r *twoPassReadSeeker) Seek(offset int64, whence int) (int64, error) {
	if offset != 0 || whence != io.SeekStart {
		return 0, errors.New("two-pass test reader only supports rewind")
	}
	r.seeks++
	if r.seeks == 1 {
		r.current = bytes.NewReader(r.first)
	} else {
		r.current = r.second()
	}
	return 0, nil
}

type paddingReader struct {
	remaining int64
	value     byte
}

func (r *paddingReader) Read(buffer []byte) (int, error) {
	if r.remaining == 0 {
		return 0, io.EOF
	}
	count := int64(len(buffer))
	if count > r.remaining {
		count = r.remaining
	}
	for index := int64(0); index < count; index++ {
		buffer[index] = r.value
	}
	r.remaining -= count
	return int(count), nil
}
