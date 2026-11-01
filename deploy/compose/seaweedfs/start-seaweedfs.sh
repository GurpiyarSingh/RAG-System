#!/bin/sh
# Start SeaweedFS with its S3 gateway, using the S3 credentials from .env.
# The identity file is generated at start-up so the secret never lives in the repo.
set -eu

: "${RAG_S3_ACCESS_KEY:?RAG_S3_ACCESS_KEY must be set in .env}"
: "${RAG_S3_SECRET_KEY:?RAG_S3_SECRET_KEY must be set in .env}"

cat > /tmp/s3.json <<EOF
{
  "identities": [
    {
      "name": "rag-service",
      "credentials": [{ "accessKey": "${RAG_S3_ACCESS_KEY}", "secretKey": "${RAG_S3_SECRET_KEY}" }],
      "actions": ["Admin", "Read", "Write", "List", "Tagging"]
    }
  ]
}
EOF

# master + volume + filer + S3 gateway in one process, which is the right size for a
# single machine. volumeSizeLimitMB keeps the pre-allocated volume files small.
exec weed server \
  -dir=/data \
  -ip.bind=0.0.0.0 \
  -master.volumeSizeLimitMB=512 \
  -s3 \
  -s3.port=8333 \
  -s3.config=/tmp/s3.json
