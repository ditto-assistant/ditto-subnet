// Binary dittobench-coding-fixture-command is public sandbox-certification
// payload. It is never used for scoring or included in a production grader.
package main

import (
	"fmt"
	"os"
	"strconv"
	"time"
)

func main() {
	if len(os.Args) < 2 {
		os.Exit(64)
	}
	switch os.Args[1] {
	case "echo":
		_, _ = fmt.Fprintln(os.Stdout, os.Args[2:])
	case "sleep-ms":
		if len(os.Args) != 3 {
			os.Exit(64)
		}
		milliseconds, err := strconv.ParseInt(os.Args[2], 10, 64)
		if err != nil || milliseconds < 0 || milliseconds > 60_000 {
			os.Exit(64)
		}
		time.Sleep(time.Duration(milliseconds) * time.Millisecond)
	case "write":
		if len(os.Args) != 4 {
			os.Exit(64)
		}
		if err := os.WriteFile(os.Args[2], []byte(os.Args[3]), 0o600); err != nil {
			os.Exit(74)
		}
	case "exit":
		if len(os.Args) != 3 {
			os.Exit(64)
		}
		code, err := strconv.Atoi(os.Args[2])
		if err != nil || code < 0 || code > 125 {
			os.Exit(64)
		}
		os.Exit(code)
	default:
		os.Exit(64)
	}
}
