#!/bin/sh
# render-config.sh - materialise the Praxis config, substituting ${DS4_API_KEY}.
#
# Praxis does NOT expand environment variables in its config file (verified: the
# only env lookup in core/src/config is the OTLP endpoint fallback). So the ds4
# bearer token cannot be written into the ConfigMap by Praxis itself, and it must
# not be written into the ConfigMap by us either - a ConfigMap is not a Secret.
#
# This script runs as an initContainer, reads the ConfigMap template, substitutes
# the token from the Secret-provided environment variable, and writes the result
# to a memory-backed emptyDir shared with the proxy container.
#
# If the config contains no ${DS4_API_KEY} token (the credential_injection
# variant, which pulls the token straight from the environment and needs no
# templating at all), this is a plain copy and still succeeds.

set -eu

SRC="${CONFIG_TEMPLATE:-/config-template/praxis.yaml}"
DST="${CONFIG_OUT:-/etc/praxis/config.yaml}"
TOKEN='${DS4_API_KEY}'

if [ ! -r "$SRC" ]; then
  echo "render-config: FATAL: template not readable: $SRC" >&2
  exit 1
fi

# -F (fixed string) matters: the bare name DS4_API_KEY also appears as
# `env_var: DS4_API_KEY` in the credential_injection variant, which needs no
# substitution at all. Only the literal ${DS4_API_KEY} token is a placeholder.
if ! grep -qF "$TOKEN" "$SRC"; then
  # No templating needed. Copy through untouched.
  cp "$SRC" "$DST"
  echo "render-config: no placeholder in template; copied verbatim to $DST"
  exit 0
fi

: "${DS4_API_KEY:?render-config: FATAL: DS4_API_KEY is unset but the template requires it}"

# Substitute with awk's ENVIRON so the credential never appears in argv (argv is
# world-readable via /proc inside the pod). Deliberately uses index()/substr()
# rather than gsub(): gsub's replacement string treats "&" and "\" as
# metacharacters, so a key containing either would be silently corrupted. This
# loop is literal and byte-exact for any key value.
awk -v tok="$TOKEN" '
  BEGIN { key = ENVIRON["DS4_API_KEY"]; n = length(tok) }
  {
    line = $0
    out  = ""
    while ((p = index(line, tok)) > 0) {
      out  = out substr(line, 1, p - 1) key
      line = substr(line, p + n)
    }
    print out line
  }
' "$SRC" > "$DST"

# The rendered file now holds a credential: restrict it as far as the (arbitrary,
# OpenShift-assigned) UID allows.
chmod 0600 "$DST" 2>/dev/null || true

if grep -qF "$TOKEN" "$DST"; then
  echo "render-config: FATAL: placeholder survived substitution in $DST" >&2
  exit 1
fi

echo "render-config: rendered $SRC -> $DST ($(wc -l < "$DST") lines, credential substituted)"
