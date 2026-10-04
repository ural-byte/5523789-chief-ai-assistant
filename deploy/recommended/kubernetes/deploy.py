"""Deploy owned resources with explicit context; credentials travel only on stdin."""

import argparse
import base64
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

NAMESPACE = "chief-ai-assistant"
ROOT = Path(__file__).resolve().parent
KEYS = {
    "SERVICE_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_TELEGRAM_USER_ID",
    "TIMEZONE",
    "AI_API_KEY",
    "AI_BASE_URL",
    "AI_FOLDER_ID",
    "GENERATION_MODEL",
    "EMBEDDING_DOC_MODEL",
    "EMBEDDING_QUERY_MODEL",
    "TOOL_PROTOCOL",
    "PRICING_PATH",
    "AI_TIMEOUT_SECONDS",
    "AI_ATTEMPTS",
    "POSTGRES_PASSWORD",
}
REQUIRED = {
    "SERVICE_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "ALLOWED_TELEGRAM_USER_ID",
    "AI_API_KEY",
    "AI_FOLDER_ID",
    "POSTGRES_PASSWORD",
}


def run(context, arguments, payload=None):
    result = subprocess.run(
        ["kubectl", "--context", context, *arguments],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        # kubectl diagnostics can echo Secret input. Never expose them.
        raise SystemExit(
            f"kubectl {arguments[0]} failed (exit {result.returncode}); inspect cluster status"
        )
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--image", required=True, help="Application image pinned by sha256 digest")
    parser.add_argument(
        "--postgres-image",
        default="pgvector/pgvector:pg16@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a",
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--telegram-route",
        action="store_true",
        help="Explicit optional Telegram hostAliases overlay",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}", args.image):
        raise SystemExit("--image must be an immutable sha256 reference")
    values = {k: v for k, v in dotenv_values(args.env_file).items() if k in KEYS and v is not None}
    if any(not values.get(key) for key in REQUIRED):
        raise SystemExit("Required environment settings are missing; see .env.example")
    password = quote(values["POSTGRES_PASSWORD"], safe="")
    values["DATABASE_URL"] = f"postgresql+psycopg://assistant:{password}@db:5432/assistant"
    manifests = ROOT.parent / "telegram-route" if args.telegram_route else ROOT
    rendered = run(args.context, ["kustomize", str(manifests)])
    rendered = rendered.replace("chief-ai-assistant:local", args.image)
    rendered = rendered.replace(
        "pgvector/pgvector:pg16@sha256:7b822b0aac60967beb1ea5e576b8602c94c300a157d187f385ae3e0da199b90a",
        args.postgres_image,
    )
    run(args.context, ["apply", "--dry-run=client", "--validate=false", "-f", "-"], rendered)
    if args.dry_run:
        print("Manifests rendered and validated; no resources or credentials written")
        return
    existing = json.loads(
        run(args.context, ["get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "json"])
        or "null"
    )
    if (
        existing
        and existing["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") != NAMESPACE
    ):
        raise SystemExit("Namespace already exists without ownership label")
    run(args.context, ["apply", "-f", str(ROOT / "namespace.yaml")])
    old_secret = json.loads(
        run(
            args.context,
            [
                "get",
                "secret",
                "chief-environment",
                "-n",
                NAMESPACE,
                "--ignore-not-found",
                "-o",
                "json",
            ],
        )
        or "null"
    )
    if (
        old_secret
        and old_secret["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") != NAMESPACE
    ):
        raise SystemExit("Secret exists without ownership label")
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {
            "name": "chief-environment",
            "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/part-of": NAMESPACE},
        },
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in values.items()},
    }
    run(
        args.context,
        ["apply", "--server-side", "--field-manager=chief-ai-assistant-deploy", "-f", "-"],
        json.dumps(secret),
    )
    run(args.context, ["apply", "--dry-run=server", "-f", "-"], rendered)
    print(run(args.context, ["apply", "-f", "-"], rendered).strip())
    # Secret updates also need to restart the existing pod; Recreate preserves one poller.
    if old_secret:
        run(args.context, ["rollout", "restart", "deployment/assistant", "-n", NAMESPACE])
    print(f"Applied to {args.context}/{NAMESPACE}; wait for rollout and check all containers")


if __name__ == "__main__":
    main()
