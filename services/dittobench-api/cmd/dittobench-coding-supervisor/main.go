package main

import (
	"context"
	"fmt"
	"os"

	"github.com/ditto-assistant/dittobench-api/internal/codingexecutor"
)

func main() {
	if err := codingexecutor.SupervisorMain(context.Background(), os.Args[1:]); err != nil {
		_, _ = fmt.Fprintln(os.Stderr, "coding supervisor failed")
		os.Exit(111)
	}
}
