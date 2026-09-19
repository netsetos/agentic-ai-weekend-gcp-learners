# All runs are plans with mock providers. No cloud credentials, writes or state are used.
mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number          = "123456789012"
      billing_account = "ABCDEF-123456-ABCDEF"
    }
  }
}
mock_provider "google-beta" {}

run "omitted_account_uses_project_link" {
  command = plan
  assert {
    condition     = google_billing_budget.documind.billing_account == "ABCDEF-123456-ABCDEF"
    error_message = "An omitted account must use the project's linked account."
  }
}

run "empty_account_uses_project_link" {
  command = plan
  variables { billing_account_id = "" }
  assert {
    condition     = google_billing_budget.documind.billing_account == "ABCDEF-123456-ABCDEF"
    error_message = "An empty account from old tfvars must still use the project link."
  }
}

run "blank_account_uses_project_link" {
  command = plan
  variables { billing_account_id = "  " }
  assert {
    condition     = google_billing_budget.documind.billing_account == "ABCDEF-123456-ABCDEF"
    error_message = "Whitespace input must not hide the project's linked account."
  }
}

run "explicit_account_is_normalized" {
  command = plan
  variables { billing_account_id = " billingAccounts/abcdef-987654-fedcba " }
  assert {
    condition     = google_billing_budget.documind.billing_account == "ABCDEF-987654-FEDCBA"
    error_message = "A valid explicit account must retain its override behavior and be normalized."
  }
}

run "placeholder_account_rejected" {
  command = plan
  variables { billing_account_id = "000000-000000-000000" }
  expect_failures = [var.billing_account_id]
}

run "malformed_account_rejected" {
  command = plan
  variables { billing_account_id = "not-an-account" }
  expect_failures = [var.billing_account_id]
}

run "missing_project_link_rejected" {
  command = plan
  override_data {
    target = data.google_project.current
    values = { number = "123456789012", billing_account = "" }
  }
  expect_failures = [google_billing_budget.documind]
}

# A real provider read error (for example 403) already stops Terraform. This
# covers a successful read that nevertheless supplies no usable billing value.
run "null_billing_account_rejected" {
  command = plan
  override_data {
    target = data.google_project.current
    values = { number = "123456789012", billing_account = null }
  }
  expect_failures = [google_billing_budget.documind]
}

run "explicit_digital_parser_for_both_tenants" {
  command = plan
  variables { managed_search = true }
  assert {
    condition = alltrue([
      for tenant in ["acme", "zeta"] :
      length(google_discovery_engine_data_store.tenant[tenant].document_processing_config[0].default_parsing_config[0].digital_parsing_config) == 1
    ])
    error_message = "Both stores must declare the API-returned digital parser to avoid replacement drift."
  }
  assert {
    condition = alltrue([
      for tenant in ["acme", "zeta"] :
      google_discovery_engine_data_store.tenant[tenant].data_store_id == "documind-${tenant}"
    ])
    error_message = "The fix must preserve the existing data-store IDs."
  }
}
