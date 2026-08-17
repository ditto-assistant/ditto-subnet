// ditto-submission-builder performs one credential-minimal Kaniko build.
//
// The only authority it receives is an expiring token bound by Platform to one
// screening attempt and one object key. It never receives provider, cloud,
// registry, screener, controller, GitHub, or validator credentials.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const (
	maxSourceBytes      = 20 * 1024 * 1024
	maxOutputBytes      = int64(4 * 1024 * 1024 * 1024)
	successHoldDuration = 90 * time.Second
)

var (
	buildIDPattern  = regexp.MustCompile(`^[0-9a-f-]{36}$`)
	digestPattern   = regexp.MustCompile(`^[0-9a-f]{64}$`)
	imageRefPattern = regexp.MustCompile(`^ditto-screen/[0-9a-f-]{73}:latest$`)
)

type sourceResponse struct {
	SourceURLB64   string `json:"source_url_b64"`
	ArtifactSHA256 string `json:"artifact_sha256"`
	ImageRef       string `json:"image_ref"`
}

type uploadRequest struct {
	OutputSHA256    string `json:"output_sha256"`
	OutputSizeBytes int64  `json:"output_size_bytes"`
}

type uploadResponse struct {
	UploadURLB64    string            `json:"upload_url_b64"`
	RequiredHeaders map[string]string `json:"required_headers"`
}

type stagedError struct {
	stage string
	err   error
}

func (e *stagedError) Error() string { return e.err.Error() }
func (e *stagedError) Unwrap() error { return e.err }

func stageFailure(stage string, err error) error {
	if err == nil {
		return nil
	}
	return &stagedError{stage: stage, err: err}
}

func failureStageName(err error) string {
	var failure *stagedError
	if errors.As(err, &failure) {
		return failure.stage
	}
	return "CONTRACT"
}

func failureExitCode(stage string) int {
	switch stage {
	case "SOURCE":
		return 71
	case "KANIKO":
		return 72
	case "ARCHIVE":
		return 73
	case "UPLOAD":
		return 74
	case "COMPLETE":
		return 75
	default:
		return 76
	}
}

func main() {
	if err := run(); err != nil {
		stage := failureStageName(err)
		// Public-safe machine-readable classification only. Never print the
		// underlying error: submitted build output may contain private source or
		// capability-bearing URLs.
		fmt.Fprintf(os.Stderr, "DITTO_SUBMISSION_BUILD_FAILED=%s\n", stage)
		os.Exit(failureExitCode(stage))
	}
}

func run() error {
	platform := strings.TrimRight(os.Getenv("DITTO_PLATFORM_URL"), "/")
	buildID := os.Getenv("DITTO_BUILD_ID")
	token := os.Getenv("DITTO_BUILD_JOB_TOKEN")
	os.Unsetenv("DITTO_BUILD_JOB_TOKEN")
	if platform == "" || !strings.HasPrefix(platform, "https://") {
		return errors.New("invalid platform URL")
	}
	if !buildIDPattern.MatchString(buildID) || len(token) < 43 {
		return errors.New("invalid build identity")
	}

	client := &http.Client{Timeout: 20 * time.Minute}
	base := platform + "/api/v1/screener/submission-image-builds/" + buildID
	var source sourceResponse
	if err := jobJSON(client, http.MethodGet, base+"/source", token, nil, &source); err != nil {
		return stageFailure("SOURCE", err)
	}
	if !digestPattern.MatchString(source.ArtifactSHA256) ||
		!imageRefPattern.MatchString(source.ImageRef) {
		return stageFailure("SOURCE", errors.New("invalid source contract"))
	}
	sourceURL, err := decodeURL(source.SourceURLB64)
	if err != nil {
		return stageFailure("SOURCE", err)
	}
	if err := downloadVerified(client, sourceURL, "/workspace/source.tar.gz", source.ArtifactSHA256); err != nil {
		return stageFailure("SOURCE", err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Minute)
	defer cancel()
	cmd := exec.CommandContext(
		ctx,
		"/kaniko/executor",
		"--context=tar:///workspace/source.tar.gz",
		"--dockerfile=Dockerfile",
		"--destination="+source.ImageRef,
		"--no-push",
		"--no-push-cache",
		"--cache=false",
		"--tar-path=/workspace/image.tar",
		"--digest-file=/workspace/manifest-digest",
		"--verbosity=info",
	)
	cmd.Env = sanitizedEnvironment()
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		return stageFailure("KANIKO", fmt.Errorf("kaniko build failed: %w", err))
	}

	outputSHA, outputSize, err := hashBounded("/workspace/image.tar", maxOutputBytes)
	if err != nil {
		return stageFailure("ARCHIVE", err)
	}
	payload := uploadRequest{OutputSHA256: outputSHA, OutputSizeBytes: outputSize}
	var upload uploadResponse
	if err := jobJSON(client, http.MethodPost, base+"/upload", token, payload, &upload); err != nil {
		return stageFailure("UPLOAD", err)
	}
	uploadURL, err := decodeURL(upload.UploadURLB64)
	if err != nil {
		return stageFailure("UPLOAD", err)
	}
	if err := uploadFile(client, uploadURL, "/workspace/image.tar", upload.RequiredHeaders); err != nil {
		return stageFailure("UPLOAD", err)
	}
	var complete struct {
		Verified bool `json:"verified"`
	}
	if err := jobJSON(client, http.MethodPost, base+"/complete", token, payload, &complete); err != nil {
		return stageFailure("COMPLETE", err)
	}
	if !complete.Verified {
		return stageFailure("COMPLETE", errors.New("platform did not verify remote image"))
	}
	fmt.Printf("DITTO_SUBMISSION_BUILD_OK=%s:%d\n", outputSHA, outputSize)
	// Rentals restart when PID 1 exits. Hold just long enough for the
	// controller to observe Platform's committed success and DELETE the
	// rental. A longer hold keeps a finished build billed.
	time.Sleep(successHoldDuration)
	return nil
}

func sanitizedEnvironment() []string {
	result := make([]string, 0, len(os.Environ()))
	for _, item := range os.Environ() {
		name, _, _ := strings.Cut(item, "=")
		if name == "DITTO_BUILD_JOB_TOKEN" || strings.Contains(name, "TOKEN") ||
			strings.Contains(name, "SECRET") || strings.Contains(name, "KEY") {
			continue
		}
		result = append(result, item)
	}
	return result
}

func decodeURL(encoded string) (string, error) {
	raw, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return "", errors.New("invalid encoded URL")
	}
	value := string(raw)
	if !strings.HasPrefix(value, "https://") {
		return "", errors.New("non-TLS URL refused")
	}
	return value, nil
}

func jobJSON(client *http.Client, method, url, token string, input, output any) error {
	var body io.Reader
	if input != nil {
		encoded, err := json.Marshal(input)
		if err != nil {
			return err
		}
		body = bytes.NewReader(encoded)
	}
	req, err := http.NewRequest(method, url, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")
	if input != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	response, err := client.Do(req)
	if err != nil {
		return errors.New("platform request failed")
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
		return fmt.Errorf("platform request rejected: HTTP %d", response.StatusCode)
	}
	if output == nil {
		return nil
	}
	return json.NewDecoder(io.LimitReader(response.Body, 64*1024)).Decode(output)
}

func downloadVerified(client *http.Client, url, path, expected string) error {
	response, err := client.Get(url)
	if err != nil {
		return errors.New("source download failed")
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("source download rejected: HTTP %d", response.StatusCode)
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	hash := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(file, hash), io.LimitReader(response.Body, maxSourceBytes+1))
	closeErr := file.Close()
	if copyErr != nil || closeErr != nil || written > maxSourceBytes {
		os.Remove(path)
		return errors.New("source download exceeded its bound")
	}
	if hex.EncodeToString(hash.Sum(nil)) != expected {
		os.Remove(path)
		return errors.New("source digest mismatch")
	}
	return nil
}

func hashBounded(path string, maximum int64) (string, int64, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer file.Close()
	hash := sha256.New()
	size, err := io.Copy(hash, io.LimitReader(file, maximum+1))
	if err != nil || size <= 0 || size > maximum {
		return "", 0, errors.New("image archive exceeded its bound")
	}
	return hex.EncodeToString(hash.Sum(nil)), size, nil
}

func uploadFile(client *http.Client, url, path string, headers map[string]string) error {
	file, err := os.Open(filepath.Clean(path))
	if err != nil {
		return err
	}
	defer file.Close()
	request, err := http.NewRequest(http.MethodPut, url, file)
	if err != nil {
		return err
	}
	for name, value := range headers {
		request.Header.Set(name, value)
	}
	if raw := headers["Content-Length"]; raw != "" {
		size, parseErr := strconv.ParseInt(raw, 10, 64)
		if parseErr != nil || size <= 0 || size > maxOutputBytes {
			return errors.New("invalid upload size")
		}
		request.ContentLength = size
	}
	response, err := client.Do(request)
	if err != nil {
		return errors.New("image upload failed")
	}
	defer response.Body.Close()
	io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return fmt.Errorf("image upload rejected: HTTP %d", response.StatusCode)
	}
	return nil
}
