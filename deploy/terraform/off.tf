# The nightly off switch. Cost control, 10 September 2026.
#
# Everything GPU-shaped on the lane is explicit and ends the day at zero - by hand (make off) and, from tonight, by
# this job: a Cloud Run job on the gcloud image, run by Cloud Scheduler at 23:00 IST, that sets min-instances 0 on
# documind-slm, documind-vllm, documind-gateway and documind-ui wherever a floor is above zero. (Until 15 September
# 2026 it also deleted a leftover Autopilot cluster; the cluster is Terraform's now, gke.tf, and make gke-down
# removes only the workload.) It never builds or deploys anything. A forgotten L4 then costs an evening
# (11.4: Rs 121 an hour), not a month (Rs 86,904). Updating a service mints a revision that runs AS that service's
# account, which is why the job's account needs actAs on each of them - the default compute account included, which
# is what make deploy-slm runs the SLM as. The alarm for the same condition is alerts.tf's gpu_left_warm; the ceiling
# below both is make gpu-cap.
resource "google_service_account" "off" {
  account_id   = "documind-off-sa"
  display_name = "DocuMind nightly off (scales the GPU services to zero)"
}

resource "google_project_iam_member" "off_run" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.off.email}"
}

resource "google_project_iam_member" "off_gke" {
  project = var.project_id
  role    = "roles/container.clusterAdmin"
  member  = "serviceAccount:${google_service_account.off.email}"
}

data "google_compute_default_service_account" "default" {}

resource "google_service_account_iam_member" "off_act_as" {
  for_each = {
    gateway = google_service_account.gateway.name
    vllm    = google_service_account.vllm.name
    ui      = google_service_account.ui.name
    compute = data.google_compute_default_service_account.default.name
  }
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.off.email}"
}

locals {
  # The job's whole script. A floor is read back as JSON and parsed with the image's own python (no jq, no gcloud
  # projection quoting to get wrong); a service already at zero is left alone, so a quiet night mints no revision.
  off_script = <<-EOT
    set -u
    floor() {
      gcloud run services describe "$1" --region "$2" --project "$PROJECT" --format=json 2>/dev/null         | python3 -c 'import json, sys; j = json.load(sys.stdin); print((j["spec"]["template"]["metadata"].get("annotations") or {}).get("autoscaling.knative.dev/minScale", "0"))' 2>/dev/null         || echo absent
    }
    for pair in documind-slm:$SLM_REGION documind-vllm:$SLM_REGION documind-gateway:$REGION documind-ui:$REGION; do
      s=$${pair%%:*}; r=$${pair##*:}; f=$(floor "$s" "$r")
      if [ "$f" = absent ]; then echo "$s: not deployed"
      elif [ "$f" = 0 ]; then echo "$s: min-instances 0 already"
      else gcloud run services update "$s" --region "$r" --project "$PROJECT" --min-instances 0 --quiet && echo "$s: min-instances $f -> 0"
      fi
    done
    echo "documind-autopilot: Terraform's since 15 September 2026 (gke.tf) - make gke-down removes the vLLM workload, make down the cluster"
    EOT
}

resource "google_cloud_run_v2_job" "off" {
  name                = "documind-off"
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.off.email
      timeout         = "1200s"
      max_retries     = 0
      containers {
        image   = "gcr.io/google.com/cloudsdktool/google-cloud-cli:slim"
        command = ["/bin/bash", "-c"]
        args    = [local.off_script]
        env {
          name  = "PROJECT"
          value = var.project_id
        }
        env {
          name  = "REGION"
          value = var.region
        }
        env {
          name  = "SLM_REGION"
          value = "us-central1"
        }
      }
    }
  }
}

resource "google_cloud_run_v2_job_iam_member" "off_invoker" {
  name     = google_cloud_run_v2_job.off.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.off.email}"
}

# 23:00 IST, every night, the Cloud Run Admin API's run verb on the job with the job's own account's OAuth token.
# make off-now runs the same job on demand; make off runs the same four switches from a shell.
resource "google_cloud_scheduler_job" "off" {
  name        = "documind-off-nightly"
  description = "DocuMind: floor the GPU services, the gateway and the UI to zero (the Autopilot cluster is Terraform's; its workload is make gke-down's)"
  schedule    = "0 23 * * *"
  time_zone   = "Asia/Kolkata"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.off.name}:run"
    oauth_token {
      service_account_email = google_service_account.off.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.off_invoker]
}
