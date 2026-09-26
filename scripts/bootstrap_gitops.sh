#!/usr/bin/env bash
set -euo pipefail
# Run only to bootstrap/rotate this repository's read-only Argo deploy key.
repo=devsecopslonghn/statuspage-platform
keydir=$(mktemp -d)
trap 'rm -rf "$keydir"' EXIT
ssh-keygen -q -t ed25519 -N '' -C statuspage-argocd -f "$keydir/id_ed25519"
gh repo deploy-key add "$keydir/id_ed25519.pub" --repo "$repo" --title "argocd-statuspage-$(date -u +%Y%m%dT%H%M%SZ)"
kubectl -n argocd create secret generic repo-statuspage-platform \
  --from-literal=type=git \
  --from-literal=url=git@github.com:devsecopslonghn/statuspage-platform.git \
  --from-file=sshPrivateKey="$keydir/id_ed25519" \
  --dry-run=client -o json | \
  kubectl apply --server-side --field-manager=statuspage-repo-bootstrap -f -
kubectl -n argocd label secret repo-statuspage-platform argocd.argoproj.io/secret-type=repository --overwrite
