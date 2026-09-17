package gen

import "testing"

func TestPrivateBaseObserverDoesNotChangeFrozenBytes(t *testing.T) {
	for _, size := range []string{"small", "full"} {
		base, tokens, err := GeneratePrivateBase(4242, size, 91)
		if err != nil {
			t.Fatal(err)
		}
		profile, _ := ProfileForVersion(size, 13)
		ordinary, err := GenerateDatasetWithSurface(4242, profile, 13, SurfaceOptions{Salt: 91})
		if err != nil {
			t.Fatal(err)
		}
		if string(mustMarshal(t, base)) != string(mustMarshal(t, ordinary)) || len(tokens) == 0 {
			t.Fatal("observer changed bytes or missed typos")
		}
		requests, err := PrivateSurfaceRequests(base, tokens)
		if err != nil || len(requests) == 0 {
			t.Fatal("could not plan protected requests")
		}
	}
}
