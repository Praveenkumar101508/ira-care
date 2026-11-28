#!/bin/sh
# Off-site copies of the nightly encrypted backups, in any S3-compatible bucket.
#
#   offsite.sh push FILE        upload FILE (an ira-care-*.sql.gz.enc written by backup.sh) and FILE.sha256
#   offsite.sh fetch NAME DIR   download NAME and NAME.sha256 into DIR, and check the checksum
#   offsite.sh latest           print the newest backup's name in the bucket
#
# Only files backup.sh has already encrypted (AES-256, the passphrase never leaves the
# machine) are ever uploaded: anything else is refused, so a plain dump cannot leave by
# mistake. The bucket holds ciphertext; whoever runs it cannot read the data. Keeping
# old copies is the bucket's lifecycle rule (docs/DEPLOY.md), not this script's.
#
# Settings (environment):
#   OFFSITE_S3_ENDPOINT   https://<host>[:port]   (http only with OFFSITE_ALLOW_HTTP=1, for a local drill)
#   OFFSITE_S3_BUCKET     the bucket name
#   OFFSITE_S3_REGION     the region for request signing (default us-east-1)
#   OFFSITE_S3_ACCESS_KEY, OFFSITE_S3_SECRET_KEY
# Requests are signed with AWS Signature V4 by curl (--aws-sigv4); the keys are handed to
# curl on standard input, never on the command line where `ps` would show them.
set -eu
# Fetched copies are private to the service account, like the ones backup.sh writes.
umask 077
command -v curl >/dev/null 2>&1 || { echo "offsite: curl not found" >&2; exit 1; }
: "${OFFSITE_S3_ENDPOINT:?set OFFSITE_S3_ENDPOINT}"
: "${OFFSITE_S3_BUCKET:?set OFFSITE_S3_BUCKET}"
: "${OFFSITE_S3_ACCESS_KEY:?set OFFSITE_S3_ACCESS_KEY}"
: "${OFFSITE_S3_SECRET_KEY:?set OFFSITE_S3_SECRET_KEY}"
REGION="${OFFSITE_S3_REGION:-us-east-1}"
case "$OFFSITE_S3_ENDPOINT" in
  https://*) ;;
  http://*) [ "${OFFSITE_ALLOW_HTTP:-0}" = "1" ] || { echo "offsite: refusing a plain-http endpoint" >&2; exit 2; } ;;
  *) echo "offsite: the endpoint must be an https:// address" >&2; exit 2 ;;
esac
BASE="${OFFSITE_S3_ENDPOINT%/}/${OFFSITE_S3_BUCKET}"

s3() {  # s3 <curl arguments...>: one signed request, keys from stdin
  printf 'user = "%s:%s"\n' "$OFFSITE_S3_ACCESS_KEY" "$OFFSITE_S3_SECRET_KEY" \
    | curl -fsS --retry 3 --retry-delay 2 --aws-sigv4 "aws:amz:${REGION}:s3" -K - "$@"
}

is_backup_name() {
  case "$1" in ira-care-*.sql.gz.enc) return 0 ;; *) return 1 ;; esac
}

cmd="${1:-}"
case "$cmd" in
  push)
    file="${2:?usage: offsite.sh push FILE}"
    name="$(basename "$file")"
    is_backup_name "$name" || { echo "offsite: refusing $name: only encrypted backups (ira-care-*.sql.gz.enc) leave this machine" >&2; exit 3; }
    [ -f "$file" ] && [ -f "$file.sha256" ] || { echo "offsite: $file or its .sha256 is missing" >&2; exit 3; }
    s3 -T "$file" "$BASE/$name" >/dev/null
    s3 -T "$file.sha256" "$BASE/$name.sha256" >/dev/null
    echo "off-site copy written: $name"
    ;;
  fetch)
    name="${2:?usage: offsite.sh fetch NAME DIR}"; dir="${3:?usage: offsite.sh fetch NAME DIR}"
    is_backup_name "$name" || { echo "offsite: $name is not a backup name" >&2; exit 3; }
    mkdir -p "$dir"
    chmod 700 "$dir"
    s3 -o "$dir/$name" "$BASE/$name"
    s3 -o "$dir/$name.sha256" "$BASE/$name.sha256"
    # The checksum file names the path it was written with; check the content, not the path.
    want="$(cut -d' ' -f1 "$dir/$name.sha256")"
    got="$(sha256sum "$dir/$name" | cut -d' ' -f1)"
    [ "$want" = "$got" ] || { echo "offsite: checksum mismatch for $name: not restoring it" >&2; rm -f "$dir/$name"; exit 4; }
    echo "fetched and verified: $dir/$name"
    ;;
  latest)
    s3 "$BASE/?list-type=2&prefix=ira-care-" \
      | tr '<' '\n' | sed -n 's/^Key>\(ira-care-[^<]*\.sql\.gz\.enc\)$/\1/p' | sort | tail -1
    ;;
  *)
    echo "usage: offsite.sh push FILE | fetch NAME DIR | latest" >&2; exit 64 ;;
esac
