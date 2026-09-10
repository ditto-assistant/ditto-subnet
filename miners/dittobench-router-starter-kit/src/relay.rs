//! The router's single upstream: the validator's ticket-scoped relay.
//!
//! The scored router never talks to a provider directly. Its only egress is
//! the validator relay, whose base URL and single-use bearer ticket arrive as
//! environment variables at run time (`DITTOBENCH_RELAY_BASE_URL`,
//! `DITTOBENCH_RELAY_TICKET`). The relay holds the real provider credentials,
//! enforces the frozen catalog and the arm's budget, and logs every upstream
//! body. No provider secret is ever read, printed, or baked into this image.

use std::fmt;
use std::time::Duration;

use reqwest::redirect::Policy;
use reqwest::Url;

/// Environment variable holding the relay base URL (e.g. the ticket-scoped
/// origin the validator stands up for one arm).
pub const RELAY_BASE_URL_ENV: &str = "DITTOBENCH_RELAY_BASE_URL";

/// Environment variable holding the single-use relay bearer ticket.
pub const RELAY_TICKET_ENV: &str = "DITTOBENCH_RELAY_TICKET";

#[derive(Debug, thiserror::Error)]
pub enum RelayError {
    #[error("invalid relay base url: {0}")]
    InvalidBaseUrl(String),
    #[error("relay ticket is empty")]
    EmptyTicket,
    #[error("build relay http client: {0}")]
    Client(String),
}

/// A configured relay upstream. Cloneable and cheap to share across handlers
/// (`reqwest::Client` is an `Arc` internally).
#[derive(Clone)]
pub struct Relay {
    base: Url,
    ticket: String,
    client: reqwest::Client,
}

impl Relay {
    /// Reads the relay configuration from the environment.
    ///
    /// Returns `Ok(None)` when neither variable is set, so the binary can run
    /// in local health-only practice (serving `/router/health` and
    /// `/router/seed`) without a relay. Returns an error when the values are
    /// present but malformed.
    ///
    /// # Errors
    ///
    /// Returns [`RelayError`] when the base URL is not a valid absolute
    /// http(s) URL, the ticket is empty, or the HTTP client cannot be built.
    pub fn from_env() -> Result<Option<Self>, RelayError> {
        let base = std::env::var(RELAY_BASE_URL_ENV)
            .ok()
            .filter(|value| !value.trim().is_empty());
        let ticket = std::env::var(RELAY_TICKET_ENV)
            .ok()
            .filter(|value| !value.trim().is_empty());
        match (base, ticket) {
            (Some(base), Some(ticket)) => Self::new(&base, ticket).map(Some),
            (None, None) => Ok(None),
            (Some(_), None) => Err(RelayError::EmptyTicket),
            (None, Some(_)) => Err(RelayError::InvalidBaseUrl(
                "base url is not set".to_string(),
            )),
        }
    }

    /// Builds a relay upstream from an explicit base URL and ticket.
    ///
    /// # Errors
    ///
    /// Returns [`RelayError`] when the base URL is not absolute http(s), the
    /// ticket is empty, or the HTTP client cannot be constructed.
    pub fn new(base: &str, ticket: String) -> Result<Self, RelayError> {
        let base =
            Url::parse(base).map_err(|error| RelayError::InvalidBaseUrl(error.to_string()))?;
        if !matches!(base.scheme(), "http" | "https") {
            return Err(RelayError::InvalidBaseUrl(format!(
                "scheme must be http or https, got {}",
                base.scheme()
            )));
        }
        if base.cannot_be_a_base() {
            return Err(RelayError::InvalidBaseUrl(
                "base url must be absolute".to_string(),
            ));
        }
        if ticket.trim().is_empty() {
            return Err(RelayError::EmptyTicket);
        }
        // The relay may stream a full model turn; allow a long ceiling. (600s
        // reads clearly as ten minutes; keep the explicit seconds unit.)
        #[allow(clippy::duration_suboptimal_units)]
        let client = reqwest::Client::builder()
            // The relay is the only permitted destination; never chase a
            // redirect to some other origin.
            .redirect(Policy::none())
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(600))
            .build()
            .map_err(|error| RelayError::Client(error.to_string()))?;
        Ok(Self {
            base,
            ticket,
            client,
        })
    }

    /// The configured HTTP client.
    #[must_use]
    pub fn client(&self) -> &reqwest::Client {
        &self.client
    }

    /// The single-use bearer ticket. Kept crate-visible; never logged.
    #[must_use]
    pub(crate) fn ticket(&self) -> &str {
        &self.ticket
    }

    /// Joins a provider route path (e.g. `/v1/messages`) onto the relay base,
    /// preserving any base path prefix.
    ///
    /// # Errors
    ///
    /// Returns [`RelayError`] when the resulting URL is invalid.
    pub fn url_for(&self, route: &str) -> Result<Url, RelayError> {
        let base_path = self.base.path().trim_end_matches('/');
        let joined = format!("{base_path}{route}");
        let mut url = self.base.clone();
        url.set_path(&joined);
        url.set_query(None);
        url.set_fragment(None);
        Ok(url)
    }
}

impl fmt::Debug for Relay {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        // Never render the ticket; the elided client field is intentional.
        f.debug_struct("Relay")
            .field("base", &self.base.as_str())
            .field("ticket", &"<redacted>")
            .finish_non_exhaustive()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_non_http_base() {
        assert!(Relay::new("file:///etc/passwd", "ticket".to_string()).is_err());
        assert!(Relay::new("not a url", "ticket".to_string()).is_err());
    }

    #[test]
    fn rejects_empty_ticket() {
        assert!(matches!(
            Relay::new("https://relay.internal", "   ".to_string()),
            Err(RelayError::EmptyTicket)
        ));
    }

    #[test]
    fn joins_routes_onto_base_path_and_drops_query() {
        let relay = Relay::new("https://relay.internal/arm/abc?x=1", "ticket".to_string()).unwrap();
        assert_eq!(
            relay.url_for("/v1/messages").unwrap().as_str(),
            "https://relay.internal/arm/abc/v1/messages"
        );
        assert_eq!(
            relay.url_for("/v1/messages/count_tokens").unwrap().as_str(),
            "https://relay.internal/arm/abc/v1/messages/count_tokens"
        );
    }

    #[test]
    fn debug_never_reveals_ticket() {
        let relay = Relay::new("https://relay.internal", "super-secret".to_string()).unwrap();
        let rendered = format!("{relay:?}");
        assert!(!rendered.contains("super-secret"));
        assert!(rendered.contains("<redacted>"));
    }
}
