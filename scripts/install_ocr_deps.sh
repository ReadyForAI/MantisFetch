#!/usr/bin/env sh
set -eu

machine="$(uname -m)"
python_bin="${PYTHON_BIN:-}"
if [ -z "$python_bin" ]; then
  if command -v python3 >/dev/null 2>&1; then
    python_bin="python3"
  elif command -v python >/dev/null 2>&1; then
    python_bin="python"
  else
    echo "python executable not found" >&2
    exit 1
  fi
fi

case "$machine" in
  x86_64|amd64)
    echo "Installing Linux x86_64 OCR dependencies"
    # The image pins this stack with the rest (constraints.txt); a checkout
    # without the file installs from the ranges alone.
    if [ -f constraints.txt ]; then
      "$python_bin" -m pip install --no-cache-dir -r requirements-ocr-linux-x86_64.txt -c constraints.txt
    else
      "$python_bin" -m pip install --no-cache-dir -r requirements-ocr-linux-x86_64.txt
    fi
    ;;
  arm64|aarch64)
    echo "Installing ARM OCR dependencies"
    # Not constrained: constraints.txt is the x86_64 image's set, and this
    # PaddleOCR 2.x stack needs numpy<2 and paddlepaddle 2.6.2, which it contradicts.
    "$python_bin" -m pip install --no-cache-dir -r requirements-ocr-arm64.txt
    ;;
  *)
    echo "Unsupported OCR architecture: $machine" >&2
    exit 1
    ;;
esac
