use std::path::PathBuf;
use std::sync::Arc;

use anyhow::{Context, Result};
use clap::{Parser, ValueEnum};
use dittobench_coding_starter_kit::server::{
    DirectOpenRouterModelFactory, ScriptedModelFactory, TicketBrokerModelFactory,
};
use dittobench_coding_starter_kit::{router, CodingService, ModelFactory};

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum ModelMode {
    Broker,
    Scripted,
    Openrouter,
}

#[derive(Debug, Parser)]
#[command(
    name = "dittobench-coding-miner",
    version,
    about = "Shadow-only DittoBench coding reference harness"
)]
struct Cli {
    #[arg(long, default_value_t = 8080)]
    port: u16,
    #[arg(long, value_enum, default_value_t = ModelMode::Broker)]
    model_mode: ModelMode,
    #[arg(long)]
    script: Option<PathBuf>,
    #[arg(long, default_value_t = false)]
    allow_practice_model: bool,
    #[arg(long, default_value_t = false)]
    allow_direct_openrouter: bool,
}

#[tokio::main]
async fn main() -> Result<()> {
    let cli = Cli::parse();
    let bind_host = bind_host(cli.model_mode);
    let models: Arc<dyn ModelFactory> = match cli.model_mode {
        ModelMode::Broker => {
            anyhow::ensure!(
                !cli.allow_direct_openrouter && !cli.allow_practice_model && cli.script.is_none(),
                "practice flags are invalid in broker mode"
            );
            Arc::new(TicketBrokerModelFactory)
        }
        ModelMode::Scripted => {
            anyhow::ensure!(
                cli.allow_practice_model,
                "scripted mode requires --allow-practice-model"
            );
            let path = cli.script.context("scripted mode requires --script")?;
            let bytes = std::fs::read(&path)
                .with_context(|| format!("read scripted model fixture {}", path.display()))?;
            let chunks = serde_json::from_slice(&bytes).context("decode scripted model fixture")?;
            Arc::new(ScriptedModelFactory::new(chunks, true)?)
        }
        ModelMode::Openrouter => {
            anyhow::ensure!(
                cli.allow_direct_openrouter,
                "direct OpenRouter requires --allow-direct-openrouter"
            );
            let key = std::env::var("OPENROUTER_API_KEY")
                .context("OPENROUTER_API_KEY is required for direct local practice")?;
            Arc::new(DirectOpenRouterModelFactory::new(key, true)?)
        }
    };

    let address = format!("{bind_host}:{}", cli.port);
    let listener = tokio::net::TcpListener::bind(&address)
        .await
        .with_context(|| format!("bind {address}"))?;
    eprintln!("dittobench coding miner listening on {address}");
    axum::serve(listener, router(CodingService::new(models)))
        .await
        .context("serve coding harness")
}

fn bind_host(mode: ModelMode) -> &'static str {
    match mode {
        ModelMode::Broker => "0.0.0.0",
        ModelMode::Scripted | ModelMode::Openrouter => "127.0.0.1",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credentialed_and_scripted_practice_modes_bind_loopback_only() {
        assert_eq!(bind_host(ModelMode::Scripted), "127.0.0.1");
        assert_eq!(bind_host(ModelMode::Openrouter), "127.0.0.1");
        assert_eq!(bind_host(ModelMode::Broker), "0.0.0.0");
    }
}
