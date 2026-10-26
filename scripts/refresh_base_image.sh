#!/usr/bin/env sh
# Point the Dockerfile's FROM at the current digest of its tag. The digest is
# the multi-arch index, so arm64 builds keep resolving their own image. Needs
# docker with buildx; prints the old and new Python version when it moves.
set -eu

cd "$(dirname "$0")/.."
line="$(grep -m1 '^FROM ' Dockerfile)"
ref="${line#FROM }"
tag="${ref%@*}"
old="${ref#*@}"
new="$(docker buildx imagetools inspect "$tag" --format '{{.Manifest.Digest}}')"
case "$new" in
  sha256:*) ;;
  *) echo "unexpected digest for $tag: $new" >&2; exit 1 ;;
esac
if [ "$new" = "$old" ]; then
  echo "$tag is current ($new)"
  exit 0
fi
sed -i.bak "s|^FROM $tag@$old\$|FROM $tag@$new|" Dockerfile && rm Dockerfile.bak
grep -q "^FROM $tag@$new\$" Dockerfile
echo "$tag: $old -> $new"
echo "python: $(docker run --rm "$tag@$old" python --version) -> $(docker run --rm "$tag@$new" python --version)"
