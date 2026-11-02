// private-recover finalizes a pinned, complete, trusted local producer checkpoint.
// It accepts no credentials and performs no inference or production mutation.
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/ditto-assistant/dittobench-datagen/gen"
	"github.com/ditto-assistant/dittobench-datagen/privatesurface"
)

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
func run() error {
	basePath := flag.String("base", "", "owner-only original base.json")
	diagnosticsPath := flag.String("diagnostics", "", "owner-only complete producer diagnostics.json")
	confirmedSHA := flag.String("confirmed-diagnostics-sha256", "", "required pin of the trusted checkpoint")
	profilePath := flag.String("profile", "", "owner-only original producer profile JSON")
	profileSHA := flag.String("expected-profile-sha256", "", "original producer profile digest")
	runSize := flag.String("run-size", "full", "original run size")
	out := flag.String("output", "", "new owner-only output directory")
	flag.Parse()
	if *out == "" || len(*confirmedSHA) != 64 || len(*profileSHA) != 64 {
		return errors.New("private recovery: explicit output and checkpoint/profile pins required")
	}
	baseBytes, err := readPrivate(*basePath, gen.MaxPrivateArtifactBytes)
	if err != nil {
		return err
	}
	trace, err := readPrivate(*diagnosticsPath, 64<<20)
	if err != nil {
		return err
	}
	if fmt.Sprintf("%x", sha256.Sum256(trace)) != *confirmedSHA {
		return errors.New("private recovery: checkpoint digest mismatch")
	}
	profileBytes, err := readPrivate(*profilePath, 4096)
	if err != nil {
		return err
	}
	var profile privatesurface.Profile
	if json.Unmarshal(profileBytes, &profile) != nil {
		return errors.New("private recovery: invalid profile")
	}
	actualProfile, err := profile.Digest()
	if err != nil || actualProfile != *profileSHA {
		return errors.New("private recovery: profile digest mismatch")
	}
	var original gen.DatasetArtifact
	if json.Unmarshal(baseBytes, &original) != nil {
		return errors.New("private recovery: invalid base")
	}
	base, protected, err := gen.GeneratePrivateBase(original.Seed, *runSize, original.SurfaceSalt)
	if err != nil {
		return err
	}
	regenerated, err := base.Marshal()
	if err != nil || !bytes.Equal(regenerated, baseBytes) {
		return errors.New("private recovery: original base does not match generator")
	}
	var rows []privatesurface.Diagnostic
	if json.Unmarshal(trace, &rows) != nil {
		return errors.New("private recovery: invalid checkpoint")
	}
	dataset, receipt, err := privatesurface.RecoverCompletedDiagnostics(context.Background(), base, profile, rows, protected)
	if err != nil {
		return err
	}
	if err := os.Mkdir(*out, 0700); err != nil {
		return errors.New("private recovery: output must be new")
	}
	for _, file := range []struct {
		name string
		body []byte
	}{{"base.json", baseBytes}, {"dataset.json", dataset}, {"validation.json", receipt}} {
		f, err := os.OpenFile(filepath.Join(*out, file.name), os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			return err
		}
		_, err = f.Write(file.body)
		if err == nil {
			err = f.Sync()
		}
		closeErr := f.Close()
		if err != nil {
			return err
		}
		if closeErr != nil {
			return closeErr
		}
	}
	fmt.Println("trusted complete checkpoint finalized offline; NOT qualified, pinned, leased or activated")
	return nil
}

func readPrivate(path string, maximum int) ([]byte, error) {
	fail := errors.New("private recovery: input must be a bounded owner-only regular file")
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0077 != 0 || info.Size() <= 0 || info.Size() > int64(maximum) {
		return nil, fail
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, fail
	}
	defer f.Close()
	actual, err := f.Stat()
	if err != nil || !os.SameFile(info, actual) {
		return nil, fail
	}
	raw, err := io.ReadAll(io.LimitReader(f, int64(maximum)+1))
	if err != nil || len(raw) > maximum {
		return nil, fail
	}
	return raw, nil
}
