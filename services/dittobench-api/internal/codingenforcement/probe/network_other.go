//go:build !linux

package probe

import (
	"errors"
	"net"
	"time"
)

func setUserTimeout(*net.TCPConn, time.Duration) error {
	return errors.New("probe: TCP_USER_TIMEOUT is Linux only")
}
