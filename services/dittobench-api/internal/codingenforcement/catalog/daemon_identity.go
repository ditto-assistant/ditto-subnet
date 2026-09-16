package catalog

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"path"
	"regexp"
	"slices"
)

// DaemonIdentitySchema names the canonical Docker daemon identity that
// preflights, evidence records and the signed approval bind by digest.
const DaemonIdentitySchema = "dittobench-coding-native-daemon-identity-v1"

// DaemonImageStore is the containerd image store every native daemon uses.
const DaemonImageStore = "io.containerd.snapshotter.v1"

// daemonText is printable ASCII without whitespace, bounded like a path.
var daemonText = regexp.MustCompile(`^[\x21-\x7e]{1,512}$`)

// ErrDaemonIdentity marks docker info that cannot identify a native daemon.
var ErrDaemonIdentity = errors.New("daemon identity is malformed")

// DaemonIdentity is the stable identity of the dedicated rootless dockerd.
//
// It keeps only fields that change when the daemon itself changes: its
// persisted engine ID, binary version, rootless isolation, the socket the
// client used, data root, storage and image store, containerd instance,
// cgroup setup, security options and default runtime. Counters, clocks,
// memory, CPU count, host name and kernel are left out; machine and boot are
// bound separately.
type DaemonIdentity struct {
	Schema            string   `json:"schema"`
	EngineID          string   `json:"engine_id"`
	ServerVersion     string   `json:"server_version"`
	Rootless          bool     `json:"rootless"`
	SocketPath        string   `json:"socket_path"`
	DockerRootDir     string   `json:"docker_root_dir"`
	StorageDriver     string   `json:"storage_driver"`
	ImageStore        string   `json:"image_store"`
	ContainerdAddress string   `json:"containerd_address"`
	CgroupDriver      string   `json:"cgroup_driver"`
	CgroupVersion     string   `json:"cgroup_version"`
	SecurityOptions   []string `json:"security_options"`
	DefaultRuntime    string   `json:"default_runtime"`
}

// DaemonIdentityFromInfo derives the identity from `docker info --format
// '{{json .}}'` output decoded by Decode, for the Unix socket the caller
// itself pinned. It refuses a daemon that is not rootless, not on cgroup v2
// with systemd, or not on the containerd image store.
func DaemonIdentityFromInfo(info any, socketPath string) (DaemonIdentity, error) {
	object, ok := info.(map[string]any)
	if !ok || !daemonText.MatchString(socketPath) || !path.IsAbs(socketPath) || path.Clean(socketPath) != socketPath {
		return DaemonIdentity{}, ErrDaemonIdentity
	}
	text := func(value any) (string, bool) {
		item, ok := value.(string)
		return item, ok && daemonText.MatchString(item)
	}
	identity := DaemonIdentity{Schema: DaemonIdentitySchema, Rootless: true, SocketPath: socketPath, ImageStore: DaemonImageStore}
	for _, field := range []struct {
		target *string
		key    string
	}{
		{&identity.EngineID, "ID"},
		{&identity.ServerVersion, "ServerVersion"},
		{&identity.DockerRootDir, "DockerRootDir"},
		{&identity.StorageDriver, "Driver"},
		{&identity.CgroupDriver, "CgroupDriver"},
		{&identity.CgroupVersion, "CgroupVersion"},
		{&identity.DefaultRuntime, "DefaultRuntime"},
	} {
		if *field.target, ok = text(object[field.key]); !ok {
			return DaemonIdentity{}, ErrDaemonIdentity
		}
	}
	containerd, ok := object["Containerd"].(map[string]any)
	if !ok {
		return DaemonIdentity{}, ErrDaemonIdentity
	}
	if identity.ContainerdAddress, ok = text(containerd["Address"]); !ok {
		return DaemonIdentity{}, ErrDaemonIdentity
	}
	options, ok := object["SecurityOptions"].([]any)
	if !ok {
		return DaemonIdentity{}, ErrDaemonIdentity
	}
	for _, option := range options {
		item, ok := text(option)
		if !ok || slices.Contains(identity.SecurityOptions, item) {
			return DaemonIdentity{}, ErrDaemonIdentity
		}
		identity.SecurityOptions = append(identity.SecurityOptions, item)
	}
	slices.Sort(identity.SecurityOptions)
	status, ok := object["DriverStatus"].([]any)
	if !ok {
		return DaemonIdentity{}, ErrDaemonIdentity
	}
	store := false
	for _, entry := range status {
		pair, ok := entry.([]any)
		store = store || (ok && len(pair) == 2 && pair[0] == "driver-type" && pair[1] == DaemonImageStore)
	}
	if !store || !slices.Contains(identity.SecurityOptions, "name=rootless") ||
		identity.CgroupDriver != "systemd" || identity.CgroupVersion != "2" {
		return DaemonIdentity{}, ErrDaemonIdentity
	}
	return identity, nil
}

// Canonical is the identity's canonical JSON bytes.
func (d DaemonIdentity) Canonical() ([]byte, error) {
	options := make([]any, len(d.SecurityOptions))
	for index, option := range d.SecurityOptions {
		options[index] = option
	}
	return Canonical(map[string]any{
		"schema":             d.Schema,
		"engine_id":          d.EngineID,
		"server_version":     d.ServerVersion,
		"rootless":           d.Rootless,
		"socket_path":        d.SocketPath,
		"docker_root_dir":    d.DockerRootDir,
		"storage_driver":     d.StorageDriver,
		"image_store":        d.ImageStore,
		"containerd_address": d.ContainerdAddress,
		"cgroup_driver":      d.CgroupDriver,
		"cgroup_version":     d.CgroupVersion,
		"security_options":   options,
		"default_runtime":    d.DefaultRuntime,
	})
}

// SHA256 is the hex digest a record's host.daemon_identity_sha256 carries.
func (d DaemonIdentity) SHA256() (string, error) {
	raw, err := d.Canonical()
	if err != nil {
		return "", err
	}
	return digestOf(raw), nil
}

func digestOf(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
