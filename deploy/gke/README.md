# deploy/gke — vLLM on GKE Autopilot

Owned by lesson **11.5** (Cloud Run vs GKE Autopilot for LLM serving).

The question 11.5 answers is not "which is better" but **what do you give up, and where can
you put it**. Cloud Run scales to zero and costs nothing idle; Autopilot keeps a pod warm,
costs money at 3am, holds the model in GPU memory so the first request is not a cold start —
and can run an L4 in Mumbai, which Cloud Run cannot.

## Run it

```bash
terraform -chdir=../terraform apply -target=google_container_cluster.autopilot
gcloud container clusters get-credentials documind-autopilot --region "$REGION"

# The manifest runs the image lesson 11.1 builds (weights baked). Build and push
# it first, or the pod will pull a tag that does not exist:
#   gcloud builds submit --tag us-central1-docker.pkg.dev/$PROJECT_ID/vllm-repo/gemma-3-4b-it:latest ../services/gemma-vllm
kubectl apply -f vllm-deployment.yaml

kubectl get pods -w          # Accelerator provisioning takes minutes, not seconds
```

## Reach it

The Service is ClusterIP, so there is no public route and no load-balancer charge. Forward a
port and talk to it directly — this is the step the runbook used to be missing, and without
it the benchmark in 11.5 has nothing to call:

```bash
kubectl port-forward svc/documind-vllm 8080:80 &

curl -s localhost:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"google/gemma-3-4b-it",
       "messages":[{"role":"user","content":"What is the notice period for a senior engineer?"}]}' \
  | python -c 'import json,sys; print(json.load(sys.stdin)["choices"][0]["message"]["content"])'
```

## What it costs

Priced 2026-09-05 from Google's own tables, us-central1, per hour:

| line | rate |
|---|---|
| g2-standard-8 (8 vCPU, 32 GiB, 1× L4) | $0.853624 |
| Autopilot L4 GPU premium | $0.067 / GPU-hr |
| Autopilot vCPU premium | $0.003 × 8 |
| Autopilot memory premium | $0.00035 × 32 |
| **pod subtotal** | **$0.9558** |
| Autopilot cluster fee (after the free-tier credit) | $0.10 |
| **all-in** | **$1.0558/hr → $771/month → ₹65,514/month** |

Cloud Run's comparable instance is **$1.42092/hr → ₹88,168/month** at 100% duty — but **₹0
when idle**. That is the whole comparison, and it is a duty-cycle question: GKE wins above
roughly **74%**. Below that, Cloud Run's scale-to-zero wins.

> An earlier version of this file said "roughly Rs 72,000/month". That was in the right
> neighbourhood but unsourced; the table above is derived, and every line is checkable.

**Scale it down after the demo.** `kubectl delete -f vllm-deployment.yaml` releases the node;
deleting the cluster stops the $0.10/hr fee as well.

## Known gaps

Honest list, so nobody mistakes absence for a decision:

- **No VPC attachment.** `gke.tf` sets neither `network` nor `subnetwork`, so the cluster
  lands in the default VPC rather than `documind-vpc` from `network.tf`.
- **No autoscaling.** `replicas` is 1 and there is no HPA. The queue-depth HPA the plan calls
  for (`vllm:num_requests_waiting`) needs Managed Prometheus and a custom-metrics adapter that
  this kit does not provision — 11.5 explains the wiring rather than shipping a manifest that
  has never been run.
- **No Gateway.** There is no GKE Inference Gateway, `InferencePool` or `HTTPRoute` here. A
  previous header comment claimed otherwise; it has been removed.
