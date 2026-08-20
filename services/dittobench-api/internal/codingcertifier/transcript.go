package codingcertifier

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"slices"

	"github.com/ditto-assistant/dittobench-api/internal/codingrunner"
)

var requiredToolSequence = []string{
	"repo.read_file",
	"repo.apply_patch",
	"tests.run",
	"git.diff",
}

var errCapabilityProofIncomplete = errors.New("coding capability proof is incomplete")

type transcriptProofWriter struct {
	destination TranscriptWriter
	buffer      []byte
	maximumLine int
	sequence    uint64
	observed    map[string]uint64
}

func newTranscriptProofWriter(destination TranscriptWriter, limits codingrunner.Limits) *transcriptProofWriter {
	return &transcriptProofWriter{
		destination: destination,
		maximumLine: 2*int(codingrunner.MaxToolRequestBytes) + limits.MaxResponseBytes + 8192,
		observed:    make(map[string]uint64, len(requiredToolSequence)),
	}
}

func (writer *transcriptProofWriter) Write(body []byte) (int, error) {
	written, err := writer.destination.Write(body)
	if err != nil {
		return written, err
	}
	if written != len(body) {
		return written, errors.New("coding transcript sink performed a short write")
	}
	writer.buffer = append(writer.buffer, body...)
	for {
		newline := bytes.IndexByte(writer.buffer, '\n')
		if newline < 0 {
			if len(writer.buffer) > writer.maximumLine {
				return written, errors.New("coding transcript event exceeds its signed bound")
			}
			return written, nil
		}
		line := writer.buffer[:newline]
		if len(line) == 0 || len(line) > writer.maximumLine {
			return written, errors.New("coding transcript contains an invalid event line")
		}
		if err := writer.observe(line); err != nil {
			return written, err
		}
		writer.buffer = append(writer.buffer[:0], writer.buffer[newline+1:]...)
	}
}

func (writer *transcriptProofWriter) observe(line []byte) error {
	if err := validateJSONEnvelope(line); err != nil {
		return fmt.Errorf("coding transcript event JSON: %w", err)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(line, &fields); err != nil {
		return errors.New("coding transcript event is invalid")
	}
	for _, field := range []string{"sequence", "name", "error"} {
		if _, present := fields[field]; !present {
			return errors.New("coding transcript event lacks a required field")
		}
	}
	var sequence uint64
	var name string
	if err := json.Unmarshal(fields["sequence"], &sequence); err != nil || sequence != writer.sequence+1 {
		return errors.New("coding transcript event sequence is invalid")
	}
	if err := json.Unmarshal(fields["name"], &name); err != nil || !validIdentifier(name, 128) {
		return errors.New("coding transcript event tool name is invalid")
	}
	writer.sequence = sequence
	if slices.Contains(requiredToolSequence, name) && bytes.Equal(bytes.TrimSpace(fields["error"]), []byte("null")) {
		if _, exists := writer.observed[name]; !exists {
			writer.observed[name] = sequence
		}
	}
	return nil
}

func (writer *transcriptProofWriter) finish(identity codingrunner.TranscriptIdentity) error {
	if len(writer.buffer) != 0 || writer.sequence != identity.Events {
		return errors.New("coding transcript proof does not match its identity")
	}
	previous := uint64(0)
	for _, name := range requiredToolSequence {
		sequence, exists := writer.observed[name]
		if !exists || sequence <= previous {
			return errCapabilityProofIncomplete
		}
		previous = sequence
	}
	return nil
}
