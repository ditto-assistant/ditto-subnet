module github.com/ditto-assistant/dittobench-api

go 1.26.6

require (
	github.com/ditto-assistant/dittobench-datagen v0.13.2
	github.com/google/uuid v1.6.0
	github.com/smacker/go-tree-sitter v0.0.0-20240827094217-dd81d9e9be82
	golang.org/x/sys v0.29.0
)

replace github.com/ditto-assistant/dittobench-datagen => ../../research/dittobench-datagen
