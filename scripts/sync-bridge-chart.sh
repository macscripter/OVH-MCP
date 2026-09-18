#!/usr/bin/env bash
# Refresh the vendored copy of the Bridge Helm chart.
#
# The chart is a sub-chart of the Governance Authority and lives in the Bridge repository.
# This server vendors a copy so the container image can deploy it with no checkout, which
# means the copy goes stale the moment the chart changes. Run this after every chart change,
# and check the diff before committing it.
set -euo pipefail

SOURCE="${1:-${BRIDGE_REPO:-$HOME/SIMPL-OPEN/bridge}}/charts/bridge"
TARGET="$(cd "$(dirname "$0")/.." && pwd)/vendor/charts/bridge"

[ -f "$SOURCE/Chart.yaml" ] || { echo "no chart at $SOURCE" >&2; exit 1; }
rm -rf "$TARGET"
mkdir -p "$(dirname "$TARGET")"
cp -r "$SOURCE" "$TARGET"
echo "vendored $(grep '^version:' "$TARGET/Chart.yaml") from $SOURCE"
