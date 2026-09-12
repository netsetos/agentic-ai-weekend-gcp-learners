# Staging, then a canary in prod. UNOWNED: lesson 12.7 teaches it.
#
# A canary is not a slower deploy - it is a deploy you can stop. 10% of traffic on the
# new revision, a pause, then the rest; `gcloud deploy targets rollback documind-prod`
# at any point. (The `targets` group is not optional - there is no bare
# `gcloud deploy rollback`, and finding that out during an incident is the wrong time.)
resource "google_clouddeploy_target" "staging" {
  count = local.full ? 1 : 0   # the full profile only (variables.tf)
  location = var.region
  name     = "documind-staging"
  run { location = "projects/${var.project_id}/locations/${var.region}" }
  execution_configs {
    usages            = ["RENDER", "DEPLOY", "VERIFY"]
    service_account   = google_service_account.cicd.email
    execution_timeout = "3600s"
  }
}

resource "google_clouddeploy_target" "prod" {
  count = local.full ? 1 : 0   # the full profile only (variables.tf)
  location = var.region
  name     = "documind-prod"
  run { location = "projects/${var.project_id}/locations/${var.region}" }
  # A human says yes before production. The eval gate in 12.7 runs before this point;
  # this is the second gate, and it is a person.
  require_approval = true
  execution_configs {
    usages            = ["RENDER", "DEPLOY", "VERIFY"]
    service_account   = google_service_account.cicd.email
    execution_timeout = "3600s"
  }
}

resource "google_clouddeploy_delivery_pipeline" "documind" {
  count = local.full ? 1 : 0   # the full profile only (variables.tf)
  location = var.region
  name     = "documind"
  serial_pipeline {
    stages {
      target_id = google_clouddeploy_target.staging[0].name
      profiles  = ["staging"]
    }
    stages {
      target_id = google_clouddeploy_target.prod[0].name
      profiles  = ["prod"]
      strategy {
        canary {
          runtime_config {
            cloud_run { automatic_traffic_control = true }
          }
          canary_deployment {
            percentages = [10]
            # FALSE, deliberately. verify runs a skaffold `verify` stanza, and the release is
            # cut with --from-run-manifest, whose generated skaffold config has none. Leaving
            # this true makes the canary fail at its verify phase - AFTER 10% of production
            # traffic is already on the new revision, which is the worst moment to find out.
            #
            # The check still happens: documind-cd.yml waits for the staging rollout and then
            # runs deploy/evals/run_eval.py against it, before prod is promoted at all. Flip
            # this to true the day a skaffold verify stanza exists to run.
            verify = false
          }
        }
      }
    }
  }
}

output "delivery_pipeline" { value = one(google_clouddeploy_delivery_pipeline.documind[*].name) }
