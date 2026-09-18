package parserprobe

import (
	"fmt"
	"math"
)

// checkedOptions prevents training-set leakage and ambiguous aggregate reports.
// Validate before generating any of the potentially 10,000 training artifacts.
func checkedOptions(opts Options) (Options, error) {
	if opts.RunSize == "" {
		opts.RunSize = "full"
	}
	if opts.RouterSeeds < 0 {
		return opts, fmt.Errorf("router seed count must not be negative")
	}
	if opts.RouterWorkers == 0 {
		opts.RouterWorkers = 1
	}
	if opts.RouterWorkers < 1 || opts.RouterWorkers > 16 {
		return opts, fmt.Errorf("router workers must be between 1 and 16")
	}
	var heldOut []int64
	var first, last int64
	if len(opts.Artifacts) == 0 {
		if opts.Seeds <= 0 {
			return opts, fmt.Errorf("at least one seed or artifact is required")
		}
		first = opts.FirstSeed
		if first > math.MaxInt64-int64(opts.Seeds-1) {
			return opts, fmt.Errorf("probe seed range overflows")
		}
		last = first + int64(opts.Seeds-1)
	} else {
		seen := map[int64]bool{}
		for i, a := range opts.Artifacts {
			if a.BenchVersion != opts.BenchVersion {
				return opts, fmt.Errorf("artifact benchmark versions must match the probe")
			}
			if seen[a.Seed] {
				return opts, fmt.Errorf("duplicate held-out artifact seed")
			}
			seen[a.Seed] = true
			heldOut = append(heldOut, a.Seed)
			if i == 0 || a.Seed > last {
				last = a.Seed
			}
		}
	}
	if opts.RouterSeeds == 0 {
		return opts, nil
	}
	if opts.RouterFirstSeed == 0 {
		if last > math.MaxInt64-1001 {
			return opts, fmt.Errorf("default router seed range overflows")
		}
		opts.RouterFirstSeed = last + 1001
	}
	trainFirst := opts.RouterFirstSeed
	if trainFirst > math.MaxInt64-int64(opts.RouterSeeds-1) {
		return opts, fmt.Errorf("router seed range overflows")
	}
	trainLast := trainFirst + int64(opts.RouterSeeds-1)
	if len(heldOut) == 0 {
		if trainFirst <= last && first <= trainLast {
			return opts, fmt.Errorf("router training and probe seed ranges overlap")
		}
	} else {
		for _, seed := range heldOut {
			if trainFirst <= seed && seed <= trainLast {
				return opts, fmt.Errorf("router training includes a held-out artifact seed")
			}
		}
	}
	return opts, nil
}
