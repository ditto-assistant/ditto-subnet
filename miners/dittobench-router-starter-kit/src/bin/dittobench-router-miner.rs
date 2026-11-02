use anyhow::{Context, Result};
use clap::Parser;
use dittobench_router_starter_kit::relay::{RELAY_BASE_URL_ENV, RELAY_TICKET_ENV};
use dittobench_router_starter_kit::reroute::RerouteConfig;
use dittobench_router_starter_kit::{router, Relay, RouterService};

/// Environment fallbacks for the reference lever (see `.env.example`).
const REROUTE_ENV: &str = "DITTOBENCH_ROUTER_REROUTE_ASIDES";
const ASIDE_MODEL_ENV: &str = "DITTOBENCH_ROUTER_ASIDE_MODEL";

#[derive(Debug, Parser)]
#[command(
    name = "dittobench-router-miner",
    version,
    about = "Shadow-only DittoBench router reference harness (contract v1)"
)]
struct Cli {
    #[arg(long, default_value_t = 8080)]
    port: u16,
    /// Bind loopback only and tolerate a missing relay, for local practice of
    /// `/router/health` and `/router/seed`. Scored runs never set this.
    #[arg(long, default_value_t = false)]
    local_practice: bool,
    /// Enable the reference archetype-rerouting lever. Off by default; also
    /// enabled by `DITTOBENCH_ROUTER_REROUTE_ASIDES=1`.
    #[arg(long, default_value_t = false)]
    reroute_asides: bool,
    /// Catalog route id to reroute cheap asides to. Must exist in the frozen
    /// task catalog. Falls back to `DITTOBENCH_ROUTER_ASIDE_MODEL`.
    #[arg(long)]
    aside_model: Option<String>,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();

    let relay = Relay::from_env().context("read relay configuration from environment")?;
    if relay.is_none() && !cli.local_practice {
        eprintln!(
            "warning: {RELAY_BASE_URL_ENV}/{RELAY_TICKET_ENV} are unset; \
             provider routes will return 503 until the relay is configured"
        );
    }

    let reroute_enabled = cli.reroute_asides || env_flag(REROUTE_ENV);
    let aside_model = cli
        .aside_model
        .or_else(|| std::env::var(ASIDE_MODEL_ENV).ok());
    let reroute = RerouteConfig::new(reroute_enabled, aside_model);
    if reroute.is_active() {
        eprintln!("archetype rerouting lever is ACTIVE (cheap Anthropic Messages asides -> configured catalog route)");
    }

    let service = RouterService::new(relay, reroute);

    let bind_host = bind_host(cli.local_practice);
    let address = format!("{bind_host}:{}", cli.port);
    let listener = tokio::net::TcpListener::bind(&address)
        .await
        .with_context(|| format!("bind {address}"))?;
    eprintln!("dittobench router miner listening on {address}");
    axum::serve(listener, router(service))
        .await
        .context("serve router harness")
}

/// Scored runs bind all interfaces so the tap can reach the router; local
/// practice binds loopback only.
fn bind_host(local_practice: bool) -> &'static str {
    if local_practice {
        "127.0.0.1"
    } else {
        "0.0.0.0"
    }
}

/// Reads a boolean-ish environment flag (`1`/`true`/`yes`, case-insensitive).
fn env_flag(name: &str) -> bool {
    std::env::var(name).is_ok_and(|value| {
        matches!(
            value.trim().to_ascii_lowercase().as_str(),
            "1" | "true" | "yes"
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn local_practice_binds_loopback_scored_binds_all_interfaces() {
        assert_eq!(bind_host(true), "127.0.0.1");
        assert_eq!(bind_host(false), "0.0.0.0");
    }
}
