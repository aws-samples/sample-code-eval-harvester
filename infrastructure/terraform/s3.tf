# A random suffix keeps the bucket name globally unique (S3 bucket names are account-agnostic, so a
# fixed name collides the moment two accounts or two regions apply this). byte_length = 4 yields an
# 8-char hex suffix; the name segment is shortened to "mv" so ${project}-mv-<8 hex> stays well under
# the 63-char S3 limit even for a long project name.
resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# Versioning is off by design, not by omission: a versioned object is not removed by a lifecycle
# expiry rule, so enabling it would make every trial's build artifacts accumulate rather than expire
# after 7 days. The aws_s3_bucket_versioning resource below states the decision outright, and the
# CKV_AWS_21 skip inside this block records checkov's twin of this same rule.
# nosemgrep: aws-s3-bucket-versioning-not-enabled
resource "aws_s3_bucket" "microvm_artifacts" {
  bucket        = "${var.project}-mv-${random_id.bucket_suffix.hex}"
  force_destroy = true

  # checkov:skip=CKV_AWS_18:Access logging not warranted for transient build artifacts with 7-day expiry
  # checkov:skip=CKV_AWS_144:Cross-region replication not needed — these are ephemeral per-trial artifacts, not durable data
  # checkov:skip=CKV_AWS_21:Versioning is intentionally disabled — versioned objects bypass lifecycle expiry and would accumulate
  # checkov:skip=CKV2_AWS_62:Event notifications add operational overhead with no benefit for a transient artifact staging bucket
  # checkov:skip=CKV_AWS_145:AES256 SSE is sufficient for short-lived build artifacts; KMS adds key-management overhead without meaningful security gain here

  tags = {
    Project = var.project
    Purpose = "Lambda MicroVMs build artifacts for the eval harness"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "microvm_artifacts" {
  bucket = aws_s3_bucket.microvm_artifacts.id

  # checkov:skip=CKV2_AWS_67:There is no customer managed key here to rotate — the rule's premise does not hold. Encryption is SSE-S3 (AES256, below), chosen for the reason recorded in the CKV_AWS_145 skip: these are short-lived build artifacts, and a CMK adds key management without a matching gain.

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "microvm_artifacts" {
  bucket                  = aws_s3_bucket.microvm_artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "microvm_artifacts" {
  bucket = aws_s3_bucket.microvm_artifacts.id
  versioning_configuration {
    # Intentionally disabled: versioned objects bypass S3 lifecycle expiry rules,
    # so enabling versioning would cause trial artifacts to accumulate rather than
    # expire after 7 days. The bucket holds transient build inputs, not durable data.
    status = "Disabled"
  }
}

# Artifacts are transient (one per trial, deleted after the image is built).
# 7 days is a safety net for failed cleanup; adjust down if storage cost matters.
resource "aws_s3_bucket_lifecycle_configuration" "microvm_artifacts" {
  bucket = aws_s3_bucket.microvm_artifacts.id

  rule {
    id     = "expire-artifacts"
    status = "Enabled"

    filter {
      prefix = "harbor/lambda-microvms/"
    }

    expiration {
      days = 7
    }
  }

  # CKV_AWS_300: global rule to abort incomplete multipart uploads after 1 day,
  # preventing orphaned upload parts from accumulating storage costs.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}
