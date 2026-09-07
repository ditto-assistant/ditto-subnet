// Package codinggobuild prepares candidate-only Go programs. Private tests and
// oracle helpers are never inputs to generated bridge code or the Go compiler.
package codinggobuild

import (
	"bytes"
	_ "embed"
	"errors"
	"go/format"
	"go/token"
	"sort"
	"strconv"
	"strings"
)

var ErrBuild = errors.New("candidate Go build rejected")

const bridgeSymbol = "DittobenchCandidateBridgeV1"

//go:embed bridge.go.txt
var bridgeTemplate string

// BridgeSource accepts only independently approved function names, never names,
// expected values or source derived from a private test suite.
func BridgeSource(packageName string, functions []string) ([]byte, error) {
	if !token.IsIdentifier(packageName) || packageName == "_" || len(functions) == 0 || len(functions) > 256 {
		return nil, ErrBuild
	}
	names := append([]string(nil), functions...)
	sort.Strings(names)
	var bindings bytes.Buffer
	for index, name := range names {
		if !token.IsIdentifier(name) || name == "_" || name == "init" || name == "main" || strings.HasPrefix(name, "Dittobench") || strings.HasPrefix(name, "dittobench") || (index > 0 && names[index-1] == name) {
			return nil, ErrBuild
		}
		bindings.WriteString(strconv.Quote(name) + ": dittobenchReflect.ValueOf(" + name + "),\n")
	}
	source := strings.ReplaceAll(bridgeTemplate, "PACKAGE_NAME", packageName)
	source = strings.ReplaceAll(source, "FUNCTION_BINDINGS", bindings.String())
	formatted, err := format.Source([]byte(source))
	if err != nil {
		return nil, ErrBuild
	}
	return formatted, nil
}

func MainSource(modulePath string) ([]byte, error) {
	if modulePath == "" || len(modulePath) > 240 || strings.ContainsAny(modulePath, "\\\n\r\x00") {
		return nil, ErrBuild
	}
	return format.Source([]byte("package main\nimport subject " + strconv.Quote(modulePath) + "\nfunc main(){subject." + bridgeSymbol + "()}\n"))
}
