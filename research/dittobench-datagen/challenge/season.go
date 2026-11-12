package challenge

import (
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"strings"
)

// Dueling season derivations (docs/dueling-bench.md section 7.2).
//
// A season pins the generator columns every canonical agent run draws its
// slice from. Like a round, it commits to a secret before the block that
// anchors it exists, so neither Platform (which cannot grind the block) nor a
// generator owner (who cannot see the secret) can compute a run's slice before
// the run. The derivations reuse the round shape (RoundCommitment, RoundSeed,
// RoundColumnSeed) under their own domain tags, so a season value can never
// collide with a round value.
//
// Every function returns lowercase hex and is mirrored byte for byte by
// ditto_screening_protocol.dueling (testdata/season_vectors.json).
const (
	seasonCommitDomain = "dittobench-dueling-season-commit/v1"
	seasonSeedDomain   = "dittobench-dueling-season-seed/v1"
	sliceSeedDomain    = "dittobench-dueling-slice/v1"
)

// SeasonSecretBytes is the length of a season secret.
const SeasonSecretBytes = 32

func checkSeasonSecret(secret []byte) error {
	if len(secret) != SeasonSecretBytes {
		return errors.New("season secret must be 32 bytes")
	}
	return nil
}

// SeasonCommitment is the public commitment to a season secret:
//
//	sha256("dittobench-dueling-season-commit/v1" ‖ 0x00 ‖ u64be(season) ‖ secret)
//
// season is the season index (the generator round index the season opens
// on). Platform publishes the commitment before the season's anchor block
// exists and reveals the secret after the privacy window.
func SeasonCommitment(season uint64, secret []byte) (string, error) {
	if err := checkSeasonSecret(secret); err != nil {
		return "", err
	}
	h := sha256.New()
	h.Write([]byte(seasonCommitDomain))
	h.Write([]byte{0})
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], season)
	h.Write(b[:])
	h.Write(secret)
	return hex.EncodeToString(h.Sum(nil)), nil
}

// SeasonSeed derives a season's seed from its committed secret and the hash
// of the season's anchor block (the opening round's block):
//
//	sha256("dittobench-dueling-season-seed/v1" ‖ 0x00 ‖ u64be(season) ‖ secret ‖ block32)
//
// The block hash is 32 bytes of hex, with or without a 0x prefix, in either
// case.
func SeasonSeed(season uint64, secret []byte, anchorBlockHash string) (string, error) {
	if err := checkSeasonSecret(secret); err != nil {
		return "", err
	}
	block, err := hex.DecodeString(strings.TrimPrefix(strings.ToLower(strings.TrimSpace(anchorBlockHash)), "0x"))
	if err != nil || len(block) != 32 {
		return "", errors.New("season anchor block hash must be 32 bytes of hex")
	}
	h := sha256.New()
	h.Write([]byte(seasonSeedDomain))
	h.Write([]byte{0})
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], season)
	h.Write(b[:])
	h.Write(secret)
	h.Write(block)
	return hex.EncodeToString(h.Sum(nil)), nil
}

// SliceSeed derives one slice column's GenerateRequest seed for one agent run:
//
//	sha256("dittobench-dueling-slice/v1" ‖ 0x00 ‖ season_seed_hex ‖ 0x00 ‖ u64be(run_seed) ‖ 0x00 ‖ column_id)
//
// seasonSeed is the season seed as 64 lowercase hex characters (hashed as
// that text, like RoundColumnSeed), runSeed is the ticket's datagen seed, and
// columnID names the slice column. Every column of every run gets its own
// seed, so the public default generator can fill several columns of one run
// without repeating cases.
func SliceSeed(seasonSeed string, runSeed int64, columnID string) (string, error) {
	if !shaPattern.MatchString(seasonSeed) {
		return "", errors.New("season seed must be 64 lowercase hex characters")
	}
	if runSeed < 0 {
		return "", errors.New("slice run seed must be non-negative")
	}
	if columnID == "" {
		return "", errors.New("slice column id must be non-empty")
	}
	h := sha256.New()
	h.Write([]byte(sliceSeedDomain))
	h.Write([]byte{0})
	h.Write([]byte(seasonSeed))
	h.Write([]byte{0})
	var b [8]byte
	binary.BigEndian.PutUint64(b[:], uint64(runSeed))
	h.Write(b[:])
	h.Write([]byte{0})
	h.Write([]byte(columnID))
	return hex.EncodeToString(h.Sum(nil)), nil
}
