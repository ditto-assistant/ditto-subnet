package main

import (
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"math/cmplx"
	"os"
	"sync"
	"time"

	"github.com/tuneinsight/lattigo/v6/circuits/ckks/lintrans"
	"github.com/tuneinsight/lattigo/v6/circuits/ckks/polynomial"
	"github.com/tuneinsight/lattigo/v6/core/rlwe"
	"github.com/tuneinsight/lattigo/v6/ring"
	"github.com/tuneinsight/lattigo/v6/schemes/ckks"
	"github.com/tuneinsight/lattigo/v6/utils/bignum"
)

// The 768-d query is padded to 1024 sparse slots so every rotation is cyclic
// within one block; the 128 hidden units tile those 1024 slots 8 times.
const (
	adapterLogSlots = 10
	adapterSlots    = 1 << adapterLogSlots
)

type adapterFixture struct {
	Kind            string   `json:"kind"`
	Synthetic       bool     `json:"synthetic"`
	Dim             int      `json:"dim"`
	Hidden          int      `json:"hidden"`
	Alpha           float64  `json:"alpha"`
	GeluInputBound  float64  `json:"gelu_input_bound"`
	ParityMinCosine float64  `json:"parity_min_cosine"`
	ParityMaxAbs    float64  `json:"parity_max_abs"`
	W1              string   `json:"W1"`
	B1              string   `json:"b1"`
	W2              string   `json:"W2"`
	B2              string   `json:"b2"`
	Queries         []string `json:"queries"`
	Reference       []string `json:"reference"`
}

// adapter holds f(q) = normalize(q + alpha*(gelu(q@W1+b1)@W2+b2)).
// w1 is dim x hidden and w2 is hidden x dim, both row-major (numpy C order).
type adapter struct {
	dim, hidden    int
	alpha          float64
	w1, b1, w2, b2 []float64
}

func decodeF32(s string, n int) ([]float64, error) {
	raw, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		return nil, err
	}
	if len(raw) != 4*n {
		return nil, fmt.Errorf("got %d bytes, want %d", len(raw), 4*n)
	}
	out := make([]float64, n)
	for i := range out {
		out[i] = float64(math.Float32frombits(binary.LittleEndian.Uint32(raw[4*i:])))
	}
	return out, nil
}

func geluTanh(x float64) float64 {
	return 0.5 * x * (1 + math.Tanh(0.79788456*(x+0.044715*x*x*x)))
}

// plainApply is the float64 reference of the pilot's reference.apply.
func (a *adapter) plainApply(x []float64) []float64 {
	h := make([]float64, a.hidden)
	for i := 0; i < a.hidden; i++ {
		z := a.b1[i]
		for j := 0; j < a.dim; j++ {
			z += x[j] * a.w1[j*a.hidden+i]
		}
		h[i] = geluTanh(z)
	}
	out := make([]float64, a.dim)
	for k := 0; k < a.dim; k++ {
		d := a.b2[k]
		for i := 0; i < a.hidden; i++ {
			d += h[i] * a.w2[i*a.dim+k]
		}
		out[k] = x[k] + a.alpha*d
	}
	return normalized(out)
}

func normalized(v []float64) []float64 {
	var n float64
	for _, x := range v {
		n += x * x
	}
	n = math.Sqrt(n) + 1e-9
	out := make([]float64, len(v))
	for i, x := range v {
		out[i] = x / n
	}
	return out
}

func parity(a, b []float64) (cos, maxAbs float64) {
	var dot, na, nb float64
	for i := range a {
		dot += a[i] * b[i]
		na += a[i] * a[i]
		nb += b[i] * b[i]
		maxAbs = math.Max(maxAbs, math.Abs(a[i]-b[i]))
	}
	return dot / (math.Sqrt(na)*math.Sqrt(nb) + 1e-300), maxAbs
}

type adapterParams struct {
	LogN            int     `json:"log_n"`
	LogQ            []int   `json:"log_q"`
	LogP            []int   `json:"log_p"`
	LogDefaultScale int     `json:"log_default_scale"`
	LogQP           float64 `json:"log_qp"`
	MaxLevel        int     `json:"max_level"`
	Slots           int     `json:"slots_used"`
	BSGSRatio       int     `json:"log_bsgs_ratio,omitempty"`
}

type adapterParity struct {
	VsPilotReferenceMinCos float64 `json:"vs_pilot_reference_min_cos"`
	VsPilotReferenceMaxAbs float64 `json:"vs_pilot_reference_max_abs"`
	VsGoFloat64MinCos      float64 `json:"vs_go_float64_min_cos"`
	VsGoFloat64MaxAbs      float64 `json:"vs_go_float64_max_abs"`
	PilotBarMinCos         float64 `json:"pilot_bar_min_cos"`
	PilotBarMaxAbs         float64 `json:"pilot_bar_max_abs"`
	PassesPilotBar         bool    `json:"passes_pilot_bar"`
}

type adapterResult struct {
	Experiment string        `json:"experiment"`
	Synthetic  bool          `json:"synthetic"`
	Host       hostInfo      `json:"host"`
	Mode       string        `json:"mode"`
	Params     adapterParams `json:"params"`
	GeluDegree int           `json:"gelu_degree"`
	GeluBound  float64       `json:"gelu_interval_bound"`
	Sizes      struct {
		GaloisKeys            int     `json:"galois_keys"`
		GaloisKeyMiB          float64 `json:"galois_keys_mib"`
		RelinKeyMiB           float64 `json:"relin_key_mib"`
		InputCiphertextKiB    float64 `json:"input_ciphertext_kib"`
		OutputCiphertextKiB   float64 `json:"output_ciphertext_kib"`
		OutputLevel           int     `json:"output_level"`
		PlaintextWeightsKiB   float64 `json:"plaintext_weights_f32_kib"`
		EncryptedWeightsMiB   float64 `json:"encrypted_weights_mib,omitempty"`
		PlaintextQueryF32Byte int     `json:"plaintext_query_f32_bytes"`
	} `json:"sizes"`
	Setup struct {
		KeyGenMs     float64 `json:"keygen_ms"`
		WeightPrepMs float64 `json:"weight_prep_ms"`
	} `json:"setup"`
	Ditto struct {
		Encrypt          latency `json:"encrypt"`
		DecryptNormalize latency `json:"decrypt_decode_normalize"`
	} `json:"ditto_side"`
	Miner struct {
		Layer1 latency `json:"layer1_768x128"`
		GELU   latency `json:"gelu_poly"`
		Layer2 latency `json:"layer2_128x768_residual"`
		Total  latency `json:"total"`
	} `json:"miner_side_single_thread"`
	Throughput struct {
		Workers       int     `json:"workers"`
		Queries       int     `json:"queries"`
		WallMs        float64 `json:"wall_ms"`
		QueriesPerSec float64 `json:"queries_per_sec"`
	} `json:"miner_throughput"`
	PlainGoApplyUs float64       `json:"plaintext_go_apply_us"`
	Parity         adapterParity `json:"parity"`
}

// adapterCircuit is everything a miner holds to evaluate one tenant's adapter
// on an encrypted query: evaluation keys plus plaintext or encrypted weights.
type adapterCircuit struct {
	params ckks.Parameters
	evk    rlwe.EvaluationKeySet
	poly   bignum.Polynomial
	hidden int
	// plain mode
	lt1, lt2     lintrans.LinearTransformation
	bias1, bias2 []float64
	// encrypted mode
	m1, m2         *encMatrix
	ctB1, ctB2     *rlwe.Ciphertext
	encryptedModel bool
}

type stageTimes struct{ layer1, gelu, layer2 time.Duration }

func (c *adapterCircuit) eval(ev *ckks.Evaluator, ctX *rlwe.Ciphertext) (*rlwe.Ciphertext, stageTimes, error) {
	var st stageTimes
	t0 := time.Now()
	var ctT *rlwe.Ciphertext
	var err error
	if c.encryptedModel {
		ctT, err = c.m1.apply(ev, ctX)
	} else {
		ctT, err = lintrans.NewEvaluator(ev).EvaluateNew(ctX, c.lt1)
	}
	if err != nil {
		return nil, st, fmt.Errorf("layer1: %w", err)
	}
	// Sum the 8 partial windows; every 128-slot block then holds the hidden vector.
	if err = ev.InnerSum(ctT, c.hidden, adapterSlots/c.hidden, ctT); err != nil {
		return nil, st, fmt.Errorf("inner sum: %w", err)
	}
	if err = ev.Rescale(ctT, ctT); err != nil {
		return nil, st, err
	}
	if c.encryptedModel {
		err = ev.Add(ctT, c.ctB1, ctT)
	} else {
		err = ev.Add(ctT, c.bias1, ctT)
	}
	if err != nil {
		return nil, st, err
	}
	t1 := time.Now()
	ctH, err := polynomial.NewEvaluator(c.params, ev).Evaluate(ctT, c.poly, c.params.DefaultScale())
	if err != nil {
		return nil, st, fmt.Errorf("gelu: %w", err)
	}
	t2 := time.Now()
	var ctD *rlwe.Ciphertext
	if c.encryptedModel {
		ctD, err = c.m2.apply(ev, ctH)
	} else {
		ctD, err = lintrans.NewEvaluator(ev).EvaluateNew(ctH, c.lt2)
	}
	if err != nil {
		return nil, st, fmt.Errorf("layer2 (gelu output level %d): %w", ctH.Level(), err)
	}
	if err = ev.Rescale(ctD, ctD); err != nil {
		return nil, st, err
	}
	if c.encryptedModel {
		err = ev.Add(ctD, c.ctB2, ctD)
	} else {
		err = ev.Add(ctD, c.bias2, ctD)
	}
	if err != nil {
		return nil, st, err
	}
	xLow := ev.DropLevelNew(ctX, ctX.Level()-ctD.Level())
	if err = ev.Add(ctD, xLow, ctD); err != nil {
		return nil, st, fmt.Errorf("residual: %w", err)
	}
	// Ditto normalizes after decryption, so the miner returns q + alpha*delta.
	ev.DropLevel(ctD, ctD.Level())
	t3 := time.Now()
	st = stageTimes{layer1: t1.Sub(t0), gelu: t2.Sub(t1), layer2: t3.Sub(t2)}
	return ctD, st, nil
}

func runAdapter(argv []string) (any, error) {
	fs := flag.NewFlagSet("adapter", flag.ExitOnError)
	fixturePath := fs.String("fixture", "", "fixture JSON from python/export_adapter_fixture.py")
	mode := fs.String("mode", "plain", "plain: weights in the clear on the miner; encrypted: Ditto-encrypted weights")
	degree := fs.Int("degree", 15, "Chebyshev degree of the GELU approximation")
	nQueries := fs.Int("queries", 16, "queries timed one at a time")
	workers := fs.Int("workers", 10, "goroutines for the throughput phase")
	n1 := fs.Int("bsgs-n1", 16, "baby steps for the encrypted-weights product")
	nP := fs.Int("p-primes", 1, "61-bit key-switching primes (more primes: fewer gadget digits, faster key switching, larger LogQP)")
	bsgsRatio := fs.Int("bsgs-ratio", 2, "log2 baby-step/giant-step ratio for plaintext-weight transforms")
	_ = fs.Parse(argv)
	if *mode != "plain" && *mode != "encrypted" {
		return nil, fmt.Errorf("unknown -mode %q", *mode)
	}
	if *degree < 1 {
		return nil, fmt.Errorf("-degree must be >= 1, got %d", *degree)
	}
	if *nQueries < 1 {
		return nil, fmt.Errorf("-queries must be >= 1, got %d", *nQueries)
	}
	if *mode == "encrypted" && *n1 < 1 {
		return nil, fmt.Errorf("-bsgs-n1 must be >= 1, got %d", *n1)
	}

	raw, err := os.ReadFile(*fixturePath)
	if err != nil {
		return nil, err
	}
	var fx adapterFixture
	if err := json.Unmarshal(raw, &fx); err != nil {
		return nil, err
	}
	if !fx.Synthetic {
		return nil, fmt.Errorf("refusing a fixture not marked synthetic")
	}
	a := &adapter{dim: fx.Dim, hidden: fx.Hidden, alpha: fx.Alpha}
	if a.w1, err = decodeF32(fx.W1, fx.Dim*fx.Hidden); err != nil {
		return nil, fmt.Errorf("W1: %w", err)
	}
	if a.b1, err = decodeF32(fx.B1, fx.Hidden); err != nil {
		return nil, fmt.Errorf("b1: %w", err)
	}
	if a.w2, err = decodeF32(fx.W2, fx.Hidden*fx.Dim); err != nil {
		return nil, fmt.Errorf("W2: %w", err)
	}
	if a.b2, err = decodeF32(fx.B2, fx.Dim); err != nil {
		return nil, fmt.Errorf("b2: %w", err)
	}
	if adapterSlots%a.hidden != 0 || a.dim > adapterSlots {
		return nil, fmt.Errorf("shape %dx%d does not tile %d slots", a.dim, a.hidden, adapterSlots)
	}
	n := min(*nQueries, len(fx.Queries))
	if n == 0 || len(fx.Reference) < n {
		return nil, fmt.Errorf("fixture has %d queries and %d references; need at least one of each",
			len(fx.Queries), len(fx.Reference))
	}
	queries := make([][]float64, n)
	refs := make([][]float64, n)
	for i := 0; i < n; i++ {
		if queries[i], err = decodeF32(fx.Queries[i], a.dim); err != nil {
			return nil, err
		}
		if refs[i], err = decodeF32(fx.Reference[i], a.dim); err != nil {
			return nil, err
		}
	}

	res := adapterResult{Experiment: "sn473-ckks-adapter", Synthetic: true, Host: newHostInfo(), Mode: *mode, GeluDegree: *degree}

	// Depth: layer 1 (1) + Chebyshev GELU (ceil(log2(deg+1))) + layer 2 (1).
	polyDepth := int(math.Ceil(math.Log2(float64(*degree + 1))))
	levels := 2 + polyDepth
	logQ := []int{55}
	for i := 0; i < levels; i++ {
		logQ = append(logQ, 45)
	}
	logP := make([]int, max(1, *nP))
	for i := range logP {
		logP[i] = 61
	}
	params, err := ckks.NewParametersFromLiteral(ckks.ParametersLiteral{
		LogN: 14, LogQ: logQ, LogP: logP, LogDefaultScale: 45,
	})
	if err != nil {
		return nil, err
	}
	if params.LogQP() > 438 {
		return nil, fmt.Errorf("logQP %.1f exceeds the 128-bit bound 438 for N=2^14", params.LogQP())
	}
	res.Params = adapterParams{LogN: 14, LogQ: logQ, LogP: logP, LogDefaultScale: 45, LogQP: params.LogQP(), MaxLevel: params.MaxLevel(), Slots: adapterSlots, BSGSRatio: *bsgsRatio}

	// GELU on [-B, B], where B bounds every pre-activation exactly for unit-norm
	// queries; Ditto derives it from the adapter weights.
	bound := fx.GeluInputBound * 1.01
	res.GeluBound = bound
	prec := params.EncodingPrecision()
	geluC := func(x complex128) complex128 {
		return 0.5 * x * (1 + cmplx.Tanh(complex(0.79788456, 0)*(x+complex(0.044715, 0)*x*x*x)))
	}
	// In Lattigo v6.2.0, Nodes is the degree of the interpolant.
	poly := bignum.ChebyshevApproximation(geluC, bignum.Interval{
		Nodes: *degree, A: *bignum.NewFloat(-bound, prec), B: *bignum.NewFloat(bound, prec),
	})
	if poly.Degree() != *degree {
		return nil, fmt.Errorf("GELU interpolant has degree %d, want %d", poly.Degree(), *degree)
	}
	mulBig, addBig := poly.ChangeOfBasis()
	s, _ := mulBig.Float64()
	c, _ := addBig.Float64()

	// Layer 1 diagonals (change of basis folded in): d_k[i] = s*W1[(i+k)%1024][i%128].
	diag1 := make(map[int][]float64, a.hidden)
	for k := 0; k < a.hidden; k++ {
		d := make([]float64, adapterSlots)
		for i := 0; i < adapterSlots; i++ {
			if col := (i + k) % adapterSlots; col < a.dim {
				d[i] = s * a.w1[col*a.hidden+i%a.hidden]
			}
		}
		diag1[k] = d
	}
	bias1 := make([]float64, adapterSlots)
	for i := range bias1 {
		bias1[i] = s*a.b1[i%a.hidden] + c
	}
	// Layer 2 diagonals (alpha folded in): d_k[i] = alpha*W2[(i+k)%128][i] for i < 768.
	diag2 := make(map[int][]float64, a.hidden)
	for k := 0; k < a.hidden; k++ {
		d := make([]float64, adapterSlots)
		for i := 0; i < a.dim; i++ {
			d[i] = a.alpha * a.w2[((i+k)%a.hidden)*a.dim+i]
		}
		diag2[k] = d
	}
	bias2 := make([]float64, adapterSlots)
	for i := 0; i < a.dim; i++ {
		bias2[i] = a.alpha * a.b2[i]
	}

	kgen := rlwe.NewKeyGenerator(params)
	sk := kgen.GenSecretKeyNew()
	ecd := ckks.NewEncoder(params)
	enc := rlwe.NewEncryptor(params, sk)
	dec := rlwe.NewDecryptor(params, sk)
	dims := ring.Dimensions{Rows: 0, Cols: adapterLogSlots}
	geluLevel := params.MaxLevel() - 1 - polyDepth

	circ := &adapterCircuit{params: params, poly: poly, hidden: a.hidden, encryptedModel: *mode == "encrypted"}
	var galEls []uint64
	tPrep := time.Now()
	if circ.encryptedModel {
		if circ.m1, err = newEncMatrix(params, ecd, enc, diag1, *n1, params.MaxLevel(), adapterLogSlots); err != nil {
			return nil, err
		}
		if circ.m2, err = newEncMatrix(params, ecd, enc, diag2, *n1, geluLevel, adapterLogSlots); err != nil {
			return nil, err
		}
		encVec := func(v []float64, level int) (*rlwe.Ciphertext, error) {
			pt := ckks.NewPlaintext(params, level)
			pt.LogDimensions = dims
			if err := ecd.Encode(v, pt); err != nil {
				return nil, err
			}
			return enc.EncryptNew(pt)
		}
		if circ.ctB1, err = encVec(bias1, params.MaxLevel()-1); err != nil {
			return nil, err
		}
		if circ.ctB2, err = encVec(bias2, geluLevel-1); err != nil {
			return nil, err
		}
		galEls = rotationElements(params, append(circ.m1.rotations(), circ.m2.rotations()...))
		res.Sizes.EncryptedWeightsMiB = mib(circ.m1.bytes() + circ.m2.bytes() + circ.ctB1.BinarySize() + circ.ctB2.BinarySize())
	} else {
		mkLT := func(diags map[int][]float64, level int) (lintrans.LinearTransformation, []uint64, error) {
			d := lintrans.Diagonals[float64](diags)
			ltp := lintrans.Parameters{
				DiagonalsIndexList:        d.DiagonalsIndexList(),
				LevelQ:                    level,
				LevelP:                    params.MaxLevelP(),
				Scale:                     rlwe.NewScale(params.Q()[level]),
				LogDimensions:             dims,
				LogBabyStepGiantStepRatio: *bsgsRatio,
			}
			lt := lintrans.NewTransformation(params, ltp)
			if err := lintrans.Encode(ecd, d, lt); err != nil {
				return lt, nil, err
			}
			return lt, lintrans.GaloisElements(params, ltp), nil
		}
		var g1, g2 []uint64
		if circ.lt1, g1, err = mkLT(diag1, params.MaxLevel()); err != nil {
			return nil, err
		}
		if circ.lt2, g2, err = mkLT(diag2, geluLevel); err != nil {
			return nil, err
		}
		circ.bias1, circ.bias2 = bias1, bias2
		galEls = append(g1, g2...)
	}
	res.Setup.WeightPrepMs = float64(time.Since(tPrep).Microseconds()) / 1e3
	galEls = append(galEls, params.GaloisElementsForInnerSum(a.hidden, adapterSlots/a.hidden)...)

	tKey := time.Now()
	rlk := kgen.GenRelinearizationKeyNew(sk)
	gks, gkBytes := galoisKeysFor(params, kgen, sk, galEls)
	res.Setup.KeyGenMs = float64(time.Since(tKey).Microseconds()) / 1e3
	circ.evk = rlwe.NewMemEvaluationKeySet(rlk, gks...)
	res.Sizes.GaloisKeys = len(gks)
	res.Sizes.GaloisKeyMiB = mib(gkBytes)
	res.Sizes.RelinKeyMiB = mib(rlk.BinarySize())
	res.Sizes.PlaintextWeightsKiB = float64(4*(2*a.dim*a.hidden+a.dim+a.hidden)) / 1024
	res.Sizes.PlaintextQueryF32Byte = 4 * a.dim

	// Ditto side: encrypt each query (padded to 1024 slots).
	cts := make([]*rlwe.Ciphertext, n)
	var encT []time.Duration
	for i, q := range queries {
		t := time.Now()
		v := make([]float64, adapterSlots)
		copy(v, q)
		pt := ckks.NewPlaintext(params, params.MaxLevel())
		pt.LogDimensions = dims
		if err := ecd.Encode(v, pt); err != nil {
			return nil, err
		}
		if cts[i], err = enc.EncryptNew(pt); err != nil {
			return nil, err
		}
		encT = append(encT, time.Since(t))
	}
	res.Ditto.Encrypt = summarize(encT)
	res.Sizes.InputCiphertextKiB = float64(cts[0].BinarySize()) / 1024

	// Miner side, one query at a time on one goroutine.
	ev := ckks.NewEvaluator(params, circ.evk)
	if _, _, err := circ.eval(ev, cts[0]); err != nil { // warm-up
		return nil, err
	}
	outs := make([]*rlwe.Ciphertext, n)
	var l1, gl, l2, tot []time.Duration
	for i := range cts {
		out, st, err := circ.eval(ev, cts[i])
		if err != nil {
			return nil, err
		}
		outs[i] = out
		l1, gl, l2 = append(l1, st.layer1), append(gl, st.gelu), append(l2, st.layer2)
		tot = append(tot, st.layer1+st.gelu+st.layer2)
	}
	res.Miner.Layer1, res.Miner.GELU, res.Miner.Layer2, res.Miner.Total = summarize(l1), summarize(gl), summarize(l2), summarize(tot)
	res.Sizes.OutputCiphertextKiB = float64(outs[0].BinarySize()) / 1024
	res.Sizes.OutputLevel = outs[0].Level()

	// Ditto side: decrypt, normalize, and check parity.
	res.Parity = adapterParity{VsPilotReferenceMinCos: 2, VsGoFloat64MinCos: 2, PilotBarMinCos: fx.ParityMinCosine, PilotBarMaxAbs: fx.ParityMaxAbs}
	var decT []time.Duration
	for i, out := range outs {
		t := time.Now()
		pt := dec.DecryptNew(out)
		vals := make([]float64, adapterSlots)
		if err := ecd.Decode(pt, vals); err != nil {
			return nil, err
		}
		got := normalized(vals[:a.dim])
		decT = append(decT, time.Since(t))
		cosR, absR := parity(got, refs[i])
		cosG, absG := parity(got, a.plainApply(queries[i]))
		res.Parity.VsPilotReferenceMinCos = math.Min(res.Parity.VsPilotReferenceMinCos, cosR)
		res.Parity.VsPilotReferenceMaxAbs = math.Max(res.Parity.VsPilotReferenceMaxAbs, absR)
		res.Parity.VsGoFloat64MinCos = math.Min(res.Parity.VsGoFloat64MinCos, cosG)
		res.Parity.VsGoFloat64MaxAbs = math.Max(res.Parity.VsGoFloat64MaxAbs, absG)
	}
	res.Ditto.DecryptNormalize = summarize(decT)
	res.Parity.PassesPilotBar = res.Parity.VsPilotReferenceMinCos >= fx.ParityMinCosine && res.Parity.VsPilotReferenceMaxAbs <= fx.ParityMaxAbs

	// Plaintext baseline in Go for scale.
	t := time.Now()
	const reps = 200
	for r := 0; r < reps; r++ {
		_ = a.plainApply(queries[r%n])
	}
	res.PlainGoApplyUs = float64(time.Since(t).Nanoseconds()) / 1e3 / reps

	// Throughput: each worker owns an evaluator and shares keys and weights.
	w := max(1, *workers)
	total := max(n, 2*w)
	var wg sync.WaitGroup
	errs := make(chan error, w)
	next := make(chan int, total)
	for i := 0; i < total; i++ {
		next <- i
	}
	close(next)
	start := time.Now()
	for j := 0; j < w; j++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			wev := ckks.NewEvaluator(params, circ.evk)
			for i := range next {
				if _, _, err := circ.eval(wev, cts[i%n]); err != nil {
					errs <- err
					return
				}
			}
		}()
	}
	wg.Wait()
	close(errs)
	if err := <-errs; err != nil {
		return nil, err
	}
	wall := time.Since(start)
	res.Throughput.Workers = w
	res.Throughput.Queries = total
	res.Throughput.WallMs = float64(wall.Microseconds()) / 1e3
	res.Throughput.QueriesPerSec = float64(total) / wall.Seconds()
	return res, nil
}
