#!/usr/bin/env bash
set -euo pipefail

INPUT_FILE="${1:-}"

if [ -z "$INPUT_FILE" ]; then
  echo '{"success":false,"message":"missing input file","screenshotPath":"","resultPath":""}'
  exit 1
fi

cd "$(dirname "$0")"

if [ ! -f "$INPUT_FILE" ]; then
  echo "{\"success\":false,\"message\":\"input file not found: $INPUT_FILE\",\"screenshotPath\":\"\",\"resultPath\":\"\"}"
  exit 1
fi

node dist/index.js "$INPUT_FILE"
