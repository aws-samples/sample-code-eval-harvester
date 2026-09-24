output "microvm_bucket" {
  description = "S3 bucket name — set as MICROVM_BUCKET"
  value       = aws_s3_bucket.microvm_artifacts.bucket
}

output "microvm_build_role_arn" {
  description = "Build role ARN — set as MICROVM_BUILD_ROLE_ARN"
  value       = aws_iam_role.microvm_build.arn
}

output "microvm_execution_role_arn" {
  description = "Execution role ARN — set as MICROVM_EXECUTION_ROLE_ARN (optional for current eval objectives)"
  value       = aws_iam_role.microvm_execution.arn
}

output "mise_run_eval_exports" {
  description = "Paste-ready export block for running mise run eval. Run: eval $(terraform output -raw mise_run_eval_exports)"
  value       = <<-EOT
    export MICROVM_BUCKET="${aws_s3_bucket.microvm_artifacts.bucket}"
    export MICROVM_BUILD_ROLE_ARN="${aws_iam_role.microvm_build.arn}"
    export MICROVM_EXECUTION_ROLE_ARN="${aws_iam_role.microvm_execution.arn}"
  EOT
}
