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
    parser.add_argument("--top-k-default", type=int)
    parser.add_argument("--fetch-multiplier", type=int)
    parser.add_argument("--fetch-max", type=int)
    parser.add_argument("--max-total-chunks", type=int)
    parser.add_argument("--max-chunks-per-file", type=int)
    parser.add_argument("--rewrite-enabled", choices=("true", "false"))
    parser.add_argument("--experiment-diagnostics", action="store_true")
    parser.add_argument(
        "--title-rank-weight", type=float,
        help="Override SEARCH_TITLE_RERANK_WEIGHT for an A/B/C/D canary",
    )
    parser.add_argument(
        "--temporal-rank-weight", type=float,
        help="Override SEARCH_TEMPORAL_RANK_WEIGHT for a period-rule canary",
    )
    parser.add_argument("--sequence-rank-weight", type=float)
    parser.add_argument("--document-kind-rank-weight", type=float)
    parser.add_argument("--revision-rank-weight", type=float)
    parser.add_argument(
        "--reranker-mode", choices=("none", "rrf", "cross_encoder", "llm")
    )
    parser.add_argument("--reranker-candidate-k", type=int, choices=range(10, 31))
    parser.add_argument("--no-traffic", action="store_true")
    args = parser.parse_args()
    if args.image and "@sha256:" not in args.image:
        parser.error("Use a verified immutable image digest")
    if args.cache_ttl < 0:
        parser.error("Cache TTL must be nonnegative")
    if args.title_rank_weight is not None and args.title_rank_weight < 0:
        parser.error("--title-rank-weight must be nonnegative")
    if args.temporal_rank_weight is not None and args.temporal_rank_weight < 0:
        parser.error("--temporal-rank-weight must be nonnegative")
    if any(
        value is not None and value < 0
        for value in (
            args.sequence_rank_weight,
            args.document_kind_rank_weight,
            args.revision_rank_weight,
        )
    ):
        parser.error("metadata rank weights must be nonnegative")
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
    git_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_dirty = bool(
        subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    env = {
        "GCP_PROJECT_ID": project, "GCP_REGION": region,
        "GCS_HWP_ORIGINAL_BUCKET": "routing-required", "GCS_SOURCE_BUCKET": "routing-required",
        "RAG_CORPUS_NAME": "routing-required", "MCP_ROUTING_MODE": "registry",
        "FIRESTORE_DATABASE": common.get("FIRESTORE_DATABASE", "rag-sync-state"),
        "DOC_STATE_COLLECTION": common.get("DOC_STATE_COLLECTION", "doc_state"),
        "SEARCH_CACHE_TTL_SECONDS": str(args.cache_ttl),
        "GIT_COMMIT": git_commit,
        "GIT_DIRTY": str(git_dirty).lower(),
    }
    for name in (
        "TOP_K_DEFAULT",
        "SEARCH_FETCH_MULTIPLIER",
        "SEARCH_FETCH_MAX",
        "SEARCH_REWRITE_ENABLED",
        "SEARCH_REWRITE_MODEL",
        "SEARCH_REWRITE_TIMEOUT_SECONDS",
        "SEARCH_REWRITE_WEIGHT",
        "SEARCH_TITLE_RERANK_WEIGHT",
        "SEARCH_TEMPORAL_RANK_WEIGHT",
        "SEARCH_SEQUENCE_RANK_WEIGHT",
        "SEARCH_DOCUMENT_KIND_RANK_WEIGHT",
        "SEARCH_REVISION_RANK_WEIGHT",
        "SEARCH_RERANKER_MODE",
        "SEARCH_RERANKER_CANDIDATE_K",
        "SEARCH_CROSS_ENCODER_MODEL",
        "SEARCH_LLM_RERANKER_MODEL",
        "SEARCH_RERANKER_TIMEOUT_SECONDS",
        "SEARCH_MAX_TOTAL_CHUNKS",
        "SEARCH_MAX_CHUNKS_PER_FILE",
    ):
        if name in common:
            env[name] = str(common[name])
    if args.title_rank_weight is not None:
        env["SEARCH_TITLE_RERANK_WEIGHT"] = str(args.title_rank_weight)
    if args.temporal_rank_weight is not None:
        env["SEARCH_TEMPORAL_RANK_WEIGHT"] = str(args.temporal_rank_weight)
    overrides = {
        "SEARCH_SEQUENCE_RANK_WEIGHT": args.sequence_rank_weight,
        "SEARCH_DOCUMENT_KIND_RANK_WEIGHT": args.document_kind_rank_weight,
        "SEARCH_REVISION_RANK_WEIGHT": args.revision_rank_weight,
    }
    env.update({name: str(value) for name, value in overrides.items() if value is not None})
    if args.reranker_mode is not None:
        env["SEARCH_RERANKER_MODE"] = args.reranker_mode
    if args.reranker_candidate_k is not None:
        env["SEARCH_RERANKER_CANDIDATE_K"] = str(args.reranker_candidate_k)
        # A tagged breadth benchmark must be able to request and observe the
        # whole document candidate set through the MCP response.
        env["SEARCH_TOP_K_MAX"] = str(max(20, args.reranker_candidate_k))
    experiment_overrides = {
        "TOP_K_DEFAULT": args.top_k_default,
        "SEARCH_FETCH_MULTIPLIER": args.fetch_multiplier,
        "SEARCH_FETCH_MAX": args.fetch_max,
        "SEARCH_MAX_TOTAL_CHUNKS": args.max_total_chunks,
        "SEARCH_MAX_CHUNKS_PER_FILE": args.max_chunks_per_file,
    }
    env.update({name: str(value) for name, value in experiment_overrides.items() if value is not None})
    if args.rewrite_enabled is not None:
        env["SEARCH_REWRITE_ENABLED"] = args.rewrite_enabled
    if args.experiment_diagnostics:
        env["SEARCH_EXPERIMENT_DIAGNOSTICS"] = "true"
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
