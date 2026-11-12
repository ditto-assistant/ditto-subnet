// Command storage measures the client-side cost of the storage track on one
// Apple Silicon host: AES-256-GCM sealing, Reed-Solomon erasure coding at the
// parameters public storage networks use, and SHA-256 Merkle storage proofs.
//
// EXPERIMENT ONLY. Inputs are pseudorandom synthetic bytes; nothing touches a
// network or a disk beyond process memory.
package main

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"os"
	"runtime"
	"sync"
	"time"

	"github.com/klauspost/reedsolomon"
)

const (
	mib       = 1 << 20
	objectMiB = 64
	chunkSize = 1 * mib
	leafSize  = 4096
)

type throughput struct {
	Goroutines int     `json:"goroutines"`
	MiB        int     `json:"mib"`
	MiBPerSec  float64 `json:"mib_per_sec"`
}

type rsResult struct {
	K                  int     `json:"k_data"`
	M                  int     `json:"m_parity"`
	N                  int     `json:"n_total"`
	Expansion          float64 `json:"expansion"`
	ShardKiB           int     `json:"shard_kib"`
	EncodeMiBPerSec    float64 `json:"encode_input_mib_per_sec"`
	ReconstructMiBPerS float64 `json:"reconstruct_mib_per_sec_with_m_erasures"`
}

type merkleResult struct {
	ShardMiB        int     `json:"shard_mib"`
	Leaves          int     `json:"leaves"`
	LeafBytes       int     `json:"leaf_bytes"`
	BuildMs         float64 `json:"build_ms"`
	ProofBytes      int     `json:"proof_bytes_excluding_leaf"`
	ProveUs         float64 `json:"prove_us"`
	VerifyUs        float64 `json:"verify_us"`
	ChallengeBytes  int     `json:"response_bytes_including_leaf"`
	SHA256MiBPerSec float64 `json:"sha256_mib_per_sec"`
}

type result struct {
	Experiment string `json:"experiment"`
	Synthetic  bool   `json:"synthetic"`
	Host       struct {
		GoVersion string `json:"go_version"`
		GOARCH    string `json:"goarch"`
		NumCPU    int    `json:"num_cpu"`
	} `json:"host"`
	AESGCMSeal []throughput  `json:"aes256gcm_seal"`
	AESGCMOpen []throughput  `json:"aes256gcm_open"`
	RS         []rsResult    `json:"reed_solomon"`
	Merkle     merkleResult  `json:"merkle_sha256"`
	Notes      []string      `json:"notes"`
	Elapsed    time.Duration `json:"elapsed_ns"`
}

func main() {
	start := time.Now()
	var res result
	res.Experiment, res.Synthetic = "sn473-storage", true
	res.Host.GoVersion, res.Host.GOARCH, res.Host.NumCPU = runtime.Version(), runtime.GOARCH, runtime.NumCPU()

	r := rand.New(rand.NewPCG(473, 1))
	object := make([]byte, objectMiB*mib)
	for i := 0; i+8 <= len(object); i += 8 {
		binary.LittleEndian.PutUint64(object[i:], r.Uint64())
	}

	key := make([]byte, 32)
	for i := range key {
		key[i] = byte(r.Uint32())
	}
	block, err := aes.NewCipher(key)
	check(err)
	aead, err := cipher.NewGCM(block)
	check(err)

	for _, g := range []int{1, 10} {
		seal, open := aesThroughput(aead, object, g)
		res.AESGCMSeal = append(res.AESGCMSeal, seal)
		res.AESGCMOpen = append(res.AESGCMOpen, open)
	}

	for _, km := range [][2]int{{4, 8}, {10, 20}, {16, 16}, {29, 51}} {
		res.RS = append(res.RS, rsBench(object, km[0], km[1]))
	}

	res.Merkle = merkleBench(object[:16*mib])
	res.Notes = []string{
		"one 64 MiB synthetic object; AES-GCM seals 1 MiB chunks with unique 96-bit nonces",
		"Reed-Solomon via github.com/klauspost/reedsolomon defaults (NEON on arm64)",
		"reconstruct erases m shards, data shards first, then rebuilds the full set",
		"Merkle leaves are 4 KiB; a storage-proof response is the leaf plus log2(leaves) sibling hashes",
	}
	res.Elapsed = time.Since(start)
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	check(enc.Encode(res))
}

func aesThroughput(aead cipher.AEAD, object []byte, goroutines int) (throughput, throughput) {
	chunks := len(object) / chunkSize
	sealed := make([][]byte, chunks)
	nonce := func(i int) []byte {
		n := make([]byte, aead.NonceSize())
		binary.LittleEndian.PutUint64(n, uint64(i)+1)
		return n
	}
	const rounds = 4
	run := func(work func(i int)) float64 {
		t := time.Now()
		for round := 0; round < rounds; round++ {
			var wg sync.WaitGroup
			next := make(chan int, chunks)
			for i := 0; i < chunks; i++ {
				next <- i
			}
			close(next)
			for g := 0; g < goroutines; g++ {
				wg.Add(1)
				go func() {
					defer wg.Done()
					for i := range next {
						work(i)
					}
				}()
			}
			wg.Wait()
		}
		return float64(rounds*len(object)) / mib / time.Since(t).Seconds()
	}
	sealRate := run(func(i int) {
		sealed[i] = aead.Seal(nil, nonce(i), object[i*chunkSize:(i+1)*chunkSize], nil)
	})
	openRate := run(func(i int) {
		_, err := aead.Open(nil, nonce(i), sealed[i], nil)
		check(err)
	})
	mibTotal := len(object) / mib
	return throughput{goroutines, mibTotal, sealRate}, throughput{goroutines, mibTotal, openRate}
}

func rsBench(object []byte, k, m int) rsResult {
	enc, err := reedsolomon.New(k, m)
	check(err)
	const rounds = 4
	var shards [][]byte
	t := time.Now()
	for round := 0; round < rounds; round++ {
		shards, err = enc.Split(object)
		check(err)
		check(enc.Encode(shards))
	}
	encodeRate := float64(rounds*len(object)) / mib / time.Since(t).Seconds()

	var recon time.Duration
	for round := 0; round < rounds; round++ {
		work := make([][]byte, len(shards))
		copy(work, shards)
		for i := 0; i < m; i++ {
			work[i] = nil // lose data shards first: the worst case for decode
		}
		t := time.Now()
		check(enc.Reconstruct(work))
		recon += time.Since(t)
		ok, err := enc.Verify(work)
		check(err)
		if !ok {
			panic("reconstructed shards do not verify")
		}
	}
	return rsResult{
		K: k, M: m, N: k + m,
		Expansion:          float64(k+m) / float64(k),
		ShardKiB:           len(shards[0]) / 1024,
		EncodeMiBPerSec:    encodeRate,
		ReconstructMiBPerS: float64(rounds*len(object)) / mib / recon.Seconds(),
	}
}

func merkleBench(shard []byte) merkleResult {
	leaves := len(shard) / leafSize
	t := time.Now()
	levels := [][][32]byte{make([][32]byte, leaves)}
	for i := 0; i < leaves; i++ {
		levels[0][i] = sha256.Sum256(append([]byte{0}, shard[i*leafSize:(i+1)*leafSize]...))
	}
	for len(levels[len(levels)-1]) > 1 {
		prev := levels[len(levels)-1]
		next := make([][32]byte, (len(prev)+1)/2)
		for i := range next {
			left, right := prev[2*i], prev[min(2*i+1, len(prev)-1)]
			next[i] = sha256.Sum256(append(append([]byte{1}, left[:]...), right[:]...))
		}
		levels = append(levels, next)
	}
	build := time.Since(t)
	root := levels[len(levels)-1][0]

	const proofs = 2000
	r := rand.New(rand.NewPCG(4730, 2))
	var proveT, verifyT time.Duration
	var proofLen int
	for p := 0; p < proofs; p++ {
		idx := r.IntN(leaves)
		t := time.Now()
		var path [][32]byte
		for l, i := 0, idx; l < len(levels)-1; l, i = l+1, i/2 {
			path = append(path, levels[l][min(i^1, len(levels[l])-1)])
		}
		leaf := shard[idx*leafSize : (idx+1)*leafSize]
		proveT += time.Since(t)
		proofLen = len(path) * 32

		t = time.Now()
		h := sha256.Sum256(append([]byte{0}, leaf...))
		for l, i := 0, idx; l < len(path); l, i = l+1, i/2 {
			if i%2 == 0 {
				h = sha256.Sum256(append(append([]byte{1}, h[:]...), path[l][:]...))
			} else {
				h = sha256.Sum256(append(append([]byte{1}, path[l][:]...), h[:]...))
			}
		}
		verifyT += time.Since(t)
		if h != root {
			panic(fmt.Sprintf("proof %d does not verify", p))
		}
	}
	return merkleResult{
		ShardMiB:        len(shard) / mib,
		Leaves:          leaves,
		LeafBytes:       leafSize,
		BuildMs:         float64(build.Microseconds()) / 1e3,
		ProofBytes:      proofLen,
		ProveUs:         float64(proveT.Nanoseconds()) / 1e3 / proofs,
		VerifyUs:        float64(verifyT.Nanoseconds()) / 1e3 / proofs,
		ChallengeBytes:  proofLen + leafSize,
		SHA256MiBPerSec: float64(len(shard)) / mib / build.Seconds(),
	}
}

func check(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}
