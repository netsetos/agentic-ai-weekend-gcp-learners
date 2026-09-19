# The existing project data source reads its billing linkage through the Cloud
# Billing API. Empty/omitted input therefore works for a normal rerun without a
# shell-only export. An explicit account remains available for existing callers.
locals {
  budget_billing_account_id = upper(trimprefix(trimspace(
    trimspace(var.billing_account_id) != "" ? var.billing_account_id :
    coalesce(data.google_project.current.billing_account, " ")
  ), "billingAccounts/"))
}

resource "google_billing_budget" "documind" {
  billing_account = local.budget_billing_account_id
  display_name    = "DocuMind monthly budget"

  lifecycle {
    precondition {
      condition = (
        can(regex("^[0-9A-Za-z]{6}-[0-9A-Za-z]{6}-[0-9A-Za-z]{6}$", local.budget_billing_account_id)) &&
        local.budget_billing_account_id != "000000-000000-000000"
      )
      error_message = "No valid billing account was resolved. Link billing to project_id, enable cloudbilling.googleapis.com and allow the Terraform identity to read the project's billing information, or set billing_account_id explicitly to a real XXXXXX-XXXXXX-XXXXXX account ID."
    }
  }

  budget_filter {
    # The Budgets API names projects by NUMBER. The id form was accepted by the plan and
    # refused by the apply ("Precondition check failed"), the first time it ran for real.
    projects = ["projects/${data.google_project.current.number}"]
  }

  amount {
    # In the billing account's own currency: the API refuses any other (an Indian account
    # is INR, and "USD" was refused on the first live apply as an invalid argument). Leave
    # budget_currency empty and the account's currency is used; the amount is then in it.
    specified_amount {
      currency_code = var.budget_currency != "" ? var.budget_currency : null
      units         = var.budget_amount
    }
  }

  threshold_rules { threshold_percent = 0.5 }
  threshold_rules { threshold_percent = 0.8 }
  threshold_rules { threshold_percent = 1.0 }
  threshold_rules {
    threshold_percent = 1.2
    spend_basis       = "FORECASTED_SPEND"
  }

  # Only when there is something to say. A rule with no channels and no topic is the API's own
  # default (emails to the billing admins), and the API stores nothing for it - so it read back
  # as empty and every plan after the first apply wanted to "update" the budget in place, for
  # ever (F33, the Module 10 plan on 10 September). An omitted block is the same behaviour with
  # nothing to drift.
  dynamic "all_updates_rule" {
    for_each = (length(var.alert_channels) > 0 || var.budget_pubsub) ? [1] : []
    content {
      monitoring_notification_channels = var.alert_channels
      disable_default_iam_recipients   = false
      # The Pub/Sub leg is opt-in (budget_pubsub). Under the domain-restricted-sharing org
      # policy (iam.allowedPolicyMemberDomains) the publisher grant below is refused, because
      # the billing budget agent is a Google system account outside the organisation - which
      # is how the first live apply on an organisation project ended. The emails to billing
      # admins (disable_default_iam_recipients = false) need no grant and always go out.
      pubsub_topic = var.budget_pubsub ? google_pubsub_topic.budget_alerts[0].id : null
    }
  }
  depends_on = [google_pubsub_topic_iam_member.budget_publisher]
}

resource "google_pubsub_topic" "budget_alerts" {
  count = var.budget_pubsub ? 1 : 0
  name  = "documind-budget-alerts"
}

# Budget notifications are published by Google's billing budget agent, which needs to be
# allowed to publish to the topic; without this the budget is created and never speaks.
resource "google_pubsub_topic_iam_member" "budget_publisher" {
  count  = var.budget_pubsub ? 1 : 0
  topic  = google_pubsub_topic.budget_alerts[0].id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:billing-budget-alert@system.gserviceaccount.com"
}

variable "billing_account_id" {
  type        = string
  default     = ""
  nullable    = false
  description = "Optional budget billing account ID (billingAccounts/ prefix accepted). Empty discovers the account already linked to project_id; discovery does not change billing linkage. A nonempty value overrides discovery for the budget."

  validation {
    condition = trimspace(var.billing_account_id) == "" || (
      can(regex("^(billingAccounts/)?[0-9A-Za-z]{6}-[0-9A-Za-z]{6}-[0-9A-Za-z]{6}$", trimspace(var.billing_account_id))) &&
      upper(trimprefix(trimspace(var.billing_account_id), "billingAccounts/")) != "000000-000000-000000"
    )
    error_message = "Leave billing_account_id empty to discover the project's linked billing account, or supply a real XXXXXX-XXXXXX-XXXXXX ID, optionally prefixed with billingAccounts/. The 000000-000000-000000 placeholder is not allowed."
  }
}
variable "alert_channels" {
  type    = list(string)
  default = []
}
variable "budget_pubsub" {
  type        = bool
  default     = false
  description = "Also publish budget notifications to a Pub/Sub topic. Needs an IAM grant to a Google system account, which an organisation with domain-restricted sharing refuses."
}
variable "budget_amount" {
  type        = string
  default     = "500"
  description = "Monthly budget, whole units of budget_currency (or of the billing account's currency when that is empty). The lesson's figure was 500 USD; `make up` passes BUDGET_AMOUNT."
}
variable "budget_currency" {
  type        = string
  default     = ""
  description = "ISO 4217 code, or empty for the billing account's currency - the only one the Budgets API accepts."
}
