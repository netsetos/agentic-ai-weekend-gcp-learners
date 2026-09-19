#!/usr/bin/env python3
"""Read-only check of the Terraform-managed Vertex index and its deployment.

Uses only the Python standard library, gcloud, and Terraform. Run from any
directory with --deploy-root, --project, and --region. Resource names may use
a project ID or its numeric project number; only the freshly resolved pair is
accepted. This check does not establish document ingestion or query readiness.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


def project_identity(project, selected):
    project_id = project.get("projectId")
    project_number = str(project.get("projectNumber", ""))
    if not project_id or not re.fullmatch(r"[0-9]+", project_number):
        raise ValueError("Project lookup did not return a project ID and number")
    if selected not in {project_id, project_number}:
        raise ValueError(f"Project lookup mismatch: selected {selected!r}, got {project_id!r}")
    if project.get("lifecycleState") != "ACTIVE":
        raise ValueError(f"Project is not ACTIVE: {project.get('lifecycleState')!r}")
    return project_id, project_number


def canonical_resource(name, kind, region, identity):
    """Normalize the verified project alias, retaining every resource component."""
    match = re.fullmatch(
        r"projects/([^/]+)/locations/([^/]+)/(indexes|indexEndpoints)/([^/]+)",
        name if isinstance(name, str) else "",
    )
    if not match:
        raise ValueError(f"Malformed {kind} resource name: {name!r}")
    project, actual_region, actual_kind, resource_id = match.groups()
    if project not in identity:
        raise ValueError(f"Project mismatch in {name!r}; expected {identity[0]} ({identity[1]})")
    if actual_region != region or actual_kind != kind:
        raise ValueError(f"Expected locations/{region}/{kind}/...; got {name!r}")
    return f"projects/{identity[1]}/locations/{region}/{kind}/{resource_id}"


def validate(index, endpoint, index_name, endpoint_name, deployed_id, region, identity):
    expected_index = canonical_resource(index_name, "indexes", region, identity)
    expected_endpoint = canonical_resource(endpoint_name, "indexEndpoints", region, identity)
    actual_index = canonical_resource(index.get("name"), "indexes", region, identity)
    actual_endpoint = canonical_resource(endpoint.get("name"), "indexEndpoints", region, identity)
    if actual_index != expected_index:
        raise ValueError(f"Unexpected index: expected {index_name!r}, got {index.get('name')!r}")
    if actual_endpoint != expected_endpoint:
        raise ValueError(f"Unexpected endpoint: expected {endpoint_name!r}, got {endpoint.get('name')!r}")
    if index.get("indexUpdateMethod") != "STREAM_UPDATE":
        raise ValueError(f"Index is not streaming: {index.get('indexUpdateMethod')!r}")
    dimensions = index.get("metadata", {}).get("config", {}).get("dimensions")
    if str(dimensions) != "768":
        raise ValueError(f"Expected 768 dimensions, got {dimensions!r}")
    if not deployed_id:
        raise ValueError("Terraform returned an empty deployed index ID")
    deployments = endpoint.get("deployedIndexes", [])
    if not isinstance(deployments, list) or any(not isinstance(d, dict) for d in deployments):
        raise ValueError("Endpoint returned invalid deployedIndexes data")
    matches = [d for d in deployments if d.get("id") == deployed_id]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one deployment {deployed_id!r}; found {len(matches)}")
    attached_index = canonical_resource(matches[0].get("index"), "indexes", region, identity)
    if attached_index != expected_index:
        raise ValueError(f"Deployment {deployed_id!r} points to a different index: {matches[0].get('index')!r}")
    return matches[0]


def read_command(command):
    # Do not request, print, or persist access tokens. CLI failures stop the check.
    return subprocess.run(command, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy-root", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)
    try:
        root = args.deploy_root.expanduser().resolve()
        if not (root / "terraform").is_dir():
            raise ValueError(f"Terraform directory is missing: {root / 'terraform'}")
        project = json.loads(read_command([
            "gcloud", "projects", "describe", args.project, "--format=json",
        ]))
        identity = project_identity(project, args.project)
        values = [read_command([
            "terraform", f"-chdir={root / 'terraform'}", "output", "-raw", output,
        ]) for output in ("vector_index_name", "vector_index_endpoint", "vector_deployed_index_id")]
        index_name, endpoint_name, deployed_id = values
        # Check state ownership BEFORE looking up resource IDs in the selected project.
        canonical_resource(index_name, "indexes", args.region, identity)
        canonical_resource(endpoint_name, "indexEndpoints", args.region, identity)
        if not deployed_id:
            raise ValueError("Terraform returned an empty deployed index ID")
        print(f"Project: {identity[0]} ({identity[1]})", flush=True)
        print(f"Terraform index: {index_name}\nTerraform endpoint: {endpoint_name}", flush=True)
        documents = []
        for group, name in (("indexes", index_name), ("index-endpoints", endpoint_name)):
            documents.append(json.loads(read_command([
                "gcloud", "ai", group, "describe", name.rsplit("/", 1)[-1],
                f"--project={identity[0]}", f"--region={args.region}", "--format=json",
            ])))
        index, endpoint = documents
        evidence = root / "operator-evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        for filename, document in zip(("vector-index.json", "vector-endpoint.json"), documents):
            (evidence / filename).write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(f"API index: {index.get('name')}\nAPI endpoint: {endpoint.get('name')}", flush=True)
        deployment = validate(index, endpoint, index_name, endpoint_name, deployed_id, args.region, identity)
        print("Index dimensions: 768; update method: STREAM_UPDATE")
        print("Global vector count:", index.get("indexStats", {}).get("vectorsCount", "not reported"))
        print("Deployment:", deployment["id"])
        print("Deployment sync time:", deployment.get("indexSyncTime", "not reported"))
        print("PASS: the expected index is attached to the expected endpoint.")
        print("This verifies attachment and configuration; ingestion and query checks are separate.")
        return 0
    except (ValueError, KeyError, TypeError, AttributeError, OSError, subprocess.CalledProcessError) as error:
        print(f"STOP: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
