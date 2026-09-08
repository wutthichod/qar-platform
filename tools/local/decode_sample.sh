#!/usr/bin/env bash
# Decode a sample flight with no AWS account and no credentials.
#
# Runs the same entrypoint the Fargate task runs, with LOCAL_SOURCE /
# LOCAL_FAP / LOCAL_OUT standing in for the three S3 paths. If this fails,
# the container will fail the same way, and the report says at which stage.
#
#   tools/local/decode_sample.sh a350
#   tools/local/decode_sample.sh b777
#   tools/local/decode_sample.sh b787          # container inspection only
#   QAR_DATA=/path/to/qar-data tools/local/decode_sample.sh b777
#
# Set DOCKER=1 to run the built image instead of the local interpreter.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
QAR_DATA="${QAR_DATA:-$(cd "$ROOT/.." && pwd)/qar-data}"
OUT="${OUT:-$ROOT/.local-out}"
TYPE="${1:-b777}"

if [[ ! -d "$QAR_DATA" ]]; then
  echo "sample set not found at $QAR_DATA; set QAR_DATA=/path/to/qar-data" >&2
  exit 2
fi

case "$TYPE" in
  a350)
    SOURCE="$(find "$QAR_DATA/QAR Samples/A350" -name '*.pmf' | sort | head -1)"
    FAP="$QAR_DATA/Fap/CS350THA22"
    PARAMS="ALT_STD,VRTG,N1_1,N1_2,HEADING,GS,RALT1,PITCH_ANG_CAP,LATG,GW,UTC_HOUR,UTC_MIN,UTC_SEC,YEAR,MONTH,DAY"
    ;;
  b777)
    SOURCE="$(find "$QAR_DATA/QAR Samples/B777" -name 'raw.dat' | sort | head -1)"
    FAP="$QAR_DATA/Fap/CS77724"
    PARAMS="aALTSTD1,aCAS1,aRALTC,aAIRGND1,VRTG,aGS2,aTAS1"
    ;;
  b787)
    SOURCE="$(find "$QAR_DATA/QAR Samples/B787" -name '*.zip' | sort | head -1)"
    echo "==> B787 EDS crate: container inspection only"
    PYTHONPATH="$ROOT/packages/qar-decode/src" python3 - "$SOURCE" <<'PY'
import sys
from pathlib import Path
from qar_decode.container import crate
stream, meta = crate.inspect(Path(sys.argv[1]).read_bytes())
print(f"  {meta.get('tail_number')}  {meta.get('flight_number')}  "
      f"{meta.get('origin')} -> {meta.get('destination')}")
print(f"  crate digest verified: {meta.get('digest_verified')}")
print(f"  CPL map: {meta.get('cpl_map')}")
print(f"  {stream.seconds} s of recording, {stream.resyncs} resync(s)")
for rtype, n in sorted(stream.census.items()):
    print(f"    type {rtype}: {n:>7} records")
print("\n  CPL payloads are not ARINC 717; parameter extraction needs the")
print("  Boeing CPL map named above. See container/crate.py.")
PY
    exit 0
    ;;
  *)
    echo "usage: $0 {a350|b777|b787}" >&2
    exit 2
    ;;
esac

if [[ -z "${SOURCE:-}" || ! -f "$SOURCE" ]]; then
  echo "no $TYPE sample under $QAR_DATA" >&2
  exit 2
fi

echo "==> source $SOURCE"
echo "==> fap    $FAP"
echo "==> out    $OUT"
mkdir -p "$OUT"

if [[ "${DOCKER:-0}" == "1" ]]; then
  exec docker run --rm \
    -v "$(dirname "$SOURCE"):/data/source:ro" \
    -v "$FAP:/data/fap:ro" \
    -v "$OUT:/out" \
    -e "LOCAL_SOURCE=/data/source/$(basename "$SOURCE")" \
    -e LOCAL_FAP=/data/fap \
    -e LOCAL_OUT=/out \
    -e "PARAMETERS=$PARAMS" \
    -e "RUN_ID=local-$TYPE" \
    qar-decoder:dev
fi

PYTHONPATH="$ROOT/packages/qar-decode/src" \
LOCAL_SOURCE="$SOURCE" \
LOCAL_FAP="$FAP" \
LOCAL_OUT="$OUT" \
PARAMETERS="$PARAMS" \
RUN_ID="local-$TYPE" \
exec python3 "$ROOT/services/decoder-job/src/main.py"
