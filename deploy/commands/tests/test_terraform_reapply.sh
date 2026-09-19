#!/usr/bin/env bash
# Real Terraform evaluation with mocked Google providers: no credentials or GCP changes.
set -euo pipefail
command -v terraform >/dev/null || { echo 'Install Terraform >= 1.9 first.' >&2; exit 1; }
test_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd "$test_dir/../.." && pwd)"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT
cp "$deploy_dir/terraform/budget.tf" "$deploy_dir/terraform/managed.tf" "$work_dir/"
mkdir "$work_dir/tests"
cp "$test_dir/terraform_reapply.tftest.hcl" "$work_dir/tests/"
cat > "$work_dir/main.tf" <<'HCL'
terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google      = { source = "hashicorp/google", version = "~> 6.15" }
    google-beta = { source = "hashicorp/google-beta", version = "~> 6.15" }
  }
}
variable "project_id" { default = "documind-test" }
provider "google" { project = var.project_id }
provider "google-beta" { project = var.project_id }
data "google_project" "current" { project_id = var.project_id }
HCL
terraform -chdir="$work_dir" init -backend=false -input=false -no-color
terraform -chdir="$work_dir" validate -no-color
terraform -chdir="$work_dir" test -no-color
