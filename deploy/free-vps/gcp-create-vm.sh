#!/usr/bin/env bash
# Create a Google Cloud "Always Free" e2-micro VM for the Hermes gateway.
#
# Run this in Google Cloud Shell (https://shell.cloud.google.com, free, gcloud
# preinstalled) or anywhere `gcloud` is logged in:
#
#   bash gcp-create-vm.sh                    # defaults below
#   PROJECT=my-proj ZONE=us-east1-b bash gcp-create-vm.sh
#
# Free-tier rules this script follows (docs.cloud.google.com/free/docs/free-cloud-features):
#   - machine type e2-micro, one per account, in us-west1 / us-central1 / us-east1 only
#   - 30 GB *standard* persistent disk (the console defaults to "balanced", which is billed)
#   - Standard network tier (first 200 GiB/month egress free; Premium gives only 1 GB)
# Not covered by the free tier: the in-use external IPv4 address
# (about $0.005/h, roughly $3.65/month). The $300 / 90-day trial credit covers it
# while it lasts. No inbound firewall rules are needed: Telegram long polling
# is outbound only.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
ZONE="${ZONE:-us-central1-a}"
NAME="${NAME:-hermes}"
IMAGE_FAMILY="${IMAGE_FAMILY:-debian-12}"
IMAGE_PROJECT="${IMAGE_PROJECT:-debian-cloud}"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

command -v gcloud >/dev/null || die "gcloud not found. Use Cloud Shell: https://shell.cloud.google.com"
[ -n "$PROJECT" ] || die "no project set. Run: gcloud config set project <PROJECT_ID>"

case "$ZONE" in
  us-west1-*|us-central1-*|us-east1-*) ;;
  *) die "zone $ZONE is outside the free-tier regions (us-west1, us-central1, us-east1)" ;;
esac

echo "→ project=$PROJECT zone=$ZONE name=$NAME"

gcloud services enable compute.googleapis.com --project "$PROJECT"

if gcloud compute instances describe "$NAME" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
  echo "✓ VM $NAME already exists, not recreating"
else
  gcloud compute instances create "$NAME" \
    --project "$PROJECT" \
    --zone "$ZONE" \
    --machine-type e2-micro \
    --image-family "$IMAGE_FAMILY" \
    --image-project "$IMAGE_PROJECT" \
    --boot-disk-size 30GB \
    --boot-disk-type pd-standard \
    --network-tier STANDARD \
    --maintenance-policy MIGRATE \
    --labels app=hermes
  echo "✓ VM $NAME created"
fi

cat <<EOF

Next:
  gcloud compute ssh $NAME --zone $ZONE --project $PROJECT
  # then, on the VM:
  curl -fsSL https://raw.githubusercontent.com/rmaviswholesale-create/hermes-agent/main/deploy/free-vps/bootstrap.sh -o bootstrap.sh
  bash bootstrap.sh

Recommended: set a \$1 budget alert so any non-free charge emails you:
  https://console.cloud.google.com/billing/budgets
EOF
