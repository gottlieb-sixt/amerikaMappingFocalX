#!/usr/bin/env python3
"""Den Endpoint anlegen, über den FocalX Reports abliefert.

    export AWS_PROFILE=focalx-deployer
    ~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_api.py

API Gateway nimmt den Report an, prüft den API-Key und legt ihn **direkt** in
die Warteschlange — ohne Lambda dazwischen. Das ist kein Sparen an der
falschen Stelle: Ein Annahmeweg ohne eigenen Code kann auch keinen eigenen
Fehler haben, und FocalX bekommt seine Antwort in Millisekunden statt nach den
drei Sekunden, die das Einordnen dauert.

Bewusst **ohne** Schema-Prüfung am Tor. Ein zu Unrecht abgewiesener Report ist
endgültig verloren — 4xx heißt für den Absender „nicht noch einmal versuchen".
Eine Nachricht, die erst die Lambda beanstandet, liegt dagegen in der
Fehlerablage und ist nachholbar. Solange FocalX' genaues Push-Format nicht
beobachtet ist, ist die nachholbare Variante die richtige.

Das Skript ist wiederholbar: Es sucht vorhandene Ressourcen anhand ihrer Namen
und legt nur an, was fehlt.
"""
from __future__ import annotations

import argparse
import sys

REGION_DEFAULT = "eu-central-1"
API_NAME = "focalx-archive"
STAGE = "v1"
PATH_PART = "inspections"
KEY_NAME = "focalx-push"
PLAN_NAME = "focalx-archive-push"

# SQS erwartet ein Formular, nicht JSON. Der Body wandert unverändert als
# MessageBody durch — urlEncode, weil er Ampersands enthalten kann.
REQUEST_TEMPLATE = "Action=SendMessage&MessageBody=$util.urlEncode($input.body)"
# Was FocalX zurückbekommt. Ohne diese Vorlage lieferten wir die XML-Antwort
# von SQS aus, samt interner Nachrichten-ID.
RESPONSE_TEMPLATE = '{"status":"accepted"}'


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", default=REGION_DEFAULT)
    ap.add_argument("--queue", default="focalx-archive")
    ap.add_argument("--role", default="focalx-archive-apigw-sqs")
    ap.add_argument("--rate", type=float, default=20.0,
                    help="erlaubte Anfragen je Sekunde (Vorgabe 20)")
    ap.add_argument("--burst", type=int, default=40)
    ap.add_argument("--quota", type=int, default=20000,
                    help="Anfragen je Tag; 2.000 erwartet, Rest ist Luft")
    return ap


def _resource_id(api, api_id: str, path: str) -> str | None:
    for item in api.get_resources(restApiId=api_id, limit=500)["items"]:
        if item["path"] == path:
            return item["id"]
    return None


def _once(call, **kwargs) -> None:
    """Anlegen, das ein bereits vorhandenes Gegenstück hinnimmt.

    botocore wiederholt Aufrufe von sich aus. Ist die erste Antwort unterwegs
    verloren gegangen, meldet der zweite Versuch einen Konflikt, obwohl genau
    das Gewünschte längst dasteht.
    """
    from botocore.exceptions import ClientError
    try:
        call(**kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConflictException":
            raise


def main() -> int:
    args = parser().parse_args()
    try:
        import boto3
        from botocore.exceptions import ClientError
    except ImportError:
        print("boto3 fehlt — requirements-archive.txt installieren", file=sys.stderr)
        return 2

    session = boto3.session.Session(region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    api = session.client("apigateway")
    role_arn = f"arn:aws:iam::{account}:role/{args.role}"

    existing = [a for a in api.get_rest_apis(limit=500)["items"]
                if a["name"] == API_NAME]
    if existing:
        api_id = existing[0]["id"]
        action = "vorhanden"
    else:
        api_id = api.create_rest_api(
            name=API_NAME,
            description="FocalX liefert Schadensreports zum Archivieren ab",
            endpointConfiguration={"types": ["REGIONAL"]},
            tags={"Project": "focalx-archive", "Environment": "dev"},
        )["id"]
        action = "angelegt"

    root = _resource_id(api, api_id, "/")
    resource = _resource_id(api, api_id, f"/{PATH_PART}")
    if resource is None:
        resource = api.create_resource(
            restApiId=api_id, parentId=root, pathPart=PATH_PART)["id"]

    # Die Methode wird jedes Mal neu gesetzt, damit keine von Hand in der
    # Konsole gedrehte Einstellung stehen bleibt. Der laufende Betrieb merkt
    # davon nichts: Die Stage bedient weiter die zuletzt veröffentlichte
    # Fassung, bis unten das neue Deployment erzeugt wird.
    try:
        api.delete_method(restApiId=api_id, resourceId=resource,
                          httpMethod="POST")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NotFoundException":
            raise
    _once(
        api.put_method,
        restApiId=api_id, resourceId=resource, httpMethod="POST",
        authorizationType="NONE", apiKeyRequired=True,
    )
    api.put_integration(
        restApiId=api_id, resourceId=resource, httpMethod="POST",
        type="AWS", integrationHttpMethod="POST",
        uri=f"arn:aws:apigateway:{args.region}:sqs:path/{account}/{args.queue}",
        credentials=role_arn,
        requestParameters={
            "integration.request.header.Content-Type":
                "'application/x-www-form-urlencoded'",
        },
        requestTemplates={"application/json": REQUEST_TEMPLATE},
        # Nur die Vorlage zählt. Ein fremder Content-Type bekommt 415 statt
        # ungeprüft an SQS durchgereicht zu werden.
        passthroughBehavior="NEVER",
    )
    _once(
        api.put_method_response,
        restApiId=api_id, resourceId=resource, httpMethod="POST",
        statusCode="200", responseModels={"application/json": "Empty"},
    )
    _once(
        api.put_integration_response,
        restApiId=api_id, resourceId=resource, httpMethod="POST",
        statusCode="200", selectionPattern="",
        responseTemplates={"application/json": RESPONSE_TEMPLATE},
    )

    api.create_deployment(restApiId=api_id, stageName=STAGE,
                          description="Push-Endpoint für FocalX")

    plans = [p for p in api.get_usage_plans(limit=500)["items"]
             if p["name"] == PLAN_NAME]
    stage_link = {"apiId": api_id, "stage": STAGE}
    if plans:
        plan_id = plans[0]["id"]
        if stage_link not in [{"apiId": s["apiId"], "stage": s["stage"]}
                              for s in plans[0].get("apiStages", [])]:
            api.update_usage_plan(usagePlanId=plan_id, patchOperations=[
                {"op": "add", "path": "/apiStages",
                 "value": f"{api_id}:{STAGE}"}])
    else:
        plan_id = api.create_usage_plan(
            name=PLAN_NAME,
            description="Ratenbegrenzung für den FocalX-Push",
            apiStages=[stage_link],
            throttle={"rateLimit": args.rate, "burstLimit": args.burst},
            quota={"limit": args.quota, "period": "DAY"},
        )["id"]

    keys = [k for k in api.get_api_keys(limit=500)["items"]
            if k["name"] == KEY_NAME]
    if keys:
        key_id = keys[0]["id"]
    else:
        key_id = api.create_api_key(
            name=KEY_NAME, description="Schlüssel für FocalX", enabled=True,
        )["id"]
        api.create_usage_plan_key(
            usagePlanId=plan_id, keyId=key_id, keyType="API_KEY")

    url = f"https://{api_id}.execute-api.{args.region}.amazonaws.com/{STAGE}/{PATH_PART}"
    print(f"{API_NAME}: {action}")
    print(f"  Endpoint:  POST {url}")
    print(f"  Warteschlange: {args.queue}")
    print(f"  Rolle:     {role_arn}")
    print(f"  Drosselung: {args.rate}/s, Spitze {args.burst}, {args.quota}/Tag")
    print(f"  Schlüssel-ID: {key_id}")
    print("\nDen Schlüsselwert bewusst nicht ausgegeben. Abrufen mit:")
    print(f"  aws apigateway get-api-key --api-key {key_id} "
          f"--include-value --query value --output text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
