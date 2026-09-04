module github.com/ditto-assistant/dittobench-api

go 1.26.6

require (
	github.com/ChainSafe/go-schnorrkel v1.1.0
	github.com/ditto-assistant/dittobench-datagen v0.13.2
	github.com/google/uuid v1.6.0
	github.com/mr-tron/base58 v1.3.0
	github.com/smacker/go-tree-sitter v0.0.0-20240827094217-dd81d9e9be82
	golang.org/x/crypto v0.55.0
	golang.org/x/sys v0.47.0
)

require (
	github.com/cosmos/go-bip39 v0.0.0-20180819234021-555e2067c45d // indirect
	github.com/gtank/merlin v0.1.1-0.20191105220539-8318aed1a79f // indirect
	github.com/gtank/ristretto255 v0.1.2 // indirect
	github.com/mimoo/StrobeGo v0.0.0-20181016162300-f8f6d4d2b643 // indirect
)

replace github.com/ditto-assistant/dittobench-datagen => ../../research/dittobench-datagen
