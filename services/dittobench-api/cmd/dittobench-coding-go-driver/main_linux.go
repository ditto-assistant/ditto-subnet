package main

import (
	"context"
	"github.com/ditto-assistant/dittobench-api/internal/codinggodriver"
	"os"
)

func main() {
	code, err := codinggodriver.Run(context.Background(), os.Args[1:])
	if err != nil {
		code = 70
	}
	os.Exit(code)
}
