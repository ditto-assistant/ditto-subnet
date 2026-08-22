package sandbox

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const testScreenedImageRef = "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest"

func makeScreenedImageArchive(t *testing.T) ([]byte, string) {
	return makeScreenedImageArchiveVariant(t, []string{testScreenedImageRef}, false)
}

func makeScreenedImageArchiveVariant(t *testing.T, repoTags []string, secondImage bool) ([]byte, string) {
	t.Helper()
	config := []byte(`{"architecture":"amd64","os":"linux"}`)
	configDigest := sha256.Sum256(config)
	configName := hex.EncodeToString(configDigest[:]) + ".json"
	manifestEntries := []map[string]any{{"Config": configName, "RepoTags": repoTags, "Layers": []string{}}}
	files := []struct {
		name string
		body []byte
	}{{configName, config}}
	if secondImage {
		secondConfig := []byte(`{"architecture":"arm64","os":"linux"}`)
		secondDigest := sha256.Sum256(secondConfig)
		secondName := hex.EncodeToString(secondDigest[:]) + ".json"
		manifestEntries = append(manifestEntries, map[string]any{
			"Config": secondName, "RepoTags": []string{"ditto-screen/11111111-1111-4111-8111-111111111111:latest"}, "Layers": []string{},
		})
		files = append(files, struct {
			name string
			body []byte
		}{secondName, secondConfig})
	}
	manifest, err := json.Marshal(manifestEntries)
	if err != nil {
		t.Fatal(err)
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	files = append(files, struct {
		name string
		body []byte
	}{"manifest.json", manifest})
	for _, file := range files {
		if err := writer.WriteHeader(&tar.Header{Name: file.name, Mode: 0o600, Size: int64(len(file.body))}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(file.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return archive.Bytes(), "sha256:" + hex.EncodeToString(configDigest[:])
}

func makeOCIIndexArchive(t *testing.T, mutate func(*[]ociDescriptor)) ([]byte, string) {
	t.Helper()
	config := []byte(`{"architecture":"amd64","os":"linux"}`)
	configDigest := sha256.Sum256(config)
	configName := "blobs/sha256/" + hex.EncodeToString(configDigest[:])
	manifestBytes, err := json.Marshal([]map[string]any{{
		"Config": configName, "RepoTags": nil, "Layers": []string{},
	}})
	if err != nil {
		t.Fatal(err)
	}
	runnableManifest, err := json.Marshal(map[string]any{
		"schemaVersion": 2,
		"mediaType":     ociImageManifestMediaType,
		"config": map[string]any{
			"mediaType": ociImageConfigMediaType,
			"digest":    "sha256:" + hex.EncodeToString(configDigest[:]),
			"size":      len(config),
		},
		"layers": []map[string]any{},
	})
	if err != nil {
		t.Fatal(err)
	}
	runnableSum := sha256.Sum256(runnableManifest)
	runnableDigest := "sha256:" + hex.EncodeToString(runnableSum[:])
	primaryIndex, err := json.Marshal(map[string]any{
		"schemaVersion": 2,
		"mediaType":     ociImageIndexMediaType,
		"manifests": []map[string]any{{
			"mediaType": ociImageManifestMediaType,
			"digest":    runnableDigest,
			"size":      len(runnableManifest),
			"platform":  map[string]string{"architecture": "amd64", "os": "linux"},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	primaryDigest := sha256.Sum256(primaryIndex)
	imageID := "sha256:" + hex.EncodeToString(primaryDigest[:])
	attestationConfig, err := json.Marshal(map[string]any{"architecture": "unknown", "os": "unknown", "config": map[string]any{}})
	if err != nil {
		t.Fatal(err)
	}
	attestationConfigDigest := sha256.Sum256(attestationConfig)
	attestationConfigID := "sha256:" + hex.EncodeToString(attestationConfigDigest[:])
	predicateType := "https://slsa.dev/provenance/v1"
	statement, err := json.Marshal(map[string]any{
		"_type": "https://in-toto.io/Statement/v1", "predicateType": predicateType,
		"subject": []map[string]any{{"digest": map[string]string{"sha256": strings.TrimPrefix(runnableDigest, "sha256:")}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	statementDigest := sha256.Sum256(statement)
	statementID := "sha256:" + hex.EncodeToString(statementDigest[:])
	attestationManifest, err := json.Marshal(map[string]any{
		"schemaVersion": 2, "mediaType": ociImageManifestMediaType,
		"config": map[string]any{"mediaType": ociImageConfigMediaType, "digest": attestationConfigID, "size": len(attestationConfig)},
		"layers": []map[string]any{{
			"mediaType": inTotoMediaType, "digest": statementID, "size": len(statement),
			"annotations": map[string]string{"in-toto.io/predicate-type": predicateType},
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	attestationDigest := sha256.Sum256(attestationManifest)
	attestationID := "sha256:" + hex.EncodeToString(attestationDigest[:])
	descriptors := []ociDescriptor{
		{MediaType: ociImageIndexMediaType, Digest: imageID, Size: int64(len(primaryIndex))},
		{
			MediaType: ociImageManifestMediaType,
			Digest:    attestationID,
			Size:      int64(len(attestationManifest)),
			Annotations: map[string]string{
				"io.containerd.manifest.subject": runnableDigest,
			},
		},
	}
	if mutate != nil {
		mutate(&descriptors)
	}
	indexBytes, err := json.Marshal(map[string]any{
		"schemaVersion": 2, "mediaType": ociImageIndexMediaType, "manifests": descriptors,
	})
	if err != nil {
		t.Fatal(err)
	}
	files := []struct {
		name string
		body []byte
	}{
		{configName, config},
		{"blobs/sha256/" + strings.TrimPrefix(imageID, "sha256:"), primaryIndex},
		{"blobs/sha256/" + strings.TrimPrefix(runnableDigest, "sha256:"), runnableManifest},
		{"blobs/sha256/" + strings.TrimPrefix(attestationConfigID, "sha256:"), attestationConfig},
		{"blobs/sha256/" + strings.TrimPrefix(statementID, "sha256:"), statement},
		{"blobs/sha256/" + strings.TrimPrefix(attestationID, "sha256:"), attestationManifest},
		{"manifest.json", manifestBytes},
		{"index.json", indexBytes},
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	for _, file := range files {
		if err := writer.WriteHeader(&tar.Header{Name: file.name, Mode: 0o600, Size: int64(len(file.body))}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(file.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return archive.Bytes(), imageID
}

func makeDockerSchema2OCIArchive(t *testing.T, mutate func(map[string]any), includeLayer bool) ([]byte, string) {
	t.Helper()
	config := []byte(`{"architecture":"amd64","os":"linux"}`)
	configDigest := sha256.Sum256(config)
	configID := "sha256:" + hex.EncodeToString(configDigest[:])
	layer := []byte("screened image layer")
	layerDigest := sha256.Sum256(layer)
	layerID := "sha256:" + hex.EncodeToString(layerDigest[:])
	manifest := map[string]any{
		"schemaVersion": 2,
		"mediaType":     dockerManifestMediaType,
		"config": map[string]any{
			"mediaType": dockerConfigMediaType,
			"digest":    configID,
			"size":      len(config),
		},
		"layers": []map[string]any{{
			"mediaType": dockerLayerGzipMediaType,
			"digest":    layerID,
			"size":      len(layer),
		}},
	}
	if mutate != nil {
		mutate(manifest)
	}
	primary, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	primaryDigest := sha256.Sum256(primary)
	imageID := "sha256:" + hex.EncodeToString(primaryDigest[:])
	configName := "blobs/sha256/" + strings.TrimPrefix(configID, "sha256:")
	layerName := "blobs/sha256/" + strings.TrimPrefix(layerID, "sha256:")
	manifestBytes, err := json.Marshal([]map[string]any{{
		"Config": configName, "RepoTags": nil, "Layers": []string{layerName},
	}})
	if err != nil {
		t.Fatal(err)
	}
	indexBytes, err := json.Marshal(map[string]any{
		"schemaVersion": 2,
		"mediaType":     ociImageIndexMediaType,
		"manifests": []map[string]any{{
			"mediaType": dockerManifestMediaType,
			"digest":    imageID,
			"size":      len(primary),
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	files := []struct {
		name string
		body []byte
	}{
		{configName, config},
		{"blobs/sha256/" + strings.TrimPrefix(imageID, "sha256:"), primary},
		{"manifest.json", manifestBytes},
		{"index.json", indexBytes},
	}
	if includeLayer {
		files = append(files, struct {
			name string
			body []byte
		}{layerName, layer})
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	for _, file := range files {
		if err := writer.WriteHeader(&tar.Header{Name: file.name, Mode: 0o600, Size: int64(len(file.body))}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(file.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return archive.Bytes(), imageID
}

func writeArchive(t *testing.T, archive []byte) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "image.tar")
	if err := os.WriteFile(path, archive, 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func gzipBytes(t *testing.T, raw []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	writer := gzip.NewWriter(&buf)
	if _, err := writer.Write(raw); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func makeKanikoGCRArchive(t *testing.T, repoTags ...string) ([]byte, string) {
	t.Helper()
	if len(repoTags) == 0 {
		repoTags = []string{testScreenedImageRef}
	}
	config := []byte(`{"architecture":"amd64","os":"linux"}`)
	configDigest := sha256.Sum256(config)
	configHex := hex.EncodeToString(configDigest[:])
	configName := "sha256:" + configHex
	layer := []byte("kaniko layer")
	layerDigest := sha256.Sum256(layer)
	layerName := hex.EncodeToString(layerDigest[:]) + ".tar.gz"
	manifest, err := json.Marshal([]map[string]any{{
		"Config":   configName,
		"RepoTags": repoTags,
		"Layers":   []string{layerName},
	}})
	if err != nil {
		t.Fatal(err)
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	for _, file := range []struct {
		name string
		body []byte
	}{
		{configName, config},
		{layerName, layer},
		{"manifest.json", manifest},
	} {
		if err := writer.WriteHeader(&tar.Header{Name: file.name, Mode: 0o600, Size: int64(len(file.body))}); err != nil {
			t.Fatal(err)
		}
		if _, err := writer.Write(file.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	return archive.Bytes(), "sha256:" + configHex
}

func TestValidateDockerSaveArchiveAcceptsKanikoGoContainerRegistryTar(t *testing.T) {
	archive, imageID := makeKanikoGCRArchive(t)
	path := writeArchive(t, archive)

	tagged, err := validateDockerSaveArchive(path, testScreenedImageRef, imageID)
	if err != nil {
		t.Fatalf("kaniko docker-save archive rejected: %v", err)
	}
	if !tagged {
		t.Fatal("tagged kaniko archive reported as untagged")
	}

	accepted := map[string]struct{}{imageID: {}}
	if _, err := validateDockerSaveArchiveWithStoreIDs(path, testScreenedImageRef, imageID, accepted); err != nil {
		t.Fatalf("kaniko archive store-id validation rejected: %v", err)
	}
	if len(accepted) < 2 {
		t.Fatalf("expected reconstructed store digest, got %v", accepted)
	}
}

func TestValidateDockerSaveArchiveAcceptsGzipAndUncompressed(t *testing.T) {
	archive, imageID := makeScreenedImageArchive(t)

	tagged, err := validateDockerSaveArchive(writeArchive(t, archive), testScreenedImageRef, imageID)
	if err != nil {
		t.Fatalf("uncompressed archive rejected: %v", err)
	}
	if !tagged {
		t.Fatal("tagged uncompressed archive reported as untagged")
	}

	gzPath := filepath.Join(t.TempDir(), "image.tar.gz")
	if err := os.WriteFile(gzPath, gzipBytes(t, archive), 0o600); err != nil {
		t.Fatal(err)
	}
	tagged, err = validateDockerSaveArchive(gzPath, testScreenedImageRef, imageID)
	if err != nil {
		t.Fatalf("gzip archive rejected: %v", err)
	}
	if !tagged {
		t.Fatal("tagged gzip archive reported as untagged")
	}
}

func TestValidateDockerSaveArchiveAcceptsGzipOCIIndex(t *testing.T) {
	archive, imageID := makeOCIIndexArchive(t, nil)
	path := filepath.Join(t.TempDir(), "image.tar.gz")
	if err := os.WriteFile(path, gzipBytes(t, archive), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := validateDockerSaveArchive(path, testScreenedImageRef, imageID); err != nil {
		t.Fatalf("gzip OCI archive rejected: %v", err)
	}
}

func TestValidateDockerSaveArchiveRejectsTruncatedGzip(t *testing.T) {
	archive, imageID := makeScreenedImageArchive(t)
	compressed := gzipBytes(t, archive)
	if len(compressed) < 12 {
		t.Fatal("gzip fixture too small")
	}
	path := filepath.Join(t.TempDir(), "image.tar.gz")
	if err := os.WriteFile(path, compressed[:10], 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := validateDockerSaveArchive(path, testScreenedImageRef, imageID); err == nil {
		t.Fatal("truncated gzip archive was accepted")
	}
}

func TestValidateDockerSaveArchiveRejectsGzipNonTar(t *testing.T) {
	_, imageID := makeScreenedImageArchive(t)
	path := filepath.Join(t.TempDir(), "image.tar.gz")
	if err := os.WriteFile(path, gzipBytes(t, []byte("this is not a tar archive")), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := validateDockerSaveArchive(path, testScreenedImageRef, imageID); err == nil {
		t.Fatal("gzip of non-tar was accepted")
	}
}

func TestLoadScreenedImageAcceptsGzipArchive(t *testing.T) {
	archive, imageID := makeScreenedImageArchive(t)
	compressed := gzipBytes(t, archive)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(compressed)
	}))
	defer server.Close()
	installFakeDocker(t, "", imageID)
	src := screenedImageSource(server.URL, compressed, imageID)
	if _, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir()); err != nil {
		t.Fatalf("load gzip screened image: %v", err)
	}
}

func TestValidateDockerSaveArchiveAcceptsBoundedAttestations(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*[]ociDescriptor)
	}{
		{"containerd subject", nil},
		{"Docker reference", func(descriptors *[]ociDescriptor) {
			runnable := (*descriptors)[1].Annotations["io.containerd.manifest.subject"]
			(*descriptors)[1].Annotations = map[string]string{
				"vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": runnable,
			}
			(*descriptors)[1].Platform = &struct {
				Architecture string `json:"architecture"`
				OS           string `json:"os"`
			}{Architecture: "unknown", OS: "unknown"}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			archive, imageID := makeOCIIndexArchive(t, tc.mutate)
			tagged, err := validateDockerSaveArchive(writeArchive(t, archive), testScreenedImageRef, imageID)
			if err != nil {
				t.Fatalf("valid OCI index with attestation rejected: %v", err)
			}
			if tagged {
				t.Fatal("untagged OCI index reported as tagged")
			}
		})
	}
}

func TestValidateDockerSaveArchiveAcceptsOCIConfigStoreID(t *testing.T) {
	archive, imageID := makeOCIIndexArchive(t, nil)
	accepted := map[string]struct{}{imageID: {}}
	if _, err := validateDockerSaveArchiveWithStoreIDs(
		writeArchive(t, archive), testScreenedImageRef, imageID, accepted,
	); err != nil {
		t.Fatalf("valid OCI archive rejected: %v", err)
	}
	path := writeArchive(t, archive)
	file, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	reader := tar.NewReader(file)
	for {
		header, err := reader.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if header.Name != "manifest.json" {
			continue
		}
		var manifest []struct {
			Config string `json:"Config"`
		}
		if err := json.NewDecoder(reader).Decode(&manifest); err != nil {
			t.Fatal(err)
		}
		configID := "sha256:" + strings.TrimPrefix(manifest[0].Config, "blobs/sha256/")
		if _, ok := accepted[configID]; !ok {
			t.Fatalf("Docker config store id %s was not accepted: %#v", configID, accepted)
		}
		return
	}
	t.Fatal("manifest.json missing")
}

func TestValidateDockerSaveArchiveAcceptsKanikoAttemptTag(t *testing.T) {
	attemptRef := "ditto-screen/550e8400-e29b-41d4-a716-446655440000-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:latest"
	archive, imageID := makeScreenedImageArchiveVariant(t, []string{attemptRef}, false)
	tagged, err := validateDockerSaveArchive(writeArchive(t, archive), testScreenedImageRef, imageID)
	if err != nil {
		t.Fatalf("Kaniko attempt-scoped tag rejected: %v", err)
	}
	if tagged {
		t.Fatal("attempt-scoped tag must be retagged to the agent ref")
	}
}

func TestValidateDockerSaveArchiveAcceptsKanikoGCRConfigWithAttemptTag(t *testing.T) {
	attemptRef := "ditto-screen/550e8400-e29b-41d4-a716-446655440000-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:latest"
	archive, imageID := makeKanikoGCRArchive(t, attemptRef)
	tagged, err := validateDockerSaveArchive(writeArchive(t, archive), testScreenedImageRef, imageID)
	if err != nil {
		t.Fatalf("Kaniko gcr config + attempt tag rejected: %v", err)
	}
	if tagged {
		t.Fatal("attempt-scoped Kaniko tar must be retagged to the agent ref")
	}
}

func TestValidateDockerSaveArchiveRejectsForeignAttemptTag(t *testing.T) {
	other := "ditto-screen/11111111-1111-4111-8111-111111111111-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:latest"
	archive, imageID := makeScreenedImageArchiveVariant(t, []string{other}, false)
	if _, err := validateDockerSaveArchive(writeArchive(t, archive), testScreenedImageRef, imageID); err == nil {
		t.Fatal("foreign attempt-scoped tag was accepted")
	}
}

func TestValidateDockerSaveArchiveAcceptsProvenanceDisabledDockerStoreID(t *testing.T) {
	archive, imageID := makeDockerSchema2OCIArchive(t, nil, true)
	tagged, err := validateDockerSaveArchive(writeArchive(t, archive), testScreenedImageRef, imageID)
	if err != nil {
		t.Fatalf("valid provenance-disabled Docker archive rejected: %v", err)
	}
	if tagged {
		t.Fatal("untagged Docker archive reported as tagged")
	}
}

func TestValidateDockerSaveArchiveRejectsMalformedDockerStoreManifest(t *testing.T) {
	tests := []struct {
		name         string
		mutate       func(map[string]any)
		includeLayer bool
	}{
		{"wrong schema", func(manifest map[string]any) { manifest["schemaVersion"] = 1 }, true},
		{"foreign layer", func(manifest map[string]any) {
			manifest["layers"].([]map[string]any)[0]["mediaType"] = "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip"
		}, true},
		{"wrong config size", func(manifest map[string]any) {
			manifest["config"].(map[string]any)["size"] = 999
		}, true},
		{"missing layer", nil, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			archive, imageID := makeDockerSchema2OCIArchive(t, tc.mutate, tc.includeLayer)
			if _, err := validateDockerSaveArchive(writeArchive(t, archive), testScreenedImageRef, imageID); err == nil {
				t.Fatal("malformed Docker store manifest was accepted")
			}
		})
	}
}

func TestValidateDockerSaveArchiveRejectsAmbiguousOCIIndex(t *testing.T) {
	tests := []struct {
		name   string
		mutate func(*[]ociDescriptor)
	}{
		{"second runnable image", func(descriptors *[]ociDescriptor) {
			(*descriptors)[1].Annotations = nil
			(*descriptors)[1].Platform = nil
		}},
		{"duplicate primary", func(descriptors *[]ociDescriptor) {
			*descriptors = append(*descriptors, (*descriptors)[0])
		}},
		{"named attestation", func(descriptors *[]ociDescriptor) {
			(*descriptors)[1].Annotations["io.containerd.image.name"] = "docker.io/evil/image:latest"
		}},
		{"unrelated subject", func(descriptors *[]ociDescriptor) {
			(*descriptors)[1].Annotations["io.containerd.manifest.subject"] = "sha256:" + repeatHex("cc", 32)
		}},
		{"runnable attachment platform", func(descriptors *[]ociDescriptor) {
			(*descriptors)[1].Platform = &struct {
				Architecture string `json:"architecture"`
				OS           string `json:"os"`
			}{Architecture: "amd64", OS: "linux"}
		}},
		{"too many attachments", func(descriptors *[]ociDescriptor) {
			attachment := (*descriptors)[1]
			for range maxAttestationDescriptors {
				*descriptors = append(*descriptors, attachment)
			}
		}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			archive, imageID := makeOCIIndexArchive(t, tc.mutate)
			if _, err := validateDockerSaveArchive(writeArchive(t, archive), testScreenedImageRef, imageID); err == nil {
				t.Fatal("ambiguous OCI index was accepted")
			}
		})
	}
}

func TestValidateDockerSaveArchiveAcceptsUntaggedSingleID(t *testing.T) {
	archive, imageID := makeScreenedImageArchiveVariant(t, nil, false)
	path := filepath.Join(t.TempDir(), "image.tar")
	if err := os.WriteFile(path, archive, 0o600); err != nil {
		t.Fatal(err)
	}
	tagged, err := validateDockerSaveArchive(path, testScreenedImageRef, imageID)
	if err != nil {
		t.Fatalf("valid untagged archive rejected: %v", err)
	}
	if tagged {
		t.Fatal("untagged archive reported as tagged")
	}

	wrongID := "sha256:" + repeatHex("77", 32)
	if _, err := validateDockerSaveArchive(path, testScreenedImageRef, wrongID); err == nil {
		t.Fatal("archive accepted for the wrong signed image ID")
	}
}

func TestLoadScreenedImageAcceptsDerivedContainerdStoreID(t *testing.T) {
	archive, imageID := makeScreenedImageArchive(t)
	accepted := map[string]struct{}{imageID: {}}
	if _, err := validateDockerSaveArchiveWithStoreIDs(writeArchive(t, archive), testScreenedImageRef, imageID, accepted); err != nil {
		t.Fatal(err)
	}
	containerdID := ""
	for candidate := range accepted {
		if candidate != imageID {
			containerdID = candidate
		}
	}
	if containerdID == "" {
		t.Fatal("classic archive did not derive a containerd store manifest id")
	}

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	defer server.Close()
	installFakeDocker(t, "", containerdID)
	src := screenedImageSource(server.URL, archive, imageID)
	if _, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir()); err != nil {
		t.Fatalf("load with containerd store id %s: %v", containerdID, err)
	}
}

func TestValidateDockerSaveArchiveRejectsMultipleImageIDs(t *testing.T) {
	archive, imageID := makeScreenedImageArchiveVariant(t, nil, true)
	path := filepath.Join(t.TempDir(), "image.tar")
	if err := os.WriteFile(path, archive, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := validateDockerSaveArchive(path, testScreenedImageRef, imageID); err == nil {
		t.Fatal("archive containing multiple image IDs was accepted")
	}
}

func TestLoadScreenedImageTagsUntaggedImmutableID(t *testing.T) {
	archive, imageID := makeScreenedImageArchiveVariant(t, nil, false)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	defer server.Close()
	logPath := installFakeDocker(t, "", imageID)
	src := screenedImageSource(server.URL, archive, imageID)

	if _, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir()); err != nil {
		t.Fatalf("load untagged archive: %v", err)
	}
	log, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(log), "image tag "+imageID+" "+testScreenedImageRef) {
		t.Fatalf("immutable ID was not tagged to expected ref:\n%s", log)
	}
}

func screenedImageSource(serverURL string, archive []byte, imageID string) Source {
	digest := sha256.Sum256(archive)
	return Source{
		TarballURL:          "https://example.test/source.tar.gz",
		ScreenedImageURL:    serverURL,
		ScreenedImageSHA256: hex.EncodeToString(digest[:]),
		ScreenedImageID:     imageID,
		ScreenedImageRef:    testScreenedImageRef,
		ScreenedImageSize:   int64(len(archive)),
	}
}

func installFakeDocker(t *testing.T, mode, imageID string) string {
	t.Helper()
	fakeBin := t.TempDir()
	logPath := filepath.Join(fakeBin, "docker.log")
	statePath := filepath.Join(fakeBin, "loaded")
	script := `#!/bin/sh
echo "$@" >> "$FAKE_DOCKER_LOG"
if [ "$1 $2" = "image inspect" ]; then
  [ -f "$FAKE_DOCKER_STATE" ] || exit 1
  [ "$FAKE_DOCKER_MODE" = "inspect-fail" ] && exit 1
  if [ "$4" = "{{if .Config.Volumes}}declared{{end}}" ]; then
    [ "$FAKE_DOCKER_MODE" = "volumes" ] && echo declared
    exit 0
  fi
  echo "$FAKE_DOCKER_IMAGE_ID"
  exit 0
fi
if [ "$1 $2" = "image load" ]; then
  touch "$FAKE_DOCKER_STATE"
  [ "$FAKE_DOCKER_MODE" = "load-fail" ] && exit 42
  exit 0
fi
if [ "$1 $2" = "image rm" ]; then
  rm -f "$FAKE_DOCKER_STATE"
fi
exit 0
`
	if err := os.WriteFile(filepath.Join(fakeBin, "docker"), []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", fakeBin+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("FAKE_DOCKER_LOG", logPath)
	t.Setenv("FAKE_DOCKER_STATE", statePath)
	t.Setenv("FAKE_DOCKER_MODE", mode)
	t.Setenv("FAKE_DOCKER_IMAGE_ID", imageID)
	return logPath
}

func TestLoadScreenedImageVerifiesArchiveAndDockerID(t *testing.T) {
	archive, imageID := makeScreenedImageArchive(t)
	digest := sha256.Sum256(archive)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	defer server.Close()

	fakeBin := t.TempDir()
	fakeDocker := filepath.Join(fakeBin, "docker")
	script := fmt.Sprintf("#!/bin/sh\nif [ \"$1 $2\" = \"image inspect\" ]; then if [ \"$4\" = \"{{if .Config.Volumes}}declared{{end}}\" ]; then exit 0; fi; echo %s; fi\nexit 0\n", imageID)
	if err := os.WriteFile(fakeDocker, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", fakeBin+string(os.PathListSeparator)+os.Getenv("PATH"))

	src := Source{
		TarballURL:          "https://example.test/source.tar.gz",
		ScreenedImageURL:    server.URL,
		ScreenedImageSHA256: hex.EncodeToString(digest[:]),
		ScreenedImageID:     imageID,
		ScreenedImageRef:    "ditto-screen/550e8400-e29b-41d4-a716-446655440000:latest",
		ScreenedImageSize:   int64(len(archive)),
	}
	image, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(
		context.Background(), src, t.TempDir(),
	)
	if err != nil {
		t.Fatalf("load screened image: %v", err)
	}
	if image == "" {
		t.Fatal("expected a local image reference")
	}
}

func TestLoadScreenedImageRejectsArchiveMismatchBeforeDocker(t *testing.T) {
	archive, imageID := makeScreenedImageArchive(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	defer server.Close()

	for _, tc := range []struct {
		name   string
		mutate func(*Source)
		want   string
	}{
		{"sha256", func(src *Source) { src.ScreenedImageSHA256 = repeatHex("00", 32) }, "sha256 mismatch"},
		{"size", func(src *Source) { src.ScreenedImageSize++ }, "size mismatch"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logPath := installFakeDocker(t, "", imageID)
			src := screenedImageSource(server.URL, archive, imageID)
			tc.mutate(&src)
			_, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir())
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want %q", err, tc.want)
			}
			if log, readErr := os.ReadFile(logPath); readErr == nil && len(log) != 0 {
				t.Fatalf("docker invoked before archive verification: %s", log)
			}
		})
	}
}

func TestLoadScreenedImageCleansUpFailures(t *testing.T) {
	archive, expectedID := makeScreenedImageArchive(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	defer server.Close()

	for _, tc := range []struct {
		name     string
		mode     string
		actualID string
		want     string
	}{
		{"docker-load", "load-fail", expectedID, "docker image load failed"},
		{"missing-ref", "inspect-fail", expectedID, "loaded archive missing expected image ref"},
		{"image-id", "", "sha256:" + repeatHex("56", 32), "screened image id mismatch"},
		{"declared-volume", "volumes", expectedID, "declares writable volumes"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logPath := installFakeDocker(t, tc.mode, tc.actualID)
			src := screenedImageSource(server.URL, archive, expectedID)
			_, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir())
			if err == nil || !strings.Contains(err.Error(), tc.want) {
				t.Fatalf("error = %v, want %q", err, tc.want)
			}
			log, readErr := os.ReadFile(logPath)
			if readErr != nil {
				t.Fatal(readErr)
			}
			if !strings.Contains(string(log), "image rm "+src.ScreenedImageRef) {
				t.Fatalf("loaded ref was not cleaned up:\n%s", log)
			}
		})
	}
}

func TestLoadScreenedImageDropsArchiveRefAfterRetag(t *testing.T) {
	archive, imageID := makeScreenedImageArchive(t)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	defer server.Close()
	logPath := installFakeDocker(t, "", imageID)
	src := screenedImageSource(server.URL, archive, imageID)

	localRef, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(localRef, "image-"+strings.TrimPrefix(imageID, "sha256:")[:12]) {
		t.Fatalf("local ref %q is not content-addressed", localRef)
	}
	log, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(log), "image rm "+src.ScreenedImageRef) {
		t.Fatalf("archive ref was not removed after retag:\n%s", log)
	}
}

func TestStopRemovesContainerThenRequestImage(t *testing.T) {
	imageID := "sha256:" + repeatHex("34", 32)
	logPath := installFakeDocker(t, "", imageID)
	docker := &LocalDocker{}
	docker.Stop(context.Background(), &Handle{
		ContainerID: "container-123",
		ImageRef:    "dittobench-sub:screened-image-343434343434",
	})
	log, err := os.ReadFile(logPath)
	if err != nil {
		t.Fatal(err)
	}
	commands := string(log)
	containerAt := strings.Index(commands, "rm -f container-123")
	imageAt := strings.Index(commands, "image rm dittobench-sub:screened-image-343434343434")
	if containerAt < 0 || imageAt < 0 || containerAt > imageAt {
		t.Fatalf("cleanup order was not container then image:\n%s", commands)
	}
}

func TestScreenerArchiveLoadsWithoutRebuild(t *testing.T) {
	imagePath := os.Getenv("DITTOBENCH_SCREENED_IMAGE_ARCHIVE")
	sourcePath := os.Getenv("DITTOBENCH_SCREENED_SOURCE_ARCHIVE")
	imageRef := os.Getenv("DITTOBENCH_SCREENED_IMAGE_REF")
	imageID := os.Getenv("DITTOBENCH_SCREENED_IMAGE_ID")
	if imagePath == "" || sourcePath == "" || imageRef == "" || imageID == "" {
		t.Skip("set the DITTOBENCH_SCREENED_* variables to a real screener export")
	}
	imageInfo, err := os.Stat(imagePath)
	if err != nil {
		t.Fatal(err)
	}
	imageDigest, err := digestFile(imagePath)
	if err != nil {
		t.Fatal(err)
	}
	sourceDigest, err := digestFile(sourcePath)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/screened-image.tar":
			http.ServeFile(w, r, imagePath)
		case "/source.tar.gz":
			http.ServeFile(w, r, sourcePath)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	docker := NewLocalDocker()
	docker.AllowPrivate = true
	image, _, _, err := docker.Build(context.Background(), Source{
		TarballURL:          server.URL + "/source.tar.gz",
		TarballSHA256:       sourceDigest,
		ScreenedImageURL:    server.URL + "/screened-image.tar",
		ScreenedImageSHA256: imageDigest,
		ScreenedImageID:     imageID,
		ScreenedImageRef:    imageRef,
		ScreenedImageSize:   imageInfo.Size(),
	})
	if err != nil {
		t.Fatalf("load real screener image: %v", err)
	}
	defer docker.Release(context.Background(), image)
	loadedID, err := exec.Command("docker", "image", "inspect", "--format", "{{.Id}}", image).CombinedOutput()
	if err != nil {
		t.Fatalf("inspect loaded image: %s: %v", loadedID, err)
	}
	if strings.TrimSpace(string(loadedID)) != imageID {
		t.Fatalf("loaded image id = %q, want %q", strings.TrimSpace(string(loadedID)), imageID)
	}
}

func digestFile(path string) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	hash := sha256.New()
	if _, err := io.Copy(hash, file); err != nil {
		return "", err
	}
	return hex.EncodeToString(hash.Sum(nil)), nil
}

func repeatHex(pair string, count int) string {
	result := ""
	for range count {
		result += pair
	}
	return result
}
