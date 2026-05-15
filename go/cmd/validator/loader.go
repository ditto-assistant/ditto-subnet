package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ToolCallCase mirrors the on-disk JSON shape produced by
// ditto/bench/fixtures/toolcall/*.jsonl, restricted to the subset the
// validator binary needs to feed bittensor.CoreScoreInputs.
type ToolCallCase struct {
	ID            string `json:"id"`
	Category      string `json:"category"`
	Prompt        string `json:"prompt"`
	Domain        string `json:"domain"`
	Visibility    string `json:"visibility"`
	ExpectedTools []struct {
		Name string `json:"name"`
	} `json:"expected_tools"`
}

// RetrievalCase mirrors ditto/bench/fixtures/retrieval/*.jsonl, again
// restricted to the fields the validator binary actually reads.
type RetrievalCase struct {
	ID               string   `json:"id"`
	Category         string   `json:"category"`
	Query            string   `json:"query"`
	UserFixtureID    string   `json:"user_fixture_id"`
	ExpectedPairIDs  []string `json:"expected_pair_ids"`
	ForbiddenPairIDs []string `json:"forbidden_pair_ids"`
	K                int      `json:"k"`
	Visibility       string   `json:"visibility"`
	ExpectNoTools    bool     `json:"expect_no_tools"`
}

// loadFixtures reads every JSONL file under root/toolcall and
// root/retrieval into typed slices. Empty directories are tolerated so
// validators can run --mechanism ditto_core without retrieval fixtures
// shipped.
func loadFixtures(root string) ([]ToolCallCase, []RetrievalCase, error) {
	core, err := loadToolCallCases(filepath.Join(root, "toolcall"))
	if err != nil {
		return nil, nil, fmt.Errorf("toolcall: %w", err)
	}
	retr, err := loadRetrievalCases(filepath.Join(root, "retrieval"))
	if err != nil {
		return nil, nil, fmt.Errorf("retrieval: %w", err)
	}
	return core, retr, nil
}

func loadToolCallCases(dir string) ([]ToolCallCase, error) {
	out := []ToolCallCase{}
	seen := map[string]string{}
	if _, err := os.Stat(dir); os.IsNotExist(err) {
		return out, nil
	}
	files, err := jsonlFiles(dir)
	if err != nil {
		return nil, err
	}
	for _, p := range files {
		if err := forEachJSONL(p, func(line int, raw []byte) error {
			var c ToolCallCase
			if err := json.Unmarshal(raw, &c); err != nil {
				return fmt.Errorf("%s:%d: %w", p, line, err)
			}
			if prev, ok := seen[c.ID]; ok {
				return fmt.Errorf("duplicate toolcall id %q in %s (first seen in %s)", c.ID, p, prev)
			}
			seen[c.ID] = p
			out = append(out, c)
			return nil
		}); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func loadRetrievalCases(dir string) ([]RetrievalCase, error) {
	out := []RetrievalCase{}
	seen := map[string]string{}
	if _, err := os.Stat(dir); os.IsNotExist(err) {
		return out, nil
	}
	files, err := jsonlFiles(dir)
	if err != nil {
		return nil, err
	}
	for _, p := range files {
		if err := forEachJSONL(p, func(line int, raw []byte) error {
			var c RetrievalCase
			if err := json.Unmarshal(raw, &c); err != nil {
				return fmt.Errorf("%s:%d: %w", p, line, err)
			}
			if prev, ok := seen[c.ID]; ok {
				return fmt.Errorf("duplicate retrieval id %q in %s (first seen in %s)", c.ID, p, prev)
			}
			seen[c.ID] = p
			out = append(out, c)
			return nil
		}); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func jsonlFiles(dir string) ([]string, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, err
	}
	out := []string{}
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if strings.HasSuffix(e.Name(), ".jsonl") {
			out = append(out, filepath.Join(dir, e.Name()))
		}
	}
	sort.Strings(out)
	return out, nil
}

func forEachJSONL(path string, fn func(line int, raw []byte) error) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	const maxLine = 4 * 1024 * 1024
	scanner := bufio.NewScanner(f)
	scanner.Buffer(make([]byte, 0, 64*1024), maxLine)
	for i := 1; scanner.Scan(); i++ {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "//") || strings.HasPrefix(line, "#") {
			continue
		}
		if err := fn(i, []byte(line)); err != nil {
			return err
		}
	}
	if err := scanner.Err(); err != nil {
		if err == io.EOF {
			return nil
		}
		return err
	}
	return nil
}

// MinerCommitment is the per-hotkey image record the validator consumes
// from --miner-commitments. The shape mirrors what a future on-chain
// commitment extrinsic will publish (image_repository, image_digest,
// build_metadata) — see anti_gaming.md.
type MinerCommitment struct {
	Image       string            `json:"image"` // "repo@sha256:..." or "repo:tag"
	Env         map[string]string `json:"env,omitempty"`
	Network     string            `json:"network,omitempty"`      // docker --network; default "none"
	ExtraArgs   []string          `json:"extra_args,omitempty"`   // appended to docker run
	CPULimit    string            `json:"cpu_limit,omitempty"`    // docker --cpus
	MemoryLimit string            `json:"memory_limit,omitempty"` // docker --memory
}

// loadCommitments reads the --miner-commitments JSON, which is a
// hotkey -> MinerCommitment map. When SelfTest is set the file is
// replaced by a single in-process echo miner so the binary remains
// runnable without docker or a real chain.
func loadCommitments(cfg Config) (map[string]MinerCommitment, error) {
	if cfg.SelfTest {
		return map[string]MinerCommitment{
			"self-test-hotkey": {Image: ""}, // empty image triggers in-process echo
		}, nil
	}
	raw, err := os.ReadFile(cfg.MinerCommitmentsPath)
	if err != nil {
		return nil, fmt.Errorf("read miner commitments: %w", err)
	}
	out := map[string]MinerCommitment{}
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil, fmt.Errorf("parse miner commitments: %w", err)
	}
	return out, nil
}
