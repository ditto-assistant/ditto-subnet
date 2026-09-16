package codingcertservice

import (
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ditto-assistant/dittobench-api/internal/codingcanary"
)

func TestRoutesExposeOnlyTheCanaryAndReadiness(t *testing.T) {
	marker := func(name string) http.Handler {
		return http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
			response.Header().Set("X-Route", name)
			response.WriteHeader(http.StatusTeapot)
		})
	}
	routes := Routes(marker("canary"), marker("readiness"))
	for path, want := range map[string]string{
		codingcanary.CanaryPath:                   "canary",
		codingcanary.ReadinessPath:                "readiness",
		"/v1/coding/supervisor/prepare":           "",
		"/v1/coding/publications/pending":         "",
		"/health":                                 "",
		"/":                                       "",
		codingcanary.CanaryPath + "/other":        "",
		"/v1/coding/certifier/canary/readiness/x": "",
	} {
		response := httptest.NewRecorder()
		routes.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		if got := response.Header().Get("X-Route"); got != want || (want == "" && response.Code != http.StatusNotFound) {
			t.Errorf("%s routed to %q status=%d", path, got, response.Code)
		}
	}
}

func TestControlTokenCredentialIsReadWithoutLinksAndValidated(t *testing.T) {
	valid := strings.Repeat("a", 40)
	write := func(t *testing.T, body string, mode os.FileMode) string {
		t.Helper()
		directory := t.TempDir()
		if err := os.WriteFile(filepath.Join(directory, ControlTokenCredential), []byte(body), mode); err != nil {
			t.Fatal(err)
		}
		return directory
	}
	for _, body := range []string{valid, valid + "\n"} {
		if token, err := readControlToken(write(t, body, 0o400)); err != nil || token != valid {
			t.Fatalf("token=%q err=%v", token, err)
		}
	}
	for name, directory := range map[string]func(t *testing.T) string{
		"short":          func(t *testing.T) string { return write(t, "short", 0o400) },
		"too long":       func(t *testing.T) string { return write(t, strings.Repeat("a", 257), 0o400) },
		"bad characters": func(t *testing.T) string { return write(t, valid+" x", 0o400) },
		"two newlines":   func(t *testing.T) string { return write(t, valid+"\n\n", 0o400) },
		"missing":        func(t *testing.T) string { return t.TempDir() },
		"relative":       func(*testing.T) string { return "credentials" },
		"symlink": func(t *testing.T) string {
			source := write(t, valid, 0o400)
			directory := t.TempDir()
			if err := os.Symlink(filepath.Join(source, ControlTokenCredential), filepath.Join(directory, ControlTokenCredential)); err != nil {
				t.Fatal(err)
			}
			return directory
		},
		"directory": func(t *testing.T) string {
			directory := t.TempDir()
			if err := os.Mkdir(filepath.Join(directory, ControlTokenCredential), 0o700); err != nil {
				t.Fatal(err)
			}
			return directory
		},
	} {
		t.Run(name, func(t *testing.T) {
			token, err := readControlToken(directory(t))
			if err == nil || token != "" || !errors.Is(err, ErrUnavailable) {
				t.Fatalf("token=%q err=%v", token, err)
			}
		})
	}
}

// An enabled but unproven service refuses before creating any state: the
// configured euid differs, the credential is missing, or the locked policy is
// absent. None of these reach the fixed socket paths.
func TestRunRefusesBeforeSideEffects(t *testing.T) {
	values := validEnvironment()
	if err := Run(t.Context(), lookup(values), testUID+1); !errors.Is(err, ErrConfig) {
		t.Fatalf("other euid err=%v", err)
	}
	values[ServiceUIDEnvironment] = "61001"
	if err := Run(t.Context(), lookup(values), testUID); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("missing credential err=%v", err)
	}
	for _, path := range []string{ControlSocketPath, PrivateRoot} {
		if _, err := os.Lstat(path); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("%s exists after a refusal: %v", path, err)
		}
	}
}
