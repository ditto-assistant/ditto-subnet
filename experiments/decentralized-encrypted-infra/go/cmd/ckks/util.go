package main

import (
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/tuneinsight/lattigo/v6/core/rlwe"
	"github.com/tuneinsight/lattigo/v6/schemes/ckks"
)

// latency summarizes a list of durations in milliseconds.
type latency struct {
	N      int     `json:"n"`
	P50Ms  float64 `json:"p50_ms"`
	P95Ms  float64 `json:"p95_ms"`
	MaxMs  float64 `json:"max_ms"`
	MeanMs float64 `json:"mean_ms"`
}

func summarize(ds []time.Duration) latency {
	if len(ds) == 0 {
		return latency{}
	}
	ms := make([]float64, len(ds))
	var sum float64
	for i, d := range ds {
		ms[i] = float64(d.Nanoseconds()) / 1e6
		sum += ms[i]
	}
	sort.Float64s(ms)
	pick := func(q float64) float64 {
		idx := int(math.Ceil(q*float64(len(ms)))) - 1
		return ms[max(0, min(idx, len(ms)-1))]
	}
	return latency{N: len(ms), P50Ms: pick(0.50), P95Ms: pick(0.95), MaxMs: ms[len(ms)-1], MeanMs: sum / float64(len(ms))}
}

// rotateSlice returns v rotated left by k: out[i] = v[(i+k) mod n].
func rotateSlice(v []float64, k int) []float64 {
	n := len(v)
	k = ((k % n) + n) % n
	out := make([]float64, n)
	for i := range out {
		out[i] = v[(i+k)%n]
	}
	return out
}

// encMatrix is a matrix whose generalized diagonals are encrypted by the key
// owner, laid out for a baby-step giant-step product with an encrypted vector:
//
//	y = sum_g rot( sum_b Enc(rot(d[g*n1+b], -g*n1)) * rot(x, b), g*n1 )
//
// The owner pre-rotates each diagonal before encrypting it, so the evaluator
// needs only n1-1 baby-step and n2-1 giant-step rotation keys.
type encMatrix struct {
	n1, n2 int
	diags  map[int]*rlwe.Ciphertext
}

func newEncMatrix(params ckks.Parameters, ecd *ckks.Encoder, enc *rlwe.Encryptor, diags map[int][]float64, n1, level, logSlots int) (*encMatrix, error) {
	slots := 1 << logSlots
	maxIdx := 0
	for k := range diags {
		maxIdx = max(maxIdx, k)
	}
	n2 := (maxIdx + n1) / n1
	m := &encMatrix{n1: n1, n2: n2, diags: make(map[int]*rlwe.Ciphertext, len(diags))}
	for k, d := range diags {
		if len(d) != slots {
			return nil, fmt.Errorf("diagonal %d has %d slots, want %d", k, len(d), slots)
		}
		g := k / n1
		pt := ckks.NewPlaintext(params, level)
		pt.LogDimensions.Cols = logSlots
		// The product is rescaled by q_level, so encoding at that scale keeps the
		// input's scale after the rescale.
		pt.Scale = rlwe.NewScale(params.Q()[level])
		if err := ecd.Encode(rotateSlice(d, -g*n1), pt); err != nil {
			return nil, err
		}
		ct, err := enc.EncryptNew(pt)
		if err != nil {
			return nil, err
		}
		m.diags[k] = ct
	}
	return m, nil
}

// rotations lists the rotation indices encMatrix.apply needs keys for.
func (m *encMatrix) rotations() []int {
	var rots []int
	for b := 1; b < m.n1; b++ {
		rots = append(rots, b)
	}
	for g := 1; g < m.n2; g++ {
		rots = append(rots, g*m.n1)
	}
	return rots
}

func (m *encMatrix) bytes() int {
	total := 0
	for _, ct := range m.diags {
		total += ct.BinarySize()
	}
	return total
}

// apply evaluates the product without rescaling the result.
func (m *encMatrix) apply(eval *ckks.Evaluator, ct *rlwe.Ciphertext) (*rlwe.Ciphertext, error) {
	babies := make([]int, 0, m.n1-1)
	for b := 1; b < m.n1; b++ {
		babies = append(babies, b)
	}
	rots, err := eval.RotateHoistedNew(ct, babies)
	if err != nil {
		return nil, err
	}
	rots[0] = ct
	var acc *rlwe.Ciphertext
	for g := 0; g < m.n2; g++ {
		var inner *rlwe.Ciphertext
		for b := 0; b < m.n1; b++ {
			d, ok := m.diags[g*m.n1+b]
			if !ok {
				continue
			}
			prod, err := eval.MulNew(rots[b], d) // degree 2, relinearized once below
			if err != nil {
				return nil, err
			}
			if inner == nil {
				inner = prod
			} else if err := eval.Add(inner, prod, inner); err != nil {
				return nil, err
			}
		}
		if inner == nil {
			continue
		}
		if err := eval.Relinearize(inner, inner); err != nil {
			return nil, err
		}
		if g > 0 {
			if err := eval.Rotate(inner, g*m.n1, inner); err != nil {
				return nil, err
			}
		}
		if acc == nil {
			acc = inner
		} else if err := eval.Add(acc, inner, acc); err != nil {
			return nil, err
		}
	}
	if acc == nil {
		return nil, fmt.Errorf("empty encrypted matrix")
	}
	return acc, nil
}

func galoisKeysFor(params ckks.Parameters, kgen *rlwe.KeyGenerator, sk *rlwe.SecretKey, galEls []uint64) ([]*rlwe.GaloisKey, int) {
	seen := map[uint64]bool{}
	var uniq []uint64
	for _, g := range galEls {
		if !seen[g] {
			seen[g] = true
			uniq = append(uniq, g)
		}
	}
	gks := kgen.GenGaloisKeysNew(uniq, sk)
	total := 0
	for _, gk := range gks {
		total += gk.BinarySize()
	}
	return gks, total
}

func rotationElements(params ckks.Parameters, rots []int) []uint64 {
	els := make([]uint64, 0, len(rots))
	for _, r := range rots {
		els = append(els, params.GaloisElement(r))
	}
	return els
}

func mib(b int) float64 { return float64(b) / (1 << 20) }
