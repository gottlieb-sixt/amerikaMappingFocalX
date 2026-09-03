#!/usr/bin/env python3
"""Alarme für das FocalX-Archiv einrichten.

    export AWS_PROFILE=focalx-deployer
    ~/.cache/focalx-s3venv/bin/python scripts/deploy_archive_alarms.py

Ein Archiv fällt nicht laut aus. Es hört einfach auf, Dinge aufzunehmen, und
niemand merkt es, bis jemand ein Bild von vor einem halben Jahr sucht. Die
Alarme hier decken deshalb zwei verschiedene Arten von Ausfall ab:

*Etwas geht schief* — Fehlerablage füllt sich, Lambda wirft Fehler, der
Endpoint antwortet mit Serverfehlern. Das sieht man ohnehin.

*Nichts geht mehr* — seit einem Tag kam kein einziger Report an. Das ist der
gefährlichere Fall, weil er wie Ruhe aussieht. Dieser eine Alarm ist im
Entwicklungskonto **stummgeschaltet**: Solange FocalX nicht regelmäßig
liefert, wäre er dauerhaft rot und würde alle anderen mit abstumpfen. Vor dem
Produktivgang scharfschalten (siehe Hinweis am Ende der Ausgabe).

Zwei Messwerte kommen aus den Lambda-Protokollen statt von AWS selbst: Die
Zeile, die die Lambda bei Erfolg beziehungsweise Misserfolg schreibt, wird zu
einer Zahl verdichtet. Das meldet einen Fehlschlag sofort, statt die gut halbe
Stunde abzuwarten, bis die Wiederholungen aufgebraucht sind.
"""
from __future__ import annotations

import argparse
import sys

NAMESPACE = "FocalXArchive"
LOG_GROUP = "/aws/lambda/focalx-archive"


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--region", default="eu-central-1")
    ap.add_argument("--topic", default="focalx-archive-alerts")
    ap.add_argument("--email", default="gottlieb.dinh@sixt.com",
                    help="bekommt die Alarme; muss die Anmeldung bestätigen")
    ap.add_argument("--function", default="focalx-archive")
    ap.add_argument("--queue", default="focalx-archive")
    ap.add_argument("--dlq", default="focalx-archive-dlq")
    ap.add_argument("--api", default="focalx-archive")
    ap.add_argument("--stage", default="v1")
    ap.add_argument("--stille-stunden", type=int, default=24,
                    help="so lange darf nichts ankommen, bevor es auffällt")
    return ap


def main() -> int:
    args = parser().parse_args()
    try:
        import boto3
    except ImportError:
        print("boto3 fehlt — requirements-archive.txt installieren", file=sys.stderr)
        return 2

    session = boto3.session.Session(region_name=args.region)
    sns = session.client("sns")
    logs = session.client("logs")
    cw = session.client("cloudwatch")

    topic_arn = sns.create_topic(
        Name=args.topic,
        Tags=[{"Key": "Project", "Value": "focalx-archive"}],
    )["TopicArn"]
    subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
    bekannt = {s["Endpoint"]: s["SubscriptionArn"] for s in subs}
    if args.email not in bekannt:
        sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=args.email)
        anmeldung = "Bestätigungsmail verschickt"
    elif bekannt[args.email] == "PendingConfirmation":
        anmeldung = "wartet noch auf die Bestätigung in der Mail"
    else:
        anmeldung = "bestätigt"

    for name, muster, metrik in (
        ("focalx-archive-failures", '{ $.event = "inspection_failed" }',
         "InspectionFailures"),
        ("focalx-archive-successes", '{ $.event = "inspection_archived" }',
         "InspectionsArchived"),
    ):
        logs.put_metric_filter(
            logGroupName=LOG_GROUP,
            filterName=name,
            filterPattern=muster,
            metricTransformations=[{
                "metricName": metrik,
                "metricNamespace": NAMESPACE,
                "metricValue": "1",
                "defaultValue": 0.0,
            }],
        )

    stille = args.stille_stunden * 3600
    alarme = [
        # (Name, Beschreibung, Namensraum, Messwert, Dimensionen, Statistik,
        #  Zeitfenster, Schwelle, Vergleich, fehlende Daten, scharf)
        ("focalx-archive-fehlerablage",
         "Mindestens eine Inspektion ist endgültig gescheitert und liegt in "
         "der Fehlerablage. Nachricht ansehen, Ursache beheben, zurückspielen.",
         "AWS/SQS", "ApproximateNumberOfMessagesVisible",
         {"QueueName": args.dlq}, "Maximum", 300, 0, "GreaterThanThreshold",
         "notBreaching", True),
        ("focalx-archive-stau",
         "Nachrichten warten länger als 30 Minuten. Entweder kommt die Lambda "
         "nicht hinterher oder sie scheitert wiederholt.",
         "AWS/SQS", "ApproximateAgeOfOldestMessage",
         {"QueueName": args.queue}, "Maximum", 300, 1800,
         "GreaterThanThreshold", "notBreaching", True),
        ("focalx-archive-einordnen-gescheitert",
         "Die Lambda konnte eine Inspektion nicht einordnen. Frühwarnung — "
         "die Wiederholungen laufen noch.",
         NAMESPACE, "InspectionFailures", {}, "Sum", 300, 0,
         "GreaterThanThreshold", "notBreaching", True),
        ("focalx-archive-lambda-fehler",
         "Die Lambda ist abgestürzt, statt einen Fehler zu melden. Deutet auf "
         "ein Problem außerhalb des Einordnens hin, etwa fehlende Rechte.",
         "AWS/Lambda", "Errors", {"FunctionName": args.function}, "Sum",
         300, 0, "GreaterThanThreshold", "notBreaching", True),
        ("focalx-archive-lambda-gedrosselt",
         "Die Parallelitätsgrenze greift. Kurz unkritisch, dauerhaft heißt "
         "es: Grenze anheben.",
         "AWS/Lambda", "Throttles", {"FunctionName": args.function}, "Sum",
         900, 0, "GreaterThanThreshold", "notBreaching", True),
        ("focalx-archive-endpoint-fehler",
         "Der Endpoint antwortet FocalX mit Serverfehlern. Reports können "
         "dabei verloren gehen, wenn FocalX nicht wiederholt.",
         "AWS/ApiGateway", "5XXError",
         {"ApiName": args.api, "Stage": args.stage}, "Sum", 300, 0,
         "GreaterThanThreshold", "notBreaching", True),
        ("focalx-archive-nichts-angekommen",
         f"Seit {args.stille_stunden} Stunden wurde keine Inspektion "
         "archiviert. Stiller Ausfall — vor dem Produktivgang scharfschalten.",
         NAMESPACE, "InspectionsArchived", {}, "Sum", stille, 1,
         "LessThanThreshold", "breaching", False),
    ]

    for (name, beschreibung, namensraum, messwert, dims, statistik,
         fenster, schwelle, vergleich, fehlend, scharf) in alarme:
        cw.put_metric_alarm(
            AlarmName=name,
            AlarmDescription=beschreibung,
            ActionsEnabled=scharf,
            AlarmActions=[topic_arn],
            OKActions=[topic_arn],
            Namespace=namensraum,
            MetricName=messwert,
            Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
            Statistic=statistik,
            Period=fenster,
            EvaluationPeriods=1,
            Threshold=float(schwelle),
            ComparisonOperator=vergleich,
            TreatMissingData=fehlend,
            Tags=[{"Key": "Project", "Value": "focalx-archive"}],
        )

    print(f"Meldeweg: {topic_arn}")
    print(f"  {args.email}: {anmeldung}")
    print(f"\n{len(alarme)} Alarme gesetzt:")
    for eintrag in alarme:
        marke = "scharf " if eintrag[10] else "stumm  "
        print(f"  {marke} {eintrag[0]}")
    print("\nScharfschalten, sobald FocalX regelmäßig liefert:")
    print(f"  aws cloudwatch enable-alarm-actions --region {args.region} "
          f"--alarm-names focalx-archive-nichts-angekommen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
