#!/usr/bin/env bash
set -euo pipefail

identity_dir='/var/lib/ditto-validator/coding-executor-mtls'
ca="$identity_dir/executor-ca.pem"
certificate="$identity_dir/validator-client.pem"
key="$identity_dir/validator-client-key.pem"
hotkey="${1:-}"

if [[ "$EUID" -ne 0 || ! "$hotkey" =~ ^[1-9A-HJ-NP-Za-km-z]{47,48}$ ]]; then
  echo 'validator mTLS identity verifier authority is invalid' >&2
  exit 1
fi
for path in "$ca" "$certificate" "$key"; do
  [[ -f "$path" && ! -L "$path" ]] || {
    echo 'validator mTLS identity file is invalid' >&2
    exit 1
  }
  [[ "$(stat -c '%u:%g:%a' "$path")" == '0:0:400' ]] || {
    echo 'validator mTLS identity ownership or mode is invalid' >&2
    exit 1
  }
done

openssl verify -purpose sslclient -CAfile "$ca" "$certificate" >/dev/null
certificate_public_key="$(openssl x509 -in "$certificate" -pubkey -noout | sha256sum | awk '{print $1}')"
private_public_key="$(openssl pkey -in "$key" -pubout | sha256sum | awk '{print $1}')"
[[ "$certificate_public_key" =~ ^[0-9a-f]{64}$ && "$certificate_public_key" == "$private_public_key" ]] || {
  echo 'validator mTLS certificate and key do not match' >&2
  exit 1
}
openssl x509 -in "$certificate" -noout -checkend 0 >/dev/null
san="$(openssl x509 -in "$certificate" -noout -ext subjectAltName | tail -n +2 | tr -d '[:space:]')"
[[ "$san" == "URI:spiffe://dittobench.ai/validator/$hotkey" ]] || {
  echo 'validator mTLS certificate identity is invalid' >&2
  exit 1
}
