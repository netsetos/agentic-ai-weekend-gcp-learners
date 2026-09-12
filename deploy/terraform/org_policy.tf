# The policy that makes "keyless" true instead of aspirational. Lesson 12.7.
#
# wif.tf removes the NEED for a service-account key. This removes the ABILITY to make
# one. Those are different things, and only the second survives a hurried afternoon:
# the pipeline being keyless does not stop somebody creating a key for a local script,
# emailing it to a colleague, and leaving it in a Slack thread for three years.
#
# iam.disableServiceAccountKeyCreation is a boolean constraint. Enforced, `gcloud iam
# service-accounts keys create` fails with a message naming this constraint - which is
# the useful part, because it tells the next person where the decision was made instead
# of looking like a permissions bug.
#
# Google enforces it BY DEFAULT on organizations created on or after 3 May 2024. If
# yours is older, or you turned it off, this turns it back on for this project.

variable "enforce_no_sa_keys" {
  type        = bool
  default     = false
  description = <<-EOT
    Enforce iam.disableServiceAccountKeyCreation on this project.

    Defaults to FALSE, and that default is about who is running this rather than about
    whether it is a good idea. Organization policy needs the project to belong to an
    ORGANIZATION and the caller to hold roles/orgpolicy.policyAdmin. A learner's
    throwaway project under a personal account has no organization at all, and this
    resource would fail the apply for a reason unrelated to anything they did.

    On a real project: set it to true. That is the whole exercise.
  EOT
}

resource "google_org_policy_policy" "no_sa_keys" {
  count  = var.enforce_no_sa_keys ? 1 : 0
  name   = "projects/${var.project_id}/policies/iam.disableServiceAccountKeyCreation"
  parent = "projects/${var.project_id}"

  spec {
    # inherit_from_parent is not set: this project's rule stands on its own, so a
    # loosened folder-level policy cannot quietly re-enable key creation here.
    rules {
      enforce = "TRUE"
    }
  }
}

output "sa_keys_disabled" {
  value       = var.enforce_no_sa_keys
  description = "TRUE means no service-account key can be created in this project."
}
