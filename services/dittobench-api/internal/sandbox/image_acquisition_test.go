package sandbox

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// Acquiring the screened image is the validator's job over the platform's own
// URL, and it happens before the harness executes a single instruction. A
// transport-layer failure there must be marked as ours.
func TestTransientAcquisitionFailuresAreMarkedUnavailable(t *testing.T) {
	archive, imageID := makeScreenedImageArchive(t)

	for _, tc := range []struct {
		name   string
		status int
	}{
		{"object store 500", http.StatusInternalServerError},
		{"object store 502", http.StatusBadGateway},
		{"object store 503", http.StatusServiceUnavailable},
		{"object store 429", http.StatusTooManyRequests},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				w.WriteHeader(tc.status)
			}))
			defer server.Close()
			src := screenedImageSource(server.URL, archive, imageID)

			_, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir())
			if !errors.Is(err, ErrScreenedImageUnavailable) {
				t.Fatalf("status %d was charged to the agent: %v", tc.status, err)
			}
		})
	}

	t.Run("unreachable object store", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}))
		url := server.URL
		server.Close() // nothing is listening now
		src := screenedImageSource(url, archive, imageID)

		_, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir())
		if !errors.Is(err, ErrScreenedImageUnavailable) {
			t.Fatalf("transport failure was charged to the agent: %v", err)
		}
	})

	t.Run("stream truncated mid-download", func(t *testing.T) {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Length", "999999")
			_, _ = w.Write(archive[:1])
			if flusher, ok := w.(http.Flusher); ok {
				flusher.Flush()
			}
			panic(http.ErrAbortHandler) // kill the connection mid-body
		}))
		defer server.Close()
		src := screenedImageSource(server.URL, archive, imageID)
		src.ScreenedImageSize = 999999

		_, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir())
		if !errors.Is(err, ErrScreenedImageUnavailable) {
			t.Fatalf("truncated download was charged to the agent: %v", err)
		}
	})
}

func TestCodingScreenedImageLoadRejectsSourceMaterialBeforeAcquisition(t *testing.T) {
	docker := &LocalDocker{}
	source := Source{
		GitURL:              "https://github.invalid/private.git",
		ScreenedImageURL:    "https://storage.invalid/image.tar?signature=secret",
		ScreenedImageSHA256: strings.Repeat("a", 64), ScreenedImageSize: 1024,
		ScreenedImageID:  "sha256:" + strings.Repeat("b", 64),
		ScreenedImageRef: "ditto-screen/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa:latest",
	}
	if _, _, err := docker.LoadScreenedImage(t.Context(), source); err == nil {
		t.Fatal("coding image loader accepted source material")
	}
}

// THE BOUND. Every failure that is deterministic in the bytes the platform
// stored stays terminal. A no-fault verdict on any of these would re-lease the
// same permanently broken image forever -- the mnemox loop, rebuilt.
func TestDeterministicVerificationFailuresStayTerminal(t *testing.T) {
	archive, imageID := makeScreenedImageArchive(t)

	for _, tc := range []struct {
		name   string
		status int
		mutate func(*Source)
		serve  []byte
	}{
		{name: "missing object (404)", status: http.StatusNotFound},
		{name: "expired or unauthorized grant (403)", status: http.StatusForbidden},
		{name: "bad request (400)", status: http.StatusBadRequest},
		{
			name:  "sha256 mismatch",
			serve: archive,
			mutate: func(s *Source) {
				s.ScreenedImageSHA256 = strings.Repeat("ab", 32)
			},
		},
		{
			name:   "size mismatch",
			serve:  archive,
			mutate: func(s *Source) { s.ScreenedImageSize = int64(len(archive)) - 1 },
		},
		{
			name:   "size outside the allowed range",
			serve:  archive,
			mutate: func(s *Source) { s.ScreenedImageSize = 0 },
		},
		{
			name:   "image id mismatch",
			serve:  archive,
			mutate: func(s *Source) { s.ScreenedImageID = "sha256:" + strings.Repeat("77", 32) },
		},
		{
			name:  "archive is not a valid docker save",
			serve: []byte("this is not a tar archive at all, not even close"),
		},
		{
			name:  "gzip that is not a docker save",
			serve: gzipBytes(t, []byte("this is not a tar archive at all, not even close")),
		},
		{
			name:  "truncated gzip",
			serve: gzipBytes(t, archive)[:10],
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			body := tc.serve
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				if tc.status != 0 {
					w.WriteHeader(tc.status)
					return
				}
				_, _ = w.Write(body)
			}))
			defer server.Close()

			payload := body
			if payload == nil {
				payload = archive
			}
			src := screenedImageSource(server.URL, payload, imageID)
			if tc.mutate != nil {
				tc.mutate(&src)
			}

			_, _, err := (&LocalDocker{AllowPrivate: true}).loadScreenedImage(context.Background(), src, t.TempDir())
			if err == nil {
				t.Fatal("expected the load to fail")
			}
			if errors.Is(err, ErrScreenedImageUnavailable) {
				t.Fatalf("a deterministic failure was marked no-fault and will re-lease forever: %v", err)
			}
		})
	}
}
