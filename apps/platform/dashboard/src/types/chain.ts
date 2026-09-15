/** Public `/public/chain` snapshot types. */

export interface PublicAxonInfo {
  ip: string | null;
  port: number | null;
  version: number | null;
}

export interface PublicNeuron {
  uid: number;
  hotkey: string;
  coldkey: string;
  stake: number;
  validator_permit: boolean;
  is_active: boolean;
  incentive: number;
  dividends: number;
  trust: number;
  consensus: number;
  emission: number;
  last_update: number;
  validator_trust: number;
  axon: PublicAxonInfo;
}

export interface PublicChainTotals {
  neuron_count: number;
  validator_count: number;
  miner_count: number;
  total_stake: number;
  total_emission: number;
}

export interface PublicRegistrationInfo {
  recycle_rao: number | null;
  recycle_tao: number | null;
  immunity_period: number | null;
  block: number | null;
}

export interface PublicMarketQuote {
  status: "disabled" | "fresh" | "stale" | "unavailable";
  refreshed_at: string | null;
  alpha_tao: number | null;
  alpha_usd: number | null;
  market_cap_tao: number | null;
  market_cap_usd: number | null;
  source?: "chain" | "taostats" | "none";
}

export interface PublicChainEpoch {
  tempo_blocks: number;
  block_seconds: number;
  epoch_seconds: number;
  last_epoch_block: number;
  next_epoch_block: number;
  blocks_since_last_epoch: number;
  blocks_until_next_epoch: number;
  next_epoch_at: string;
  commit_reveal_enabled: boolean | null;
  reveal_period_epochs: number | null;
  weights_rate_limit_blocks: number | null;
}

export interface PublicChainResponse {
  generated_at: string;
  netuid: number;
  block: number | null;
  stale: boolean;
  age_seconds: number;
  tao_usd: number | null;
  registration: PublicRegistrationInfo;
  market: PublicMarketQuote;
  epoch: PublicChainEpoch | null;
  totals: PublicChainTotals;
  metagraph: PublicNeuron[];
}
