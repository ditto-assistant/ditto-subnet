package upload

import (
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

// The relay and Platform must quote the same built-in submission policy when
// no settings revision exists. Platform pins its side against this file
// (test_built_in_default_fee_matches_the_go_upload_relay); this mirror runs on
// relay changes, so the guard fires whichever side moves.
func TestBuiltInDefaultsMatchPlatform(t *testing.T) {
	raw, err := os.ReadFile("../../../../apps/platform/ditto/db/queries/submission_settings.py")
	if err != nil {
		t.Fatal(err)
	}
	source := string(raw)
	constant := func(name string) int64 {
		t.Helper()
		match := regexp.MustCompile(`(?m)^` + name + ` = ([0-9_]+)`).FindStringSubmatch(source)
		if match == nil {
			t.Fatalf("%s not found in Platform submission_settings.py", name)
		}
		value, err := strconv.ParseInt(strings.ReplaceAll(match[1], "_", ""), 10, 64)
		if err != nil {
			t.Fatal(err)
		}
		return value
	}
	if got := constant("DEFAULT_SUBMISSION_FEE_RAO"); got != defaultFeeAmountRao {
		t.Fatalf("Platform default fee %d rao != relay default %d rao", got, defaultFeeAmountRao)
	}
	if got := constant("DEFAULT_SUBMISSION_COOLDOWN_SECONDS"); got != defaultCooldownSeconds {
		t.Fatalf("Platform default cooldown %d s != relay default %d s", got, defaultCooldownSeconds)
	}
}
