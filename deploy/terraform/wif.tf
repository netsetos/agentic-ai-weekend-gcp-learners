# Keyless CI. UNOWNED: lesson 12.7 teaches this; it is stood up early because the
# 16 Sept masterclass demo (D4.1) needs a pipeline that exists.
#
# The point is the ABSENCE of a key. A service-account JSON in a GitHub secret is a
# credential with no expiry that every workflow in the repo can read; WIF exchanges a
# short-lived GitHub OIDC token for one, scoped to one repository.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "documind-github"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"          = "assertion.sub"
    "attribute.repository"    = "assertion.repository"
    # The IMMUTABLE one. Repository and org NAMES are re-registrable after a
    # rename, transfer or deletion; the numeric id is not.
    "attribute.repository_id" = "assertion.repository_id"
    "attribute.ref"           = "assertion.ref"
  }

  # Three gates, and the first two are the ones tutorials stop before.
  #
  #   repository_id  pins the repo that cannot be impersonated by re-registering
  #                  a name somebody released. Pinning the NAME alone means
  #                  whoever claims "netsetos/agentic-ai-weekend-gcp" after a rename gets
  #                  a GitHub-signed token that satisfies the condition.
  #   ref            pins the BRANCH. Without it, any workflow on any branch or
  #                  tag in the repo gets full deploy credentials - so anyone who
  #                  can push a branch can add a workflow file and deploy.
  #   repository     kept for readability in logs and in the principalSet.
  attribute_condition = join(" && ", [
    "assertion.repository_id == '${var.github_repository_id}'",
    "assertion.repository == '${var.github_repository}'",
    "assertion.ref == '${var.deploy_ref}'",
  ])

  oidc { issuer_uri = "https://token.actions.githubusercontent.com" }
}

# Only this repository may impersonate the deploy identity - by immutable id, for
# the same reason the condition uses it. A principalSet keyed on the NAME is a
# second place the reclamation trick would work.
resource "google_service_account_iam_member" "cicd_wif" {
  service_account_id = google_service_account.cicd.name
  role               = "roles/iam.workloadIdentityUser"
  member = format(
    "principalSet://iam.googleapis.com/%s/attribute.repository_id/%s",
    google_iam_workload_identity_pool.github.name,
    var.github_repository_id,
  )
}

output "wif_provider" {
  description = "workload_identity_provider for the GitHub action"
  value       = google_iam_workload_identity_pool_provider.github.name
}
output "cicd_service_account" { value = google_service_account.cicd.email }

# Which ref may deploy. main by default; the demo weeks may point it at the lane's branch (Module 12, decision D5) and
# back again - one variable, in the plan output, never a second provider.
variable "deploy_ref" {
  type    = string
  default = "refs/heads/main"
}

# The candidate release job (documind-cd.yml, release-candidate) runs the live gate as the roster member and as the outsider -
# make eval-live impersonates both - so the deploy identity may mint their tokens, and nothing else's: two accounts,
# enumerated, for the same reason cicd_actas is.
resource "google_service_account_iam_member" "cicd_eval_tokens" {
  for_each           = { ui = google_service_account.ui.name, outsider = google_service_account.outsider.name }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.cicd.email}"
}
