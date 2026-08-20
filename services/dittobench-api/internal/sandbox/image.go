package sandbox

import (
	"archive/tar"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"

	"github.com/ditto-assistant/dittobench-api/internal/netguard"
)

var maxScreenedImageBytes int64 = 8 << 30 // 8 GiB, additionally exact-size pinned
var screenedImageLoadMu sync.Mutex

const (
	ociImageIndexMediaType    = "application/vnd.oci.image.index.v1+json"
	ociImageManifestMediaType = "application/vnd.oci.image.manifest.v1+json"
	ociImageConfigMediaType   = "application/vnd.oci.image.config.v1+json"
	ociLayerMediaType         = "application/vnd.oci.image.layer.v1.tar"
	ociLayerGzipMediaType     = "application/vnd.oci.image.layer.v1.tar+gzip"
	ociLayerZstdMediaType     = "application/vnd.oci.image.layer.v1.tar+zstd"
	dockerManifestMediaType   = "application/vnd.docker.distribution.manifest.v2+json"
	dockerConfigMediaType     = "application/vnd.docker.container.image.v1+json"
	dockerLayerMediaType      = "application/vnd.docker.image.rootfs.diff.tar"
	dockerLayerGzipMediaType  = "application/vnd.docker.image.rootfs.diff.tar.gzip"
	inTotoMediaType           = "application/vnd.in-toto+json"
	maxAttestationDescriptors = 32
	maxAttachmentBlobBytes    = 16 << 20
	maxAttachmentTotalBytes   = 64 << 20
	maxClassicImageLayers     = 1024
)

type dockerSchema2Descriptor struct {
	MediaType string `json:"mediaType"`
	Digest    string `json:"digest"`
	Size      int64  `json:"size"`
}

type dockerSchema2Manifest struct {
	SchemaVersion int                       `json:"schemaVersion"`
	MediaType     string                    `json:"mediaType"`
	Config        dockerSchema2Descriptor   `json:"config"`
	Layers        []dockerSchema2Descriptor `json:"layers"`
}

// ErrScreenedImageUnavailable marks a failure to ACQUIRE the platform-produced
// screened image, as opposed to a failure to VERIFY it. The distinction is the
// entire safety argument for treating one as the validator's fault and the
// other as terminal, so it is a typed sentinel rather than a substring match on
// an error message that can drift.
//
// Wrapped ONLY by conditions that are transient by construction: a network
// transport error, a 5xx/429 from the object store, a local filesystem or
// Docker daemon fault. Each is a property of THIS validator host or THIS
// network path at THIS moment, and a later attempt on another validator can
// legitimately succeed.
//
// Deliberately NOT wrapped by any verification failure -- sha256 / size / image
// id mismatch, a malformed URL, a 4xx other than 429, or archive and
// attestation rejection. Those are deterministic in the bytes the platform
// stored: they will fail identically on every future attempt, so marking them
// no-fault would re-lease the same broken artifact forever. That is exactly the
// loop that let the mnemox family reach 10 attempts against a budget of 2 with
// zero scores while holding validator slots.
var ErrScreenedImageUnavailable = errors.New("screened image unavailable")

// unavailable wraps err as a transient acquisition failure.
func unavailable(err error) error {
	return fmt.Errorf("%w: %w", ErrScreenedImageUnavailable, err)
}

// retriableFetchStatus reports whether an object-store response status could
// plausibly differ on a later attempt. 5xx is server-side by definition and 429
// is explicitly "come back later". Every other non-200 -- 403 on an expired or
// unauthorized grant, 404 on a missing blob -- is treated as terminal: a run
// must not become no-fault because the platform lost or never wrote the object,
// or the ticket would be re-leased indefinitely against a blob that is not
// coming back.
func retriableFetchStatus(status int) bool {
	return status >= 500 || status == http.StatusTooManyRequests
}

type ociDescriptor struct {
	MediaType   string            `json:"mediaType"`
	Digest      string            `json:"digest"`
	Size        int64             `json:"size"`
	Annotations map[string]string `json:"annotations"`
	Platform    *struct {
		Architecture string `json:"architecture"`
		OS           string `json:"os"`
	} `json:"platform"`
}

func (d *LocalDocker) loadScreenedImage(ctx context.Context, src Source, workdir string) (string, string, error) {
	if src.ScreenedImageSize <= 0 || src.ScreenedImageSize > maxScreenedImageBytes {
		return "", "", fmt.Errorf("screened image size %d is outside the allowed range", src.ScreenedImageSize)
	}
	path := filepath.Join(workdir, "screened-image.tar")
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, src.ScreenedImageURL, nil)
	if err != nil {
		return "", "", fmt.Errorf("screened image request: %s", redactURL(err.Error(), src.ScreenedImageURL))
	}
	response, err := netguard.Client(d.AllowPrivate).Do(request)
	if err != nil {
		// Transport: DNS, TLS, connection refused, timeout. Nothing about the
		// artifact; the miner's code has not run and cannot run.
		return "", "", unavailable(fmt.Errorf("screened image fetch: %s", redactURL(err.Error(), src.ScreenedImageURL)))
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		err := fmt.Errorf("screened image fetch: unexpected status %d", response.StatusCode)
		if retriableFetchStatus(response.StatusCode) {
			return "", "", unavailable(err)
		}
		return "", "", err
	}
	file, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
	if err != nil {
		// Local scratch disk: full, read-only, or out of inodes. Host condition.
		return "", "", unavailable(fmt.Errorf("create screened image: %w", err))
	}
	hasher := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(file, hasher), io.LimitReader(response.Body, src.ScreenedImageSize+1))
	closeErr := file.Close()
	if copyErr != nil {
		// Stream truncated mid-download (reset, timeout, disk full).
		return "", "", unavailable(fmt.Errorf("screened image download: %w", copyErr))
	}
	if closeErr != nil {
		return "", "", unavailable(fmt.Errorf("close screened image: %w", closeErr))
	}
	if written != src.ScreenedImageSize {
		return "", "", fmt.Errorf("screened image size mismatch: expected %d got %d", src.ScreenedImageSize, written)
	}
	got := hex.EncodeToString(hasher.Sum(nil))
	if !strings.EqualFold(got, src.ScreenedImageSHA256) {
		return "", "", fmt.Errorf("screened image sha256 mismatch: got %s", got)
	}
	acceptedLoadedIDs := map[string]struct{}{src.ScreenedImageID: {}}
	archiveTagged, err := validateDockerSaveArchiveWithStoreIDs(path, src.ScreenedImageRef, src.ScreenedImageID, acceptedLoadedIDs)
	if err != nil {
		return "", "", fmt.Errorf("invalid screened image archive: %w", err)
	}
	screenedImageLoadMu.Lock()
	defer screenedImageLoadMu.Unlock()
	// Preserve an operator/pre-existing tag if this UUID-like screener ref was
	// somehow already present. Every exit after docker load restores the prior
	// tag (or removes the newly imported one), including partial load failures.
	priorID := d.inspectImageID(ctx, src.ScreenedImageRef)
	loadOut, err := exec.CommandContext(ctx, "docker", "image", "load", "--input", path).CombinedOutput()
	if err != nil {
		_ = d.restoreImageRef(ctx, src.ScreenedImageRef, priorID)
		return "", tail(string(loadOut), 4000), fmt.Errorf("docker image load failed: %w", err)
	}
	cleanupSourceRef := true
	defer func() {
		if cleanupSourceRef {
			_ = d.restoreImageRef(context.Background(), src.ScreenedImageRef, priorID)
		}
	}()
	if !archiveTagged {
		// `docker image save <immutable-id>` intentionally carries no RepoTags.
		// The archive parser proved there is exactly one image and bound its bytes
		// to the signed ID; materialize the expected ref from that immutable ID.
		loadedRef := src.ScreenedImageID
		loadedID := d.inspectImageID(ctx, loadedRef)
		if loadedID == "" {
			for candidate := range acceptedLoadedIDs {
				if candidate == src.ScreenedImageID {
					continue
				}
				if candidateID := d.inspectImageID(ctx, candidate); candidateID != "" {
					loadedRef, loadedID = candidate, candidateID
					break
				}
			}
		}
		if _, ok := acceptedLoadedIDs[loadedID]; !ok {
			return "", tail(string(loadOut), 4000), fmt.Errorf("untagged screened image id mismatch: expected %s got %s", src.ScreenedImageID, loadedID)
		}
		if out, err := exec.CommandContext(ctx, "docker", "image", "tag", loadedRef, src.ScreenedImageRef).CombinedOutput(); err != nil {
			return "", tail(string(loadOut), 4000), fmt.Errorf("tag untagged screened image: %s: %w", strings.TrimSpace(string(out)), err)
		}
	}
	inspectOut, err := exec.CommandContext(ctx, "docker", "image", "inspect", "--format", "{{.Id}}", src.ScreenedImageRef).CombinedOutput()
	if err != nil {
		return "", tail(string(loadOut), 4000), fmt.Errorf("loaded archive missing expected image ref: %w", err)
	}
	loadedID := strings.TrimSpace(string(inspectOut))
	if _, ok := acceptedLoadedIDs[loadedID]; !ok {
		return "", tail(string(loadOut), 4000), fmt.Errorf("screened image id mismatch: expected %s got %s", src.ScreenedImageID, loadedID)
	}
	volumeOut, volumeErr := exec.CommandContext(
		ctx,
		"docker",
		"image",
		"inspect",
		"--format",
		"{{if .Config.Volumes}}declared{{end}}",
		src.ScreenedImageRef,
	).CombinedOutput()
	if volumeErr != nil {
		return "", tail(string(volumeOut), 4000), fmt.Errorf("inspect screened image policy: %w", volumeErr)
	}
	switch strings.TrimSpace(string(volumeOut)) {
	case "":
	case "declared":
		return "", tail(string(volumeOut), 4000), fmt.Errorf("screened image declares writable volumes")
	default:
		return "", tail(string(volumeOut), 4000), fmt.Errorf("inspect screened image policy returned invalid output")
	}
	identity, err := isolatedIdentity()
	if err != nil {
		return "", tail(string(loadOut), 4000), err
	}
	localRef := "dittobench-sub:" + safeTag(src) + "-" + identity
	if out, err := exec.CommandContext(ctx, "docker", "image", "tag", src.ScreenedImageRef, localRef).CombinedOutput(); err != nil {
		return "", tail(string(out), 4000), fmt.Errorf("tag screened image: %s: %w", strings.TrimSpace(string(out)), err)
	}
	// The local request-scoped tag now owns the image. Drop/restore the archive's
	// globally named screener ref immediately; Stop removes localRef after use.
	if err := d.restoreImageRef(ctx, src.ScreenedImageRef, priorID); err != nil {
		return "", tail(string(loadOut), 4000), fmt.Errorf("remove loaded archive ref: %w", err)
	}
	cleanupSourceRef = false
	return localRef, tail(string(loadOut), 2000), nil
}

func validateDockerSaveArchive(path, expectedRef, expectedID string) (bool, error) {
	return validateDockerSaveArchiveWithStoreIDs(path, expectedRef, expectedID, nil)
}

func attemptScopedScreenedImageRef(tag, expectedRef string) bool {
	const prefix = "ditto-screen/"
	const suffix = ":latest"
	tag = strings.TrimPrefix(tag, "docker.io/")
	expectedRef = strings.TrimPrefix(expectedRef, "docker.io/")
	if !strings.HasPrefix(expectedRef, prefix) || !strings.HasSuffix(expectedRef, suffix) {
		return false
	}
	agentID := strings.TrimSuffix(strings.TrimPrefix(expectedRef, prefix), suffix)
	want := prefix + agentID + "-"
	if !strings.HasPrefix(tag, want) || !strings.HasSuffix(tag, suffix) {
		return false
	}
	attemptID := strings.TrimSuffix(strings.TrimPrefix(tag, want), suffix)
	return screenedImageUUID(agentID) && screenedImageUUID(attemptID)
}

func screenedImageUUID(value string) bool {
	if len(value) != 36 {
		return false
	}
	for i := 0; i < 36; i++ {
		c := value[i]
		switch i {
		case 8, 13, 18, 23:
			if c != '-' {
				return false
			}
		default:
			if c >= '0' && c <= '9' || c >= 'a' && c <= 'f' {
				continue
			}
			return false
		}
	}
	return true
}

// validateDockerSaveArchiveWithStoreIDs binds the signed image-config digest
// to every Docker ID the validated archive can acquire after load. Classic
// graphdriver stores expose the config digest as `.Id`; Docker 28's containerd
// image store exposes the deterministic schema-2 manifest digest instead. Both
// identities are derived from the same validated bytes here, before Docker is
// invoked, so accepting the latter does not weaken the signed config boundary.
func validateDockerSaveArchiveWithStoreIDs(path, expectedRef, expectedID string, acceptedLoadedIDs map[string]struct{}) (bool, error) {
	file, err := os.Open(path)
	if err != nil {
		return false, err
	}
	defer file.Close()

	expectedHex := strings.TrimPrefix(expectedID, "sha256:")
	expectedClassicConfig := expectedHex + ".json"
	expectedOCIBlob := "blobs/sha256/" + expectedHex
	reader := tar.NewReader(file)
	var manifestBytes []byte
	var indexBytes []byte
	var expectedImageDescriptorBytes []byte
	verifiedSmallBlobs := make(map[string]bool)
	smallBlobBytes := make(map[string][]byte)
	ociBlobSizes := make(map[string]int64)
	smallBlobCount := 0
	smallBlobTotal := int64(0)
	expectedDigestCount := 0
	expectedDigestOK := false
	expectedConfigSize := int64(0)
	classicLayers := make(map[string]dockerSchema2Descriptor)
	for {
		header, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return false, fmt.Errorf("read tar: %w", err)
		}
		name := strings.TrimPrefix(header.Name, "./")
		if _, ok := ociBlobDigest(name); ok {
			if _, duplicate := ociBlobSizes[name]; duplicate {
				return false, fmt.Errorf("duplicate OCI blob %q", name)
			}
			ociBlobSizes[name] = header.Size
		}
		if name == "manifest.json" {
			if manifestBytes != nil {
				return false, fmt.Errorf("duplicate manifest.json")
			}
			if header.Size <= 0 || header.Size > 1<<20 {
				return false, fmt.Errorf("manifest.json has invalid size %d", header.Size)
			}
			manifestBytes, err = io.ReadAll(io.LimitReader(reader, header.Size+1))
			if err != nil {
				return false, fmt.Errorf("read manifest.json: %w", err)
			}
			if int64(len(manifestBytes)) != header.Size {
				return false, fmt.Errorf("read manifest.json: expected %d bytes got %d", header.Size, len(manifestBytes))
			}
			continue
		}
		if name == "index.json" {
			if indexBytes != nil {
				return false, fmt.Errorf("duplicate index.json")
			}
			if header.Size <= 0 || header.Size > 1<<20 {
				return false, fmt.Errorf("index.json has invalid size %d", header.Size)
			}
			indexBytes, err = io.ReadAll(io.LimitReader(reader, header.Size+1))
			if err != nil {
				return false, fmt.Errorf("read index.json: %w", err)
			}
			if int64(len(indexBytes)) != header.Size {
				return false, fmt.Errorf("read index.json: expected %d bytes got %d", header.Size, len(indexBytes))
			}
			continue
		}
		if name == expectedClassicConfig || name == expectedOCIBlob {
			expectedDigestCount++
			if name == expectedOCIBlob {
				if header.Size <= 0 || header.Size > 4<<20 {
					return false, fmt.Errorf("expected OCI index has invalid size %d", header.Size)
				}
				expectedImageDescriptorBytes, err = io.ReadAll(io.LimitReader(reader, header.Size+1))
				if err != nil {
					return false, fmt.Errorf("read expected OCI descriptor: %w", err)
				}
				if int64(len(expectedImageDescriptorBytes)) != header.Size {
					return false, fmt.Errorf("read expected OCI descriptor: expected %d bytes got %d", header.Size, len(expectedImageDescriptorBytes))
				}
				digest := sha256.Sum256(expectedImageDescriptorBytes)
				expectedDigestOK = strings.EqualFold(hex.EncodeToString(digest[:]), expectedHex)
				verifiedSmallBlobs[name] = expectedDigestOK
				if expectedDigestOK {
					smallBlobBytes[name] = expectedImageDescriptorBytes
				}
			} else {
				expectedConfigSize = header.Size
				hasher := sha256.New()
				written, err := io.Copy(hasher, io.LimitReader(reader, header.Size+1))
				if err != nil {
					return false, fmt.Errorf("read image config: %w", err)
				}
				if written != header.Size {
					return false, fmt.Errorf("read image config: expected %d bytes got %d", header.Size, written)
				}
				expectedDigestOK = strings.EqualFold(hex.EncodeToString(hasher.Sum(nil)), expectedHex)
			}
			continue
		}
		if strings.HasSuffix(name, "/layer.tar") {
			if len(classicLayers) >= maxClassicImageLayers {
				return false, fmt.Errorf("classic image has too many layers")
			}
			if _, duplicate := classicLayers[name]; duplicate {
				return false, fmt.Errorf("duplicate classic image layer %q", name)
			}
			hasher := sha256.New()
			written, err := io.Copy(hasher, io.LimitReader(reader, header.Size+1))
			if err != nil {
				return false, fmt.Errorf("read classic image layer %s: %w", name, err)
			}
			if written != header.Size {
				return false, fmt.Errorf("read classic image layer %s: expected %d bytes got %d", name, header.Size, written)
			}
			classicLayers[name] = dockerSchema2Descriptor{
				MediaType: dockerLayerMediaType,
				Digest:    "sha256:" + hex.EncodeToString(hasher.Sum(nil)),
				Size:      header.Size,
			}
			continue
		}
		if digest, ok := ociBlobDigest(name); ok && header.Size > 0 && header.Size <= maxAttachmentBlobBytes {
			smallBlobCount++
			smallBlobTotal += header.Size
			if smallBlobCount > 256 || smallBlobTotal > maxAttachmentTotalBytes {
				return false, fmt.Errorf("archive contains too many small OCI blobs")
			}
			blob, err := io.ReadAll(io.LimitReader(reader, header.Size+1))
			if err != nil {
				return false, fmt.Errorf("read OCI blob %s: %w", name, err)
			}
			if int64(len(blob)) != header.Size {
				return false, fmt.Errorf("read OCI blob %s: expected %d bytes got %d", name, header.Size, len(blob))
			}
			got := sha256.Sum256(blob)
			verifiedSmallBlobs[name] = hex.EncodeToString(got[:]) == digest
			if verifiedSmallBlobs[name] {
				smallBlobBytes[name] = blob
			}
		}
	}
	if manifestBytes == nil {
		return false, fmt.Errorf("manifest.json is missing")
	}
	var manifest []struct {
		Config   string   `json:"Config"`
		RepoTags []string `json:"RepoTags"`
		Layers   []string `json:"Layers"`
	}
	if err := json.Unmarshal(manifestBytes, &manifest); err != nil {
		return false, fmt.Errorf("decode manifest.json: %w", err)
	}
	if len(manifest) != 1 {
		return false, fmt.Errorf("archive must contain exactly one image")
	}
	archiveTagged := len(manifest[0].RepoTags) == 1 && manifest[0].RepoTags[0] == expectedRef
	if len(manifest[0].RepoTags) != 0 && !archiveTagged {
		// Targon/Kaniko `--tar-path` stamps `--destination`, which Platform
		// binds as the attempt-scoped `ditto-screen/{agent}-{attempt}:latest`.
		// The scoring lane's expected ref is the stable agent identity. Empty
		// tags and the exact agent ref stay canonical; the attempt alias is
		// accepted and retagged, same as an untagged docker save.
		if len(manifest[0].RepoTags) != 1 ||
			!attemptScopedScreenedImageRef(manifest[0].RepoTags[0], expectedRef) {
			return false, fmt.Errorf("archive tags must be empty or exactly the expected image ref %q", expectedRef)
		}
	}
	classic := strings.TrimPrefix(manifest[0].Config, "./") == expectedClassicConfig
	manifestConfig := strings.TrimPrefix(manifest[0].Config, "./")
	if !classic && !verifiedSmallBlobs[manifestConfig] {
		return false, fmt.Errorf("Docker manifest config bytes do not match their OCI digest")
	}
	oci := false
	if !classic && indexBytes != nil {
		primaryMediaType, runnableDigests, err := validatePrimaryOCIImage(
			expectedImageDescriptorBytes,
			expectedID,
			smallBlobBytes,
			ociBlobSizes,
			acceptedLoadedIDs,
		)
		if err != nil {
			return false, err
		}
		var index struct {
			Manifests []ociDescriptor `json:"manifests"`
		}
		if err := json.Unmarshal(indexBytes, &index); err != nil {
			return false, fmt.Errorf("decode index.json: %w", err)
		}
		if len(index.Manifests) == 0 || len(index.Manifests) > 1+maxAttestationDescriptors {
			return false, fmt.Errorf("index.json has an invalid descriptor count %d", len(index.Manifests))
		}
		primaryCount := 0
		for _, descriptor := range index.Manifests {
			if descriptor.Digest == expectedID {
				primaryCount++
				name := strings.TrimPrefix(descriptor.Annotations["io.containerd.image.name"], "docker.io/")
				if descriptor.MediaType != primaryMediaType ||
					(name != expectedRef &&
						!attemptScopedScreenedImageRef(name, expectedRef) &&
						(archiveTagged || name != "")) {
					return false, fmt.Errorf("expected image descriptor has invalid media type or name")
				}
				continue
			}
			if err := validateAttestationDescriptor(descriptor, runnableDigests, smallBlobBytes); err != nil {
				return false, err
			}
		}
		oci = primaryCount == 1
	}
	if !classic && !oci {
		return false, fmt.Errorf("archive index does not bind the expected image id")
	}
	if expectedDigestCount != 1 || !expectedDigestOK {
		return false, fmt.Errorf("archive bytes do not contain the expected image id digest")
	}
	if classic && acceptedLoadedIDs != nil {
		if expectedConfigSize <= 0 || len(manifest[0].Layers) > maxClassicImageLayers {
			return false, fmt.Errorf("classic image config or layer count is invalid")
		}
		layers := make([]dockerSchema2Descriptor, 0, len(manifest[0].Layers))
		for _, layerName := range manifest[0].Layers {
			descriptor, ok := classicLayers[strings.TrimPrefix(layerName, "./")]
			if !ok {
				return false, fmt.Errorf("classic image manifest references missing layer %q", layerName)
			}
			layers = append(layers, descriptor)
		}
		storeManifest, err := json.Marshal(dockerSchema2Manifest{
			SchemaVersion: 2,
			MediaType:     dockerManifestMediaType,
			Config: dockerSchema2Descriptor{
				MediaType: dockerConfigMediaType,
				Digest:    expectedID,
				Size:      expectedConfigSize,
			},
			Layers: layers,
		})
		if err != nil {
			return false, fmt.Errorf("encode classic image store manifest: %w", err)
		}
		digest := sha256.Sum256(storeManifest)
		acceptedLoadedIDs["sha256:"+hex.EncodeToString(digest[:])] = struct{}{}
	}
	return archiveTagged, nil
}

// validatePrimaryOCIImage accepts the two immutable identities emitted by
// current Docker image stores. A provenance-bearing BuildKit result is an OCI
// image index; `--provenance=false` instead makes Docker expose the schema-2
// image manifest itself as `.Id`. Both are content-addressed, single-image
// roots in the surrounding OCI archive and remain bound to expectedID.
func validatePrimaryOCIImage(
	data []byte,
	expectedID string,
	blobs map[string][]byte,
	blobSizes map[string]int64,
	acceptedLoadedIDs map[string]struct{},
) (string, map[string]struct{}, error) {
	var header struct {
		SchemaVersion int    `json:"schemaVersion"`
		MediaType     string `json:"mediaType"`
	}
	if err := json.Unmarshal(data, &header); err != nil {
		return "", nil, fmt.Errorf("decode expected OCI descriptor: %w", err)
	}
	switch header.MediaType {
	case ociImageIndexMediaType:
		runnable, err := validatePrimaryOCIIndex(data, blobs, blobSizes, acceptedLoadedIDs)
		return ociImageIndexMediaType, runnable, err
	case dockerManifestMediaType:
		configID, err := validateRunnableImageManifest(data, blobSizes, blobs)
		if err != nil {
			return "", nil, err
		}
		if acceptedLoadedIDs != nil {
			acceptedLoadedIDs[expectedID] = struct{}{}
			acceptedLoadedIDs[configID] = struct{}{}
		}
		return dockerManifestMediaType, map[string]struct{}{expectedID: {}}, nil
	default:
		return "", nil, fmt.Errorf("expected OCI descriptor has unsupported media type %q", header.MediaType)
	}
}

func validateDockerSchema2ImageManifest(data []byte, blobSizes map[string]int64, verifiedBlobs map[string][]byte) error {
	_, err := validateRunnableImageManifest(data, blobSizes, verifiedBlobs)
	return err
}

// validateRunnableImageManifest verifies the immutable manifest-to-config
// chain and returns the config digest Docker may expose as the local image ID.
// Docker's classic graphdriver store identifies an imported OCI image by this
// config digest, while containerd-backed stores may expose the manifest or
// index digest. All are safe aliases only after the signed archive binds the
// complete chain.
func validateRunnableImageManifest(data []byte, blobSizes map[string]int64, verifiedBlobs map[string][]byte) (string, error) {
	var manifest dockerSchema2Manifest
	if err := json.Unmarshal(data, &manifest); err != nil {
		return "", fmt.Errorf("decode expected image manifest: %w", err)
	}
	if manifest.SchemaVersion != 2 ||
		(manifest.MediaType != dockerManifestMediaType && manifest.MediaType != ociImageManifestMediaType) ||
		!canonicalSHA256Digest(manifest.Config.Digest) || manifest.Config.Size <= 0 ||
		len(manifest.Layers) > maxClassicImageLayers {
		return "", fmt.Errorf("expected image manifest has invalid shape")
	}
	validConfigMediaType := manifest.Config.MediaType == dockerConfigMediaType
	if manifest.MediaType == ociImageManifestMediaType {
		validConfigMediaType = manifest.Config.MediaType == ociImageConfigMediaType
	}
	if !validConfigMediaType {
		return "", fmt.Errorf("expected image manifest has invalid config media type")
	}
	configName := "blobs/sha256/" + strings.TrimPrefix(manifest.Config.Digest, "sha256:")
	if blobSizes[configName] != manifest.Config.Size || verifiedBlobs[configName] == nil {
		return "", fmt.Errorf("expected image manifest references invalid config")
	}
	for _, layer := range manifest.Layers {
		validLayerMediaType := layer.MediaType == dockerLayerMediaType || layer.MediaType == dockerLayerGzipMediaType
		if manifest.MediaType == ociImageManifestMediaType {
			validLayerMediaType = layer.MediaType == ociLayerMediaType ||
				layer.MediaType == ociLayerGzipMediaType ||
				layer.MediaType == ociLayerZstdMediaType
		}
		if !validLayerMediaType || !canonicalSHA256Digest(layer.Digest) || layer.Size <= 0 {
			return "", fmt.Errorf("expected image manifest contains an invalid layer descriptor")
		}
		name := "blobs/sha256/" + strings.TrimPrefix(layer.Digest, "sha256:")
		if blobSizes[name] != layer.Size {
			return "", fmt.Errorf("expected image manifest references a missing or size-mismatched layer")
		}
	}
	return manifest.Config.Digest, nil
}

func validatePrimaryOCIIndex(
	data []byte,
	blobs map[string][]byte,
	blobSizes map[string]int64,
	acceptedLoadedIDs map[string]struct{},
) (map[string]struct{}, error) {
	var index struct {
		SchemaVersion int             `json:"schemaVersion"`
		MediaType     string          `json:"mediaType"`
		Manifests     []ociDescriptor `json:"manifests"`
	}
	if err := json.Unmarshal(data, &index); err != nil {
		return nil, fmt.Errorf("decode expected OCI index: %w", err)
	}
	if index.SchemaVersion != 2 || index.MediaType != ociImageIndexMediaType ||
		len(index.Manifests) == 0 || len(index.Manifests) > 1+maxAttestationDescriptors {
		return nil, fmt.Errorf("expected OCI index has invalid shape")
	}
	runnable := make(map[string]struct{})
	runnableSizes := make(map[string]int64)
	runnableCount := 0
	for _, descriptor := range index.Manifests {
		if descriptor.Annotations["vnd.docker.reference.type"] == "attestation-manifest" {
			continue
		}
		if descriptor.MediaType != ociImageManifestMediaType || descriptor.Size <= 0 ||
			descriptor.Platform == nil || descriptor.Platform.Architecture == "unknown" || descriptor.Platform.OS == "unknown" ||
			!canonicalSHA256Digest(descriptor.Digest) {
			return nil, fmt.Errorf("expected OCI index contains an invalid runnable descriptor")
		}
		runnable[descriptor.Digest] = struct{}{}
		runnableSizes[descriptor.Digest] = descriptor.Size
		runnableCount++
	}
	if runnableCount != 1 || len(runnable) != 1 {
		return nil, fmt.Errorf("expected OCI index must contain exactly one runnable image")
	}
	for digest := range runnable {
		manifestName := "blobs/sha256/" + strings.TrimPrefix(digest, "sha256:")
		manifestBytes := blobs[manifestName]
		if manifestBytes == nil || blobSizes[manifestName] != runnableSizes[digest] {
			return nil, fmt.Errorf("expected OCI index references a missing or size-mismatched runnable manifest")
		}
		configID, err := validateRunnableImageManifest(manifestBytes, blobSizes, blobs)
		if err != nil {
			return nil, err
		}
		if acceptedLoadedIDs != nil {
			acceptedLoadedIDs[digest] = struct{}{}
			acceptedLoadedIDs[configID] = struct{}{}
		}
	}
	for _, descriptor := range index.Manifests {
		if descriptor.Annotations["vnd.docker.reference.type"] != "attestation-manifest" {
			continue
		}
		if err := validateAttestationDescriptor(descriptor, runnable, blobs); err != nil {
			return nil, err
		}
	}
	return runnable, nil
}

func ociBlobDigest(name string) (string, bool) {
	const prefix = "blobs/sha256/"
	if !strings.HasPrefix(name, prefix) {
		return "", false
	}
	digest := strings.TrimPrefix(name, prefix)
	if len(digest) != 64 || digest != strings.ToLower(digest) {
		return "", false
	}
	if _, err := hex.DecodeString(digest); err != nil {
		return "", false
	}
	return digest, true
}

func validateAttestationDescriptor(descriptor ociDescriptor, runnableDigests map[string]struct{}, blobs map[string][]byte) error {
	if descriptor.MediaType != ociImageManifestMediaType || descriptor.Size <= 0 || descriptor.Size > maxAttachmentBlobBytes {
		return fmt.Errorf("index.json contains a non-attestation descriptor besides the expected image")
	}
	reference := ""
	switch {
	case descriptor.Annotations["vnd.docker.reference.type"] == "attestation-manifest":
		if descriptor.Platform == nil || descriptor.Platform.Architecture != "unknown" || descriptor.Platform.OS != "unknown" {
			return fmt.Errorf("Docker attestation descriptor must target unknown/unknown")
		}
		reference = descriptor.Annotations["vnd.docker.reference.digest"]
	case descriptor.Annotations["io.containerd.manifest.subject"] != "":
		if descriptor.Platform != nil && (descriptor.Platform.Architecture != "unknown" || descriptor.Platform.OS != "unknown") {
			return fmt.Errorf("containerd attestation descriptor has a runnable platform")
		}
		reference = descriptor.Annotations["io.containerd.manifest.subject"]
	default:
		return fmt.Errorf("index.json contains a non-attestation descriptor besides the expected image")
	}
	if !canonicalSHA256Digest(descriptor.Digest) || !canonicalSHA256Digest(reference) {
		return fmt.Errorf("index.json attestation descriptor has a malformed digest")
	}
	if _, ok := runnableDigests[reference]; !ok {
		return fmt.Errorf("index.json attestation does not reference the sole runnable image")
	}
	if name := descriptor.Annotations["io.containerd.image.name"]; name != "" {
		return fmt.Errorf("index.json attestation descriptor must not carry an image name")
	}
	return validateAttestationManifest(descriptor, reference, blobs)
}

func validateAttestationManifest(descriptor ociDescriptor, runnableDigest string, blobs map[string][]byte) error {
	manifestPath := "blobs/sha256/" + strings.TrimPrefix(descriptor.Digest, "sha256:")
	manifestBytes := blobs[manifestPath]
	if int64(len(manifestBytes)) != descriptor.Size {
		return fmt.Errorf("attestation manifest bytes are missing or size-mismatched")
	}
	var manifest struct {
		SchemaVersion int             `json:"schemaVersion"`
		MediaType     string          `json:"mediaType"`
		Config        ociDescriptor   `json:"config"`
		Layers        []ociDescriptor `json:"layers"`
	}
	if err := json.Unmarshal(manifestBytes, &manifest); err != nil || manifest.SchemaVersion != 2 ||
		manifest.MediaType != ociImageManifestMediaType || manifest.Config.MediaType != ociImageConfigMediaType ||
		!canonicalSHA256Digest(manifest.Config.Digest) || manifest.Config.Size <= 0 || len(manifest.Layers) != 1 {
		return fmt.Errorf("attached manifest is not a bounded OCI attestation")
	}
	layer := manifest.Layers[0]
	if layer.MediaType != inTotoMediaType || !canonicalSHA256Digest(layer.Digest) || layer.Size <= 0 ||
		layer.Size > maxAttachmentBlobBytes || layer.Annotations["in-toto.io/predicate-type"] == "" {
		return fmt.Errorf("attached manifest does not contain exactly one in-toto layer")
	}
	configBytes := blobs["blobs/sha256/"+strings.TrimPrefix(manifest.Config.Digest, "sha256:")]
	layerBytes := blobs["blobs/sha256/"+strings.TrimPrefix(layer.Digest, "sha256:")]
	if int64(len(configBytes)) != manifest.Config.Size || int64(len(layerBytes)) != layer.Size {
		return fmt.Errorf("attestation config or layer bytes are missing")
	}
	var config struct {
		Architecture string `json:"architecture"`
		OS           string `json:"os"`
	}
	if err := json.Unmarshal(configBytes, &config); err != nil || config.Architecture != "unknown" || config.OS != "unknown" {
		return fmt.Errorf("attestation config is not unknown/unknown")
	}
	var statement struct {
		Subject []struct {
			Digest map[string]string `json:"digest"`
		} `json:"subject"`
		PredicateType string `json:"predicateType"`
	}
	if err := json.Unmarshal(layerBytes, &statement); err != nil || statement.PredicateType != layer.Annotations["in-toto.io/predicate-type"] {
		return fmt.Errorf("in-toto statement predicate does not match its descriptor")
	}
	wantHex := strings.TrimPrefix(runnableDigest, "sha256:")
	for _, subject := range statement.Subject {
		if subject.Digest["sha256"] == wantHex {
			return nil
		}
	}
	return fmt.Errorf("in-toto statement does not bind the runnable image")
}

func canonicalSHA256Digest(value string) bool {
	if !strings.HasPrefix(value, "sha256:") || len(value) != 71 || value != strings.ToLower(value) {
		return false
	}
	_, err := hex.DecodeString(strings.TrimPrefix(value, "sha256:"))
	return err == nil
}

func (d *LocalDocker) inspectImageID(ctx context.Context, ref string) string {
	out, err := exec.CommandContext(ctx, "docker", "image", "inspect", "--format", "{{.Id}}", ref).Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

func (d *LocalDocker) restoreImageRef(ctx context.Context, ref, priorID string) error {
	if priorID == "" {
		out, err := exec.CommandContext(ctx, "docker", "image", "rm", ref).CombinedOutput()
		if err != nil {
			return fmt.Errorf("docker image rm %s: %s: %w", ref, strings.TrimSpace(string(out)), err)
		}
		return nil
	}
	out, err := exec.CommandContext(ctx, "docker", "image", "tag", priorID, ref).CombinedOutput()
	if err != nil {
		return fmt.Errorf("restore docker image ref %s: %s: %w", ref, strings.TrimSpace(string(out)), err)
	}
	return nil
}
