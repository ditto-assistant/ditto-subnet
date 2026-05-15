package chain

// Config holds the parameters Pylon needs to authenticate this client and
// the subtensor identifier used for events that Pylon does not surface.
//
// Mirrors ditto.chain.models.ChainConfig.
type Config struct {
	// PylonURL is the HTTP base URL of the Pylon service.
	PylonURL string

	// IdentityName + IdentityToken authenticate the Pylon caller.
	IdentityName  string
	IdentityToken string

	// Netuid is the Bittensor subnet this client operates against (118
	// for Ditto). Pylon binds netuid at the service level rather than
	// per call, so this is informational on the client side.
	Netuid int

	// SubtensorNetwork picks the substrate WebSocket URL for event
	// reads (the Pylon gap). One of "finney" (mainnet), "test"
	// (testnet), "local", or a full ws:// URL.
	SubtensorNetwork string

	// ArchiveBlocksCutoff is the recent-block window served from the
	// live node; older blocks go through Pylon's archive fallback.
	ArchiveBlocksCutoff int
}

// NeuronInfo is a neuron registered on the subnet at a point in time.
//
// Mirrors ditto.chain.models.NeuronInfo; the JSON tags match the Pylon
// response shape so Pylon JSON decodes straight into this struct.
type NeuronInfo struct {
	Hotkey          string         `json:"hotkey"`
	Coldkey         string         `json:"coldkey"`
	UID             int            `json:"uid"`
	Stake           float64        `json:"stake"`
	AxonInfo        map[string]any `json:"axon_info,omitempty"`
	IsActive        bool           `json:"active"`
	ValidatorPermit bool           `json:"validator_permit"`
}

// BlockInfo identifies a single block by number, hash, and timestamp.
//
// Mirrors ditto.chain.models.BlockInfo.
type BlockInfo struct {
	Number    int    `json:"number"`
	Hash      string `json:"hash"`
	Timestamp int64  `json:"timestamp"`
}

// ExtrinsicInfo describes a single extrinsic at a known
// (block_number, extrinsic_index). Pylon's response does NOT include the
// block hash, so Succeeded cannot be auto-resolved from a single
// Extrinsic() call; use CheckExtrinsicSuccess separately when the caller
// holds the block hash.
//
// Mirrors ditto.chain.models.ExtrinsicInfo.
type ExtrinsicInfo struct {
	BlockNumber    int            `json:"block_number"`
	ExtrinsicIndex int            `json:"extrinsic_index"`
	ExtrinsicHash  string         `json:"extrinsic_hash"`
	CallModule     string         `json:"call_module"`
	CallFunction   string         `json:"call_function"`
	CallArgs       map[string]any `json:"call_args,omitempty"`
	SignerAddress  string         `json:"signer_address"`

	// Succeeded is nil until CheckExtrinsicSuccess fills it in. Pointer
	// so the "unresolved" state is distinguishable from "explicitly
	// false". Mirrors the Python ``succeeded: bool | None``.
	Succeeded *bool `json:"succeeded,omitempty"`
}
