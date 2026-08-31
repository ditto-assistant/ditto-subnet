#!/usr/bin/env bash
set -euo pipefail

identity_dir='/var/lib/ditto-coding-executor/mtls'
ca="$identity_dir/validator-ca.pem"
certificate="$identity_dir/scorer-server.pem"
key="$identity_dir/scorer-server-key.pem"

if [[ "$EUID" -ne 0 ]]; then
  echo 'scorer mTLS identity verifier must run as root' >&2
  exit 1
fi
for path in "$ca" "$certificate" "$key"; do
  [[ -f "$path" && ! -L "$path" ]] || {
    echo 'scorer mTLS identity file is invalid' >&2
    exit 1
  }
  [[ "$(stat -c '%u:%g:%a' "$path")" == '0:0:400' ]] || {
    echo 'scorer mTLS identity ownership or mode is invalid' >&2
    exit 1
  }
done
openssl verify -purpose sslserver -CAfile "$ca" "$certificate" >/dev/null
certificate_public_key="$(openssl x509 -in "$certificate" -pubkey -noout | sha256sum | awk '{print $1}')"
private_public_key="$(openssl pkey -in "$key" -pubout | sha256sum | awk '{print $1}')"
[[ "$certificate_public_key" =~ ^[0-9a-f]{64}$ && "$certificate_public_key" == "$private_public_key" ]] || {
  echo 'scorer mTLS certificate and key do not match' >&2
  exit 1
}
openssl x509 -in "$certificate" -noout -checkend 0 >/dev/null
