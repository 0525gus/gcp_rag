"""Deploy one scoped MCP without reading or copying legacy API keys into its env."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.dept_gui import _common, _provision_access_token
from scripts.mcp_registry import RegistryAdmin


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Immutable image digest; otherwise build the current checkout")
    parser.add_argument("--skip-build", action="store_true", help="Resolve the existing mcp:latest digest")
    parser.add_argument("--cpu", type=int, choices=(1, 2))
    parser.add_argument("--suffix", default="unified-" + datetime.now(UTC).strftime("%m%d-%H%M%S"))
    parser.add_argument("--tag")
    parser.add_argument("--cache-ttl", type=int, default=60)
    parser.add_argument("--no-traffic", action="store_true")
    args = parser.parse_args()
    if args.image and "@sha256:" not in args.image:
        parser.error("Use a verified immutable image digest")
    if args.cache_ttl < 0:
        parser.error("Cache TTL must be nonnegative")
    common = _common()
    project, region = common["GCP_PROJECT_ID"], common["GCP_REGION"]
    cpu = args.cpu or int(common.get("MCP_CPU", 1))
    if cpu not in (1, 2):
        parser.error("MCP_CPU must be 1 or 2")
    gcloud = shutil.which("gcloud")
    if not gcloud:
        parser.error("gcloud is required")
    admin = RegistryAdmin(project, region, common.get("FIRESTORE_DATABASE", "rag-sync-state"),
                          _provision_access_token())
    _, record, _ = admin.read()
    if not record:
        parser.error("Initialize the Cloud MCP registry before deploying the shared runtime")
    image = args.image
    if not image:
        tag = f"{region}-docker.pkg.dev/{project}/{common.get('ARTIFACT_REPO', 'rag-mcp')}/mcp:latest"
        if not args.skip_build:
            subprocess.run([gcloud, "builds", "submit", f"--project={project}",
                            "--config=cloudbuild.mcp.yaml", f"--substitutions=_IMAGE={tag}",
                            "--quiet"], cwd=ROOT, check=True)
        digest = subprocess.run([gcloud, "artifacts", "docker", "images", "describe", tag,
                                 f"--project={project}", "--format=value(image_summary.digest)"],
                                check=True, capture_output=True, text=True).stdout.strip()
        if not digest.startswith("sha256:"):
            raise RuntimeError("Could not resolve the MCP image digest")
        image = tag.rsplit(":", 1)[0] + "@" + digest
    env = {
        "GCP_PROJECT_ID": project, "GCP_REGION": region,
        "GCS_HWP_ORIGINAL_BUCKET": "routing-required", "GCS_SOURCE_BUCKET": "routing-required",
        "RAG_CORPUS_NAME": "routing-required", "MCP_ROUTING_MODE": "registry",
        "FIRESTORE_DATABASE": common.get("FIRESTORE_DATABASE", "rag-sync-state"),
        "DOC_STATE_COLLECTION": common.get("DOC_STATE_COLLECTION", "doc_state"),
        "SEARCH_CACHE_TTL_SECONDS": str(args.cache_ttl),
    }
    for name in ("TOP_K_DEFAULT", "SEARCH_FETCH_MULTIPLIER", "SEARCH_FETCH_MAX"):
        if name in common:
            env[name] = str(common[name])
    flags = {"--set-env-vars": env}
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as handle:
        yaml.safe_dump(flags, handle)
        path = handle.name
    try:
        command = [gcloud, "run", "deploy", "rag-mcp", f"--project={project}",
                   f"--region={region}", f"--image={image}", f"--cpu={cpu}", "--memory=1Gi",
                   "--concurrency=40", "--min=0", "--min-instances=0", "--max-instances=100",
                   "--cpu-throttling", "--cpu-boost", "--timeout=300", "--allow-unauthenticated",
                   f"--service-account=rag-mcp-search@{project}.iam.gserviceaccount.com",
                   f"--revision-suffix={args.suffix}", f"--flags-file={path}", "--quiet"]
        if args.no_traffic:
            command.append("--no-traffic")
        if args.tag:
            command.append(f"--tag={args.tag}")
        subprocess.run(command, check=True)
    finally:
        os.unlink(path)
    if not args.no_traffic:
        # Cloud Run retains an explicit revision split after a tagged comparison.
        # Always promote this revision, rather than reporting the old target as live.
        subprocess.run([
            gcloud, "run", "services", "update-traffic", "rag-mcp", f"--project={project}",
            f"--region={region}", f"--to-revisions=rag-mcp-{args.suffix}=100", "--quiet",
        ], check=True)
        service = json.loads(subprocess.run([
            gcloud, "run", "services", "describe", "rag-mcp", f"--project={project}",
            f"--region={region}", "--format=json",
        ], check=True, capture_output=True, text=True).stdout)
        url = service["status"]["url"]
        # Re-read after the deployment: concurrent key changes must survive URL publication.
        admin = RegistryAdmin(project, region, common.get("FIRESTORE_DATABASE", "rag-sync-state"),
                              _provision_access_token())
        _, current, configs = admin.read()
        if current.get("serviceUrl") != url:
            admin.write(configs, service_url=url, expected_revision=current["revision"])
        print(f"Unified MCP: {url}/mcp (cpu={cpu}, min=0)")


if __name__ == "__main__":
    main()
