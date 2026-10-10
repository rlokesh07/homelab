#!/bin/sh
# Copies pi/ to the Raspberry Pi and runs install.sh there.
# Usage: ./pi/deploy.sh [user@host]
set -eu

HOST=${1:-alarcafil@10.0.0.214}
SSH="ssh -i $HOME/.ssh/homelab"

cd "$(dirname "$0")"
rsync -a --delete -e "$SSH" ./ "$HOST:homelab/"
# shellcheck disable=SC2086
$SSH "$HOST" 'sudo sh ~/homelab/install.sh'
