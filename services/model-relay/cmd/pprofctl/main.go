// Command pprofctl fetches loopback-only Go profiles from subnet VMs over GCP
// IAP SSH. It is intentionally non-interactive and prints plain text so an
// operator or agent can capture and compare profiles without exposing pprof to
// Caddy or adding another network credential.
package main

import (
	"bytes"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const pprofPortOffset = 3000

type serviceTarget struct {
	mainPort         int
	containerService string
}

var serviceTargets = map[string]serviceTarget{
	"platform-relay-1": {mainPort: 8010},
	"platform-relay-2": {mainPort: 8011},
	// The scorer shares sandbox-docker's network namespace in the current
	// validator Compose stack, so host loopback cannot reach its pprof socket.
	"dittobench-api":         {mainPort: 8000, containerService: "sandbox-docker"},
	"dittobench-model-relay": {mainPort: 11434},
}

var safeName = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)
var safePath = regexp.MustCompile(`^/[A-Za-z0-9/._?&=%-]*$`)

type target struct {
	project          string
	zone             string
	instance         string
	service          string
	mainPort         int
	containerService string
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "pprofctl:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("usage: pprofctl <list|fetch|top|svg|web|diff|goroutines|raw> [flags]")
	}
	command := args[0]
	flags := flag.NewFlagSet(command, flag.ContinueOnError)
	flags.SetOutput(os.Stderr)
	t := target{}
	flags.StringVar(&t.project, "project", "ditto-app-dev", "GCP project")
	flags.StringVar(&t.zone, "zone", "us-central1-a", "GCP zone")
	flags.StringVar(&t.instance, "instance", "ditto-platform-prod", "GCE instance")
	flags.StringVar(&t.service, "service", "platform-relay-1", "known service name")
	flags.IntVar(&t.mainPort, "main-port", 0, "main service port (overrides --service map)")
	flags.StringVar(&t.containerService, "container-service", "", "Compose service sharing the target network namespace")
	profile := flags.String("profile", "heap", "heap|allocs|goroutine|profile|block|mutex|threadcreate|trace")
	seconds := flags.Int("seconds", 30, "CPU profile or trace duration")
	out := flags.String("out", "", "output profile path")
	nodecount := flags.Int("nodecount", 20, "maximum nodes in top output")
	base := flags.String("base", "", "baseline profile path (diff only)")
	rawPath := flags.String("path", "/debug/pprof/", "pprof URL path (raw only)")
	sampleIndex := flags.String("sample-index", "", "pprof sample index, e.g. alloc_objects")
	probe := flags.Bool("probe", false, "probe every known port (list only)")
	if err := flags.Parse(args[1:]); err != nil {
		return err
	}
	if flags.NArg() != 0 {
		return fmt.Errorf("unexpected arguments: %s", strings.Join(flags.Args(), " "))
	}
	if err := t.validate(); err != nil {
		return err
	}

	switch command {
	case "list":
		return listServices(t, *probe)
	case "goroutines":
		body, err := t.fetch("/debug/pprof/goroutine?debug=2", 60)
		if err != nil {
			return err
		}
		_, err = os.Stdout.Write(body)
		return err
	case "raw":
		body, err := t.fetch(*rawPath, 60)
		if err != nil {
			return err
		}
		_, err = os.Stdout.Write(body)
		return err
	case "fetch", "top", "svg", "web", "diff":
		path, err := profileURL(*profile, *seconds)
		if err != nil {
			return err
		}
		body, err := t.fetch(path, *seconds+60)
		if err != nil {
			return err
		}
		output := *out
		if output == "" {
			output = filepath.Join(".tmp", "pprof", fmt.Sprintf(
				"%s-%s-%s-%s.pb.gz", t.instance, t.service, *profile,
				time.Now().UTC().Format("20060102T150405"),
			))
		}
		if err := os.MkdirAll(filepath.Dir(output), 0o755); err != nil {
			return err
		}
		if err := os.WriteFile(output, body, 0o644); err != nil {
			return err
		}
		fmt.Fprintf(os.Stderr, "fetched %s %s/%s -> %s (%d bytes)\n", *profile, t.instance, t.service, output, len(body))
		if command == "fetch" {
			fmt.Println(output)
			return nil
		}
		pprofArgs := []string{"tool", "pprof"}
		if *sampleIndex != "" {
			pprofArgs = append(pprofArgs, "-sample_index="+*sampleIndex)
		}
		switch command {
		case "top":
			pprofArgs = append(pprofArgs, "-top", "-nodecount="+strconv.Itoa(*nodecount), output)
		case "svg":
			svgOut := strings.TrimSuffix(output, filepath.Ext(output)) + ".svg"
			pprofArgs = append(pprofArgs, "-svg", "-output="+svgOut, output)
			fmt.Fprintln(os.Stderr, "rendering", svgOut)
		case "web":
			pprofArgs = append(pprofArgs, "-http=localhost:0", output)
		case "diff":
			if *base == "" {
				return errors.New("diff requires --base <profile>")
			}
			if _, err := os.Stat(*base); err != nil {
				return fmt.Errorf("baseline profile: %w", err)
			}
			pprofArgs = append(pprofArgs, "-top", "-nodecount="+strconv.Itoa(*nodecount), "-diff_base="+*base, output)
		}
		pprof := exec.Command("go", pprofArgs...)
		pprof.Stdin = os.Stdin
		pprof.Stdout = os.Stdout
		pprof.Stderr = os.Stderr
		return pprof.Run()
	default:
		return fmt.Errorf("unknown command %q", command)
	}
}

func (t target) validate() error {
	for label, value := range map[string]string{
		"project": t.project, "zone": t.zone, "instance": t.instance,
		"service": t.service,
	} {
		if !safeName.MatchString(value) {
			return fmt.Errorf("unsafe %s %q", label, value)
		}
	}
	if t.containerService != "" && !safeName.MatchString(t.containerService) {
		return fmt.Errorf("unsafe container service %q", t.containerService)
	}
	_, err := t.pprofPort()
	return err
}

func (t target) pprofPort() (int, error) {
	mainPort := t.mainPort
	if mainPort == 0 {
		known, ok := serviceTargets[t.service]
		if !ok {
			return 0, fmt.Errorf("unknown service %q; pass --main-port", t.service)
		}
		mainPort = known.mainPort
	}
	port := mainPort + pprofPortOffset
	if mainPort < 1 || port > 65535 {
		return 0, fmt.Errorf("main port %d cannot map to pprof", mainPort)
	}
	return port, nil
}

func profileURL(profile string, seconds int) (string, error) {
	switch profile {
	case "profile", "trace":
		if seconds < 1 || seconds > 300 {
			return "", errors.New("--seconds must be between 1 and 300")
		}
		return fmt.Sprintf("/debug/pprof/%s?seconds=%d", profile, seconds), nil
	case "heap", "allocs", "goroutine", "block", "mutex", "threadcreate":
		return "/debug/pprof/" + profile, nil
	default:
		return "", fmt.Errorf("unsupported profile %q", profile)
	}
}

func (t target) fetch(path string, maxTime int) ([]byte, error) {
	if !safePath.MatchString(path) {
		return nil, fmt.Errorf("unsafe pprof path %q", path)
	}
	port, err := t.pprofPort()
	if err != nil {
		return nil, err
	}
	url := fmt.Sprintf("http://127.0.0.1:%d%s", port, path)
	containerService := t.containerService
	if containerService == "" {
		containerService = serviceTargets[t.service].containerService
	}
	remote := fmt.Sprintf("curl -fsS --max-time %d '%s'", maxTime, url)
	if containerService != "" {
		// docker compose project names vary by checkout, so locate the one
		// running container from its stable service label. sandbox-docker has
		// wget (its own healthcheck uses it) and shares dittobench-api's netns.
		remote = fmt.Sprintf(
			"container=$(sudo -n docker ps --filter 'label=com.docker.compose.service=%s' --format '{{.ID}}' | head -n1); test -n \"$container\"; sudo -n docker exec \"$container\" wget -qO- -T %d '%s'",
			containerService, maxTime, url,
		)
	}
	command := exec.Command(
		"gcloud", "compute", "ssh", t.instance,
		"--project", t.project, "--zone", t.zone,
		"--tunnel-through-iap", "--quiet", "--command", remote,
	)
	var stderr bytes.Buffer
	command.Stderr = &stderr
	body, err := command.Output()
	if err != nil {
		message := strings.TrimSpace(stderr.String())
		if message == "" {
			message = err.Error()
		}
		return nil, fmt.Errorf("fetch %s: %s", url, message)
	}
	return body, nil
}

func listServices(base target, probe bool) error {
	for _, service := range []string{"platform-relay-1", "platform-relay-2", "dittobench-api", "dittobench-model-relay"} {
		t := base
		t.service = service
		t.mainPort = 0
		t.containerService = ""
		port, _ := t.pprofPort()
		status := ""
		if probe {
			if _, err := t.fetch("/debug/pprof/cmdline", 5); err == nil {
				status = " UP"
			} else {
				status = " down"
			}
		}
		transport := "host"
		if serviceTargets[service].containerService != "" {
			transport = "container:" + serviceTargets[service].containerService
		}
		fmt.Printf("%-25s main=%-5d pprof=%-5d via=%-24s%s\n", service, serviceTargets[service].mainPort, port, transport, status)
	}
	return nil
}
