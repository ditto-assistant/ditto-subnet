// ditto-submission-builder performs one credential-minimal image build.
//
// The only authority it receives is an expiring token bound by Platform to one
// screening attempt and one object key. It never receives provider, cloud,
// registry, screener, controller, GitHub, or validator credentials.
package main

import (
	"archive/tar"
	"bufio"
	"bytes"
	"compress/gzip"
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
	"os/signal"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	maxSourceBytes      = 20 * 1024 * 1024
	maxOutputBytes      = int64(4 * 1024 * 1024 * 1024)
	maxBuildLogBytes    = 64 * 1024
	successHoldDuration = 30 * time.Minute
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
	ImageID         string `json:"image_id,omitempty"`
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
	case "KANIKO", "BUILDKIT", "BUILDKIT_LOCAL_CARGO_DEPENDENCY_MISSING":
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
	engine := os.Getenv("DITTO_BUILD_ENGINE")
	exitAfterComplete := exitAfterCompleteRequested()
	os.Unsetenv("DITTO_BUILD_JOB_TOKEN")
	os.Unsetenv("DITTO_BUILD_ENGINE")
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
	cmd, imageArchive, closeInput, err := imageBuildCommand(
		ctx, engine, source.ImageRef, "/workspace/source.tar.gz",
	)
	if err != nil {
		return stageFailure(buildFailureStage(engine, ""), err)
	}
	if closeInput != nil {
		defer closeInput()
	}
	cmd.Env = sanitizedEnvironment()
	var buildLog limitedBuffer
	cmd.Stdout = io.MultiWriter(os.Stdout, &buildLog)
	cmd.Stderr = io.MultiWriter(os.Stderr, &buildLog)
	if err := cmd.Run(); err != nil {
		return stageFailure(
			buildFailureStage(engine, buildLog.String()),
			fmt.Errorf("image build failed: %w", err),
		)
	}
	if engine == "docker" {
		save := exec.CommandContext(
			ctx, "docker", "image", "save", "--output="+imageArchive, source.ImageRef,
		)
		save.Env = sanitizedEnvironment()
		save.Stdout = os.Stdout
		save.Stderr = os.Stderr
		if err := save.Run(); err != nil {
			return stageFailure("ARCHIVE", fmt.Errorf("docker image save failed: %w", err))
		}
	}

	imageID, err := configDigestFromDockerSave(imageArchive)
	if err != nil {
		return stageFailure("ARCHIVE", err)
	}
	outputSHA, outputSize, err := hashBounded(imageArchive, maxOutputBytes)
	if err != nil {
		return stageFailure("ARCHIVE", err)
	}
	payload := uploadRequest{
		OutputSHA256:    outputSHA,
		OutputSizeBytes: outputSize,
		ImageID:         imageID,
	}
	var upload uploadResponse
	if err := jobJSON(client, http.MethodPost, base+"/upload", token, payload, &upload); err != nil {
		return stageFailure("UPLOAD", err)
	}
	uploadURL, err := decodeURL(upload.UploadURLB64)
	if err != nil {
		return stageFailure("UPLOAD", err)
	}
	if err := uploadFile(client, uploadURL, imageArchive, upload.RequiredHeaders); err != nil {
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
	if !exitAfterComplete {
		holdUntilDeleted()
	}
	return nil
}

// limitedBuffer keeps untrusted build output out of the durable log path. Its
// sole use is to map a known, non-secret compiler condition to a bounded
// miner-private diagnostic; raw output is never sent to Platform or serial.
type limitedBuffer struct {
	mu    sync.Mutex
	value bytes.Buffer
}

func (b *limitedBuffer) Write(value []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	originalLength := len(value)
	if len(value) >= maxBuildLogBytes {
		b.value.Reset()
		_, _ = b.value.Write(value[len(value)-maxBuildLogBytes:])
		return originalLength, nil
	}
	if overflow := b.value.Len() + len(value) - maxBuildLogBytes; overflow > 0 {
		tail := append([]byte(nil), b.value.Bytes()[overflow:]...)
		b.value.Reset()
		_, _ = b.value.Write(tail)
	}
	_, _ = b.value.Write(value)
	return originalLength, nil
}

func (b *limitedBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.value.String()
}

func (b *limitedBuffer) Len() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.value.Len()
}

func buildFailureStage(engine, output string) string {
	if engine != "docker" {
		return "KANIKO"
	}
	if strings.Contains(output, "failed to load source for dependency") &&
		strings.Contains(output, "Cargo.toml") &&
		strings.Contains(output, "No such file or directory") {
		return "BUILDKIT_LOCAL_CARGO_DEPENDENCY_MISSING"
	}
	return "BUILDKIT"
}

func imageBuildCommand(
	ctx context.Context, engine, imageRef, sourceArchive string,
) (*exec.Cmd, string, func() error, error) {
	if engine == "docker" {
		archive, err := os.Open(sourceArchive)
		if err != nil {
			return nil, "", nil, err
		}
		output := "/workspace/image.tar"
		cmd := exec.CommandContext(
			ctx,
			"docker",
			"buildx",
			"build",
			"--progress=plain",
			"--tag="+imageRef,
			"--load",
			"-",
		)
		cmd.Stdin = archive
		return cmd, output, archive.Close, nil
	}
	if engine != "" && engine != "kaniko" {
		return nil, "", nil, errors.New("invalid build engine")
	}
	output := "/kaniko/image.tar"
	cmd := exec.CommandContext(
		ctx,
		"/kaniko/executor",
		"--context=tar://"+sourceArchive,
		"--dockerfile=Dockerfile",
		"--destination="+imageRef,
		// Cloud Run egress shares Google IPs, so unauthenticated Docker Hub
		// base-image pulls exhaust the anonymous rate limit and fail the
		// KANIKO stage. mirror.gcr.io serves the cached copy and Kaniko
		// falls back to the original registry on a miss.
		"--registry-mirror=mirror.gcr.io",
		"--no-push",
		"--no-push-cache",
		"--cache=false",
		"--ignore-path=/workspace",
		"--ignore-path=/etc/resolv.conf",
		"--ignore-path=/etc/hosts",
		"--tar-path="+output,
		"--digest-file=/kaniko/manifest-digest",
		"--verbosity=info",
	)
	return cmd, output, nil, nil
}

func exitAfterCompleteRequested() bool {
	requested := os.Getenv("DITTO_BUILD_EXIT_AFTER_COMPLETE") == "1"
	os.Unsetenv("DITTO_BUILD_EXIT_AFTER_COMPLETE")
	return requested
}

// Rentals are persistent: if PID 1 exits, Targon restarts the container.
// A restart of a consumed job crash-loops and stays billed until DELETE.
// Stay up until the controller DELETE delivers SIGTERM, or the hold cap.
func holdUntilDeleted() {
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	timer := time.NewTimer(successHoldDuration)
	defer timer.Stop()
	select {
	case <-signals:
	case <-timer.C:
	}
}

func sanitizedEnvironment() []string {
	result := make([]string, 0, len(os.Environ()))
	for _, item := range os.Environ() {
		name, _, _ := strings.Cut(item, "=")
		if name == "DITTO_BUILD_EXIT_AFTER_COMPLETE" ||
			name == "DITTO_BUILD_JOB_TOKEN" || strings.Contains(name, "TOKEN") ||
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

func configDigestFromDockerSave(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	buffered := bufio.NewReader(file)
	magic, err := buffered.Peek(2)
	if err != nil {
		return "", err
	}
	var source io.Reader = buffered
	if len(magic) == 2 && magic[0] == 0x1f && magic[1] == 0x8b {
		unzipped, err := gzip.NewReader(buffered)
		if err != nil {
			return "", err
		}
		defer unzipped.Close()
		source = unzipped
	}
	archive := tar.NewReader(source)
	hashed := map[string]string{}
	var manifest []struct {
		Config string `json:"Config"`
	}
	for {
		header, err := archive.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return "", err
		}
		name := strings.TrimPrefix(header.Name, "./")
		if name == "manifest.json" {
			if header.Size <= 0 || header.Size > 1<<20 {
				return "", errors.New("manifest.json has invalid size")
			}
			raw, err := io.ReadAll(io.LimitReader(archive, header.Size+1))
			if err != nil {
				return "", err
			}
			if int64(len(raw)) != header.Size {
				return "", errors.New("manifest.json truncated")
			}
			if err := json.Unmarshal(raw, &manifest); err != nil {
				return "", err
			}
			continue
		}
		digestHex := namedConfigDigest(name)
		if digestHex == "" || header.Size <= 0 || header.Size > 4<<20 {
			continue
		}
		hasher := sha256.New()
		written, err := io.Copy(hasher, io.LimitReader(archive, header.Size+1))
		if err != nil {
			return "", err
		}
		if written != header.Size {
			return "", errors.New("image config truncated")
		}
		got := hex.EncodeToString(hasher.Sum(nil))
		if got == digestHex {
			hashed[name] = digestHex
		}
	}
	if len(manifest) != 1 {
		return "", errors.New("archive must contain exactly one image")
	}
	configName := strings.TrimPrefix(manifest[0].Config, "./")
	digestHex, ok := hashed[configName]
	if !ok {
		return "", errors.New("docker-save config digest is missing")
	}
	return "sha256:" + digestHex, nil
}

func namedConfigDigest(name string) string {
	name = strings.TrimPrefix(name, "./")
	if strings.HasSuffix(name, ".json") {
		stem := strings.TrimSuffix(name, ".json")
		if !strings.Contains(stem, "/") && digestPattern.MatchString(stem) {
			return stem
		}
	}
	const blobPrefix = "blobs/sha256/"
	if strings.HasPrefix(name, blobPrefix) {
		digest := name[len(blobPrefix):]
		if digestPattern.MatchString(digest) {
			return digest
		}
	}
	// go-containerregistry (Kaniko --tar-path) names the config "sha256:<hex>".
	const algoPrefix = "sha256:"
	if strings.HasPrefix(name, algoPrefix) && !strings.Contains(name, "/") {
		digest := name[len(algoPrefix):]
		if digestPattern.MatchString(digest) {
			return digest
		}
	}
	return ""
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
