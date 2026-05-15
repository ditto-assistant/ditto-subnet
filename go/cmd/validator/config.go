package main

// Config captures every knob the validator binary exposes. It is built
// in parseFlags and threaded through run() so each phase (load,
// partition, score, aggregate, commit) is testable with a fixed value.
type Config struct {
	FixturesRoot         string
	MinerCommitmentsPath string
	Secret               string
	Mechanism            string // "ditto_core" | "ditto_retrieval" | "all"
	Visibility           string // "public" | "private" | "canary" | "all"
	Sample               int
	DeadlineMs           int64
	BudgetLatencyMs      int64
	PrivateFrac          float64
	CanaryFrac           float64
	ReportDir            string
	Netuid               int

	PylonURL           string
	PylonIdentityName  string
	PylonIdentityToken string

	SelfTest bool
	DryRun   bool
}
