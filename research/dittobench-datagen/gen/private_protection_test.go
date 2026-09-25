package gen

import "testing"

func TestPrivateProtectionDoesNotFreezeUnrelatedWords(t *testing.T) {
	if err := checkPrivateCandidate("Retrieve this entry.", "Retrieve this record.", []string{"or"}); err != nil {
		t.Fatal("unrelated word fragment rejected")
	}
	for _, after := range []string{"Richardson has 30 apples.", "Richard has 301 apples.", "Richard has 30 apples, or pears.", "Richard has 30 apples. Richard agrees."} {
		if err := checkPrivateCandidate("Richard has 30 apples.", after, []string{"Richard", "30", "or"}); err == nil {
			t.Fatal("changed, duplicated or introduced literal accepted")
		}
	}
}
