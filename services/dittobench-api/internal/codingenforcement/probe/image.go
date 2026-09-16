package probe

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"strings"
)

var ociDigest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// imageInspection is the subset of `docker image inspect` the resolver reads.
type imageInspection struct {
	ID          string   `json:"Id"`
	RepoDigests []string `json:"RepoDigests"`
	OS          string   `json:"Os"`
	Arch        string   `json:"Architecture"`
	Config      struct {
		Volumes map[string]any    `json:"Volumes"`
		Env     []string          `json:"Env"`
		Labels  map[string]string `json:"Labels"`
	} `json:"Config"`
}

// ResolvedImage is an approved runtime image present in the local daemon.
type ResolvedImage struct {
	// Reference is the registry@sha256 reference the approval named.
	Reference string
	// ID is the local content-addressed image id, recorded in the report.
	ID string
	// RepoDigest is the matched approved digest (registry@sha256:...).
	RepoDigest string
}

// ResolveApprovedImage inspects a locally present image and refuses unless one
// of its RepoDigests carries the approved digest. It never pulls, so a missing
// image is refused before any launch is attempted.
//
// It is a pre-check, not the launch guard. The executor launches by the same
// repository@sha256 reference with `docker create --pull never` and its policy
// inspection requires the container's image to equal the image id its own
// preflight resolved. The resolved id is not passed into that launch.
func ResolveApprovedImage(ctx context.Context, cli DockerCLI, approvedReference string) (ResolvedImage, error) {
	at := strings.LastIndex(approvedReference, "@")
	if at <= 0 || !ociDigest.MatchString(approvedReference[at+1:]) {
		return ResolvedImage{}, fmt.Errorf("probe: approved image %q is not registry@sha256 addressed", approvedReference)
	}
	raw, err := cli.Output(ctx, "image", "inspect", approvedReference)
	if err != nil {
		// A non-zero inspect is a missing image. The runner refuses rather than
		// letting any later command pull it.
		return ResolvedImage{}, fmt.Errorf("probe: approved image is not present locally; refusing to pull: %s", strings.TrimSpace(string(raw)))
	}
	var images []imageInspection
	if err := json.Unmarshal(bytes.TrimSpace(raw), &images); err != nil || len(images) != 1 {
		return ResolvedImage{}, errors.New("probe: approved image inspection is invalid")
	}
	image := images[0]
	if !ociDigest.MatchString(image.ID) {
		return ResolvedImage{}, errors.New("probe: approved image id is not content-addressed")
	}
	matched := ""
	for _, digest := range image.RepoDigests {
		if strings.HasSuffix(digest, approvedReference[at:]) {
			matched = digest
		}
	}
	if matched == "" {
		return ResolvedImage{}, errors.New("probe: local image does not carry the approved repository digest")
	}
	if image.OS != "linux" || image.Arch != "amd64" {
		return ResolvedImage{}, errors.New("probe: approved image platform is not linux/amd64")
	}
	if len(image.Config.Volumes) != 0 {
		return ResolvedImage{}, errors.New("probe: approved image declares a volume")
	}
	if image.Config.Labels["io.heyditto.dittobench.coding-supervisor-contract"] != "1" {
		return ResolvedImage{}, errors.New("probe: approved image is not a coding supervisor image")
	}
	return ResolvedImage{Reference: approvedReference, ID: image.ID, RepoDigest: matched}, nil
}
