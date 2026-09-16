//go:build !linux

package probe

import (
	"context"
	"errors"
	"io"
)

// RunWorkload is Linux-only: it drives cgroup v2 limits.
func RunWorkload(context.Context, WorkloadOptions, io.Writer) error {
	return errors.New("probe: workloads need Linux")
}
