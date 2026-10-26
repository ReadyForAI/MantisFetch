#!/usr/bin/env sh
# Regenerate constraints.txt: the exact version of every package the image
# installs (CPython 3.11, Linux x86_64), for requirements.txt and the x86_64 OCR
# stack together, so a shared package cannot be pinned two ways; plus the CI
# tools in requirements-dev.txt, which the image does not install.
#
#   scripts/refresh_constraints.sh            keep existing pins; move only what a
#                                             changed requirement forces
#   scripts/refresh_constraints.sh --upgrade  move everything to the newest
#                                             versions the requirements allow
#
# Run it in the same PR as any change to a requirements file. The uv version is
# pinned because the output format is uv's: another version may reformat the file.
set -eu

UV_VERSION=0.11.1

cd "$(dirname "$0")/.."
have="$(uv --version 2>/dev/null | cut -d' ' -f2 || true)"
if [ "$have" != "$UV_VERSION" ]; then
  echo "needs uv $UV_VERSION (found: ${have:-none}); pip install uv==$UV_VERSION" >&2
  exit 1
fi

uv pip compile requirements.txt requirements-ocr-linux-x86_64.txt requirements-dev.txt \
  --python-version 3.11 \
  --python-platform x86_64-manylinux_2_28 \
  --no-header \
  --output-file constraints.txt \
  "$@"
