package server

import (
	"os"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Revision reporting mirrors apps/platform/ditto/api_server/revision.py: the
// running process's commit is resolved once at boot; the checked-out commit
// is re-read (cached, single-flighted) per /health probe so drift between
// deployed code on disk and the running process is visible.

// UnknownCommit is the fallback when no revision can be resolved.
const UnknownCommit = "unknown"

var buildCommitRe = regexp.MustCompile(`^[0-9a-f]{40}$`)

// checkedOutTTL bounds how often a /health probe may fork git.
const checkedOutTTL = 15 * time.Second

// ResolveCommitHash returns the current checkout's revision:
// DITTO_BUILD_COMMIT when set (must be a full 40-char lowercase SHA, else
// "unknown" — release artifacts carry it so deploys need no .git), otherwise
// git rev-parse HEAD, otherwise "unknown".
func ResolveCommitHash() string {
	baked := strings.ToLower(strings.TrimSpace(os.Getenv("DITTO_BUILD_COMMIT")))
	if baked != "" {
		if buildCommitRe.MatchString(baked) {
			return baked
		}
		return UnknownCommit
	}
	cmd := exec.Command("git", "rev-parse", "HEAD")
	out, err := cmd.Output()
	if err != nil {
		return UnknownCommit
	}
	v := strings.TrimSpace(string(out))
	if v == "" {
		return UnknownCommit
	}
	return v
}

type revisionCache struct {
	mu      sync.Mutex
	at      time.Time
	value   string
	resolve func() string
}

func newRevisionCache(resolve func() string) *revisionCache {
	return &revisionCache{resolve: resolve}
}

// checkedOut returns the revision on disk now, cached for checkedOutTTL and
// single-flighted under the mutex.
func (c *revisionCache) checkedOut() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.at.IsZero() && time.Since(c.at) < checkedOutTTL {
		return c.value
	}
	c.value = c.resolve()
	c.at = time.Now()
	return c.value
}

// commitsDiverged is true only when both revisions are known and differ;
// drift is reported, never enforced.
func commitsDiverged(running, checkedOut string) bool {
	return running != UnknownCommit && checkedOut != UnknownCommit && running != checkedOut
}
