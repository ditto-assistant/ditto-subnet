package main

import "testing"

func TestValidatePrivateHarnessPosture(t *testing.T) {
	cases := []struct {
		name                   string
		allowPrivate, screened bool
		wantErr                bool
	}{
		{"default", false, false, false},
		{"local rehearsal", true, false, false},
		{"validator", false, true, false},
		{"both refused", true, true, true},
	}
	for _, tc := range cases {
		err := validatePrivateHarnessPosture(tc.allowPrivate, tc.screened)
		if (err != nil) != tc.wantErr {
			t.Errorf("%s: err=%v wantErr=%v", tc.name, err, tc.wantErr)
		}
	}
}
