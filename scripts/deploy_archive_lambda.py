#!/usr/bin/env python3
"""FocalX-Archiv-Lambda reproduzierbar paketieren und deployen.

Voraussetzung: Die Rollen und SQS-Queues aus docs/aws-archiv-betrieb.md stehen.

    export AWS_PROFILE=focalx-deployer
    ~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_lambda.py

Das Paket enthält bewusst nur den Archivkern. Benchmark, Dashboard, FocalX-
Login und LLM-Abhängigkeiten gehören nicht in die Lambda.
"""
from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_FILES = (
    "archive/__init__.py",
    "archive/ingest.py",
    "archive/lambda_handler.py",
    "archive/store.py",
)


def package() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in PACKAGE_FILES:
            path = ROOT / name
            if not path.exists():
                raise FileNotFoundError(path)
            archive.writestr(name, path.read_bytes())
    return out.getvalue()


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", default="eu-central-1")
    ap.add_argument("--function", default="focalx-archive")
    ap.add_argument("--role", default="focalx-archive-lambda")
    ap.add_argument("--queue", default="focalx-archive")
    ap.add_argument("--bucket", default="sixt-focalx-archiv-test-180111006559")
    ap.add_argument("--prefix", default="focalx-push")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--memory", type=int, default=1024)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--package-only", type=Path,
                    help="nur ZIP schreiben, AWS nicht verändern")
    return ap


def main() -> int:
    args = parser().parse_args()
    payload = package()
    if args.package_only:
        args.package_only.write_bytes(payload)
        print(f"{args.package_only}: {len(payload)} Bytes")
        return 0

    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        print("boto3 fehlt — requirements-archive.txt installieren", file=sys.stderr)
        return 2

    session = boto3.session.Session(region_name=args.region)
    sts = session.client("sts")
    sqs = session.client("sqs")
    lamb = session.client("lambda")
    account = sts.get_caller_identity()["Account"]
    role_arn = f"arn:aws:iam::{account}:role/{args.role}"

    queue_url = sqs.get_queue_url(QueueName=args.queue)["QueueUrl"]
    attrs = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["QueueArn", "VisibilityTimeout"],
    )["Attributes"]
    queue_arn = attrs["QueueArn"]
    visibility = int(attrs["VisibilityTimeout"])
    if visibility < 6 * args.timeout:
        raise RuntimeError(
            f"SQS VisibilityTimeout {visibility}s ist kleiner als das "
            f"Sechsfache des Lambda-Timeouts ({6 * args.timeout}s)"
        )

    environment = {"Variables": {
        "ARCHIVE_BUCKET": args.bucket,
        "ARCHIVE_PREFIX": args.prefix,
        "DOWNLOAD_WORKERS": str(args.workers),
    }}
    try:
        lamb.get_function(FunctionName=args.function)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        lamb.create_function(
            FunctionName=args.function,
            Runtime="python3.12",
            Architectures=["arm64"],
            Handler="archive.lambda_handler.handler",
            Role=role_arn,
            Code={"ZipFile": payload},
            Timeout=args.timeout,
            MemorySize=args.memory,
            Environment=environment,
            Description="Archive one FocalX report received through SQS",
            Tags={
                "Project": "focalx-archive",
                "Environment": "dev",
                "Owner": "gottlieb.dinh@sixt.com",
            },
        )
        lamb.get_waiter("function_active_v2").wait(FunctionName=args.function)
        action = "angelegt"
    else:
        lamb.update_function_code(
            FunctionName=args.function,
            ZipFile=payload,
            Architectures=["arm64"],
        )
        lamb.get_waiter("function_updated_v2").wait(FunctionName=args.function)
        lamb.update_function_configuration(
            FunctionName=args.function,
            Runtime="python3.12",
            Handler="archive.lambda_handler.handler",
            Role=role_arn,
            Timeout=args.timeout,
            MemorySize=args.memory,
            Environment=environment,
            Description="Archive one FocalX report received through SQS",
        )
        lamb.get_waiter("function_updated_v2").wait(FunctionName=args.function)
        action = "aktualisiert"

    lamb.put_function_concurrency(
        FunctionName=args.function,
        ReservedConcurrentExecutions=args.concurrency,
    )

    mappings = lamb.list_event_source_mappings(
        FunctionName=args.function,
        EventSourceArn=queue_arn,
    )["EventSourceMappings"]
    mapping_args = {
        "FunctionName": args.function,
        "BatchSize": 1,
        "FunctionResponseTypes": ["ReportBatchItemFailures"],
        "ScalingConfig": {"MaximumConcurrency": args.concurrency},
        "Enabled": True,
    }
    if mappings:
        mapping = lamb.update_event_source_mapping(
            UUID=mappings[0]["UUID"],
            **mapping_args,
        )
    else:
        mapping = lamb.create_event_source_mapping(
            EventSourceArn=queue_arn,
            **mapping_args,
        )

    print(f"{args.function}: {action}, {len(payload)} Bytes")
    print(f"  Rolle:       {role_arn}")
    print(f"  Queue:       {queue_arn}")
    print(f"  Ziel:        s3://{args.bucket}/{args.prefix}/")
    print(f"  Parallel:    {args.concurrency} Inspektionen")
    print(f"  Trigger:     {mapping['UUID']} ({mapping['State']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
