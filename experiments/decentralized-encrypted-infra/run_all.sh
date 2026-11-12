#!/usr/bin/env bash
# Run the SN-473 feasibility experiments and write JSON to results/.
#
# EXPERIMENT ONLY: synthetic inputs on the local machine. Optional flags:
#   --with-tfhe   also run the Concrete ML (TFHE) adapter (Python 3.11, ~1 GB of wheels)
#   --with-chain  also take the read-only SN118 snapshot from the public Finney RPC
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
out="${OUT_DIR:-$here/results}"
bin="$(mktemp -d)"
fixture="$bin/adapter-fixture.json"
trap 'rm -rf "$bin"' EXIT
mkdir -p "$out"

with_tfhe=0
with_chain=0
for arg in "$@"; do
  case "$arg" in
    --with-tfhe) with_tfhe=1 ;;
    --with-chain) with_chain=1 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

uv run --script "$here/python/export_adapter_fixture.py" --out "$fixture"
(cd "$here/go" && go build -o "$bin/ckks" ./cmd/ckks && go build -o "$bin/storage" ./cmd/storage)

workers="${WORKERS:-10}"
for degree in 7 15 31; do
  "$bin/ckks" adapter -fixture "$fixture" -mode plain -degree "$degree" -queries 16 -workers "$workers" \
    > "$out/ckks-adapter-plain-deg$degree.json"
done
"$bin/ckks" adapter -fixture "$fixture" -mode encrypted -degree 15 -queries 16 -workers "$workers" \
  > "$out/ckks-adapter-encrypted-deg15.json"
for mode in plain encrypted; do
  "$bin/ckks" similarity -mode "$mode" -blocks 3 -reps 4 -workers "$workers" \
    > "$out/ckks-similarity-$mode.json"
done
"$bin/storage" > "$out/storage.json"
uv run --script "$here/python/durability.py" > "$out/durability.json"

if (( with_tfhe )); then
  uv run --script --python 3.11 "$here/python/tfhe_adapter_concrete.py" --fixture "$fixture" \
    > "$out/tfhe-adapter-concrete-6bit.json"
fi
if (( with_chain )); then
  uv run --script "$here/python/sn118_chain_snapshot.py" > "$out/sn118-chain-snapshot.json"
fi
echo "results in $out"
