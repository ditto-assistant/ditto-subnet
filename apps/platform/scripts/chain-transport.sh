# shellcheck shell=bash
# Sourced by start.sh and update.sh after .env and .env.deploy are loaded.
#
# Pylon must not use the public Finney entrypoint, which refuses websocket
# handshakes with HTTP 429 under load. When the paid provider is configured,
# export PYLON_SUBTENSOR_URL with its credential attached exactly as
# ditto/chain/client.py ChainClient._configured_archive_url builds it;
# docker-compose.yml prefers it over SUBTENSOR_NETWORK. SUBTENSOR_NETWORK
# remains the network identity everywhere else.
pylon_subtensor_url() {
  local url="${SUBTENSOR_ARCHIVE_RPC_URL:-}"
  local key="${SUBTENSOR_ARCHIVE_RPC_API_KEY:-}"
  local mode="${SUBTENSOR_ARCHIVE_RPC_AUTH_MODE:-query}"
  [ -n "$url" ] || return 0
  case "$mode" in
    none | query | path) ;;
    *)
      # ChainConfig refuses any other mode; never put a key somewhere it does not belong.
      echo "chain-transport: unsupported SUBTENSOR_ARCHIVE_RPC_AUTH_MODE=$mode; Pylon keeps SUBTENSOR_NETWORK" >&2
      return 0
      ;;
  esac
  if [ -z "$key" ] || [ "$mode" = none ]; then
    printf '%s' "$url"
  elif [ "$mode" = path ]; then
    printf '%s/%s' "${url%/}" "$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""),end="")' "$key")"
  else
    case "$url" in *\?*) sep='&' ;; *) sep='?' ;; esac
    printf '%s%sauthorization=%s' "$url" "$sep" "$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1],safe=""),end="")' "$key")"
  fi
}

PYLON_SUBTENSOR_URL="$(pylon_subtensor_url)"
export PYLON_SUBTENSOR_URL
