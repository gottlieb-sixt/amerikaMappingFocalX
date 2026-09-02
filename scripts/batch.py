#!/usr/bin/env python3
"""Unbeaufsichtigter Stapellauf: Lynx → FocalX → Piktogramm → v08-Mapping.

Gedacht für Größenordnungen, bei denen Zuschauen keine Option ist (500 Autos ≈
10 Tage Rechenzeit). Entsprechend ist alles darauf ausgelegt, dass ein Fehler
NUR das betroffene Auto trifft:

* **Phasenweise.** Erst alle Autos durch FocalX, dann alle durch die
  Entdopplung, dann Piktogramme, dann Mapping. Jede Phase ist für sich
  wiederaufsetzbar; ein Abbruch verliert höchstens das Auto in Arbeit.
* **Fertig ist fertig.** Der Zustand wird aus den ABLAGEN gelesen, nicht aus
  einer Buchhaltung — geht das Manifest verloren, erkennt der nächste Lauf
  trotzdem, was schon da ist. Nichts Fertiges wird angefasst.
* **Ein Auto je Unterprozess.** Ein Absturz, ein Timeout oder kaputtes JSON
  reißt nie die Warteschlange mit; nach `--max-attempts` Versuchen wandert das
  Auto in die Quarantäne und wird am Ende ausgewiesen.
* **Atomar geschrieben.** Manifest und Ergebnisse gehen über eine temporäre
  Datei — ein Kill mitten im Schreiben hinterlässt keine Trümmer.
* **Nur ein Lauf pro Run.** Ein Sperrdatei-Check verhindert zwei Orchestratoren
  auf demselben Verzeichnis (das hat schon einmal Ergebnisse vermischt).
* **Zeiten werden mitgeschrieben** — je Auto und Phase, für Piktogramme und
  Mapping zusätzlich je Schaden/Urteil in den Ergebnisdateien selbst.

Der Lynx-Token lebt nur ~5 Minuten. Die `fetch`-Phase wartet daher vor jedem
Aufruf darauf, dass eine frische `.lynx_token` daliegt, und macht von selbst
weiter, sobald du eine ablegst — kein Neustart nötig.

  python3 -u scripts/batch.py --run fl500 seed --from v3      # fertige übernehmen
  python3 -u scripts/batch.py --run fl500 status
  python3 -u scripts/batch.py --run fl500 fetch --days 2026-07-17..2026-07-20
  # Token sparen bei mehreren Filialen: erst alle Tage, dann SHARK in einem Zug
  python3 -u scripts/batch.py --run fl500 fetch --branch 44413 \
      --days 2026-07-23,2026-07-26 --days-only
  python3 -u scripts/batch.py --run fl500 fetch --gt-only
  python3 -u scripts/batch.py --run fl500 all --limit 30      # Pilot
  python3 -u scripts/batch.py --run fl500 all                 # der ganze Rest
  python3 -u scripts/batch.py --run fl500 mapping --workers 2
  python3 -u scripts/batch.py --run fl500 all --retry-failed   # Quarantäne leeren
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval import runs as runs_mod                                  # noqa: E402
from eval.ground_truth import load_truths                          # noqa: E402
from eval.pipeline import images_for                               # noqa: E402

RAW = ROOT / "data" / "raw"
GT_DIR = ROOT / "data" / "ground_truth"
TOKEN = ROOT / ".lynx_token"
TOKEN_TTL_S = 240          # der Token lebt ~5 min — mit Sicherheitsabstand
PY = sys.executable or "python3"
VENV_PY = ROOT / ".venv" / "bin" / "python"      # Piktogramme brauchen Pillow
# Welche Phase mit welchem Interpreter läuft. Nicht nur die Lokalisierung
# braucht Pillow: der Judge bekommt bei Strategien mit Geometrie (pictogram /
# canon_distances) das Piktogramm als Bild und den Abstand je Kandidat, und
# beides entsteht über PIL. Fehlt es, liefert eval.pictogram fail-soft None —
# der Lauf sieht erfolgreich aus und hat die halbe Strategie verschenkt. Genau
# das ist im Pilotlauf fl500 passiert: gestartet aus tmux (Homebrew-python3.14
# ohne Pillow), 46 245 Kandidaten-Abstände still `null`.
PIL_PHASES = {"pictograms", "mapping"}
PHASES = ["fetch", "focalx", "cluster", "truths", "pictograms", "mapping"]
PREREQ = {"cluster": "focalx", "truths": "cluster",
          "pictograms": "cluster", "mapping": "truths"}
WORKER_PHASES = {"focalx", "pictograms", "mapping"}
MAX_WORKERS = 3                                  # Rate-Limit des Gateways
# FocalX ist der Engpass: 13 min je Auto, davon fast alles Warten auf den
# Server. Parallel läuft nur die Wartezeit übereinander — bei 600 Autos macht
# das den Unterschied zwischen fünf Tagen und knapp zwei. FocalX hat kein
# bekanntes Org-Limit (anders als das LLM-Gateway), darf also weiter aufdrehen.
PHASE_MAX_WORKERS = {"focalx": 6}

# Obergrenze je Auto und Phase (Minuten). Ohne die kann ein einziges Auto die
# Warteschlange auf Tage blockieren: eine schleppende Antwort hängt nicht am
# Socket-Timeout, sie trickelt nur. Die Werte sind grob dreimal die gemessene
# Dauer — was länger braucht, ist kaputt und nicht langsam.
PHASE_TIMEOUT_MIN = {"focalx": 45, "cluster": 30, "truths": 20,
                     "pictograms": 60, "mapping": 45}

_stop = False


def _on_signal(signum, _frame):
    """Abbruch nach dem aktuellen Auto — nie mitten im Schreiben."""
    global _stop
    _stop = True
    print(f"\n⏸  Signal {signum} — beende nach dem laufenden Auto …", flush=True)


# ── Hilfen ──────────────────────────────────────────────────────────────────

def plate_key(plate: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", plate or "").upper()


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def hhmm(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 5400:
        return f"{seconds/60:.0f} min"
    return f"{seconds/3600:.1f} h"


VALUE_FLAGS = ["--run", "--branch", "--days", "--cars", "--strategy", "--limit",
               "--workers", "--max-attempts", "--token-timeout", "--from",
               "--timeout"]


class Opt:
    """Kommandozeile — bewusst schlicht, damit stdlib-only bleibt."""

    def __init__(self, argv: list[str]):
        # Werte von Schaltern dürfen nicht als Phase durchgehen ("--run fl500").
        skip = {i + 1 for i, a in enumerate(argv) if a in VALUE_FLAGS}
        self.phases = [a for i, a in enumerate(argv)
                       if not a.startswith("--") and i not in skip]
        self.run = self._val(argv, "--run", "")
        self.branch = self._val(argv, "--branch", "40139")
        self.days = self._val(argv, "--days", "")
        self.cars = [c for c in self._val(argv, "--cars", "").split(",") if c]
        self.strategy = self._val(argv, "--strategy", "v08-all-distance")
        self.from_run = self._val(argv, "--from", "")
        self.limit = int(self._val(argv, "--limit", "0"))
        # Die Obergrenze hängt an der Phase (s. PHASE_MAX_WORKERS), deshalb hier
        # nur einlesen und erst in do_phase begrenzen.
        self.workers = max(1, int(self._val(argv, "--workers", "1")))
        self.max_attempts = int(self._val(argv, "--max-attempts", "3"))
        self.token_timeout = int(self._val(argv, "--token-timeout", "0"))
        # Minuten je Auto und Phase; 0 = Vorgaben aus PHASE_TIMEOUT_MIN.
        self.timeout_min = int(self._val(argv, "--timeout", "0"))
        # Vorgabe: nur Autos, die in SHARK einen Exterior-Schaden haben — bei
        # schadenfreien Autos gibt es nichts zu mappen. --include-clean nimmt alle.
        self.only_damaged = "--include-clean" not in argv
        self.retry_failed = "--retry-failed" in argv
        # Token sparen: erst alle Tage aus mehreren Filialen holen (je Tag ein
        # Aufruf), die SHARK-Wahrheiten danach in einem Rutsch für alle.
        self.days_only = "--days-only" in argv
        # Gegenstück: nur die SHARK-Wahrheiten + GT-Fotos, für alle schon
        # geholten Tage aller Filialen.
        self.gt_only = "--gt-only" in argv
        self.dry_run = "--dry-run" in argv

    @staticmethod
    def _val(argv: list[str], flag: str, default: str) -> str:
        return argv[argv.index(flag) + 1] if flag in argv else default


# ── Manifest (Buchhaltung: Versuche, Zeiten, Quarantäne) ────────────────────

class Manifest:
    """Buchhaltung, die zwei gleichzeitige Orchestratoren verträgt.

    Laufen etwa FocalX und die Piktogramme in getrennten Sitzungen, schreiben
    beide dieselbe Datei. Wer zuletzt speichert, dürfte dabei nicht die Einträge
    des anderen mit seinem Stand von vor einer Stunde überbügeln — deshalb wird
    beim Speichern neu eingelesen und nur das eigene Delta hineingelegt."""

    def __init__(self, path: Path):
        self.path = path
        self.data = self._read()
        self._dirty: set[tuple[str, str]] = set()      # (checkin, phase)
        self._dirty_days: set[str] = set()

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
        except Exception:
            d = {}
        d.setdefault("cars", {})
        d.setdefault("days", {})
        return d

    def car(self, checkin: str) -> dict:
        c = self.data["cars"].setdefault(checkin, {"phases": {}})
        c.setdefault("phases", {})
        return c

    def phase(self, checkin: str, phase: str) -> dict:
        return self.car(checkin)["phases"].setdefault(phase, {})

    def record(self, checkin: str, phase: str, ok: bool, secs: float,
               note: str = "") -> None:
        p = self.phase(checkin, phase)
        p["attempts"] = int(p.get("attempts", 0)) + 1
        p["seconds"] = round(secs, 1)
        p["at"] = datetime.now().isoformat(timespec="seconds")
        p["status"] = "done" if ok else "failed"
        if note:
            p["note"] = note[:500]
        self._dirty.add((checkin, phase))
        self.save()

    def blocked(self, checkin: str, phase: str, max_attempts: int) -> bool:
        p = self.phase(checkin, phase)
        return (p.get("status") == "failed"
                and int(p.get("attempts", 0)) >= max_attempts)

    def clear_failures(self) -> int:
        n = 0
        for checkin, c in self.data["cars"].items():
            for phase, p in c.get("phases", {}).items():
                if p.get("status") == "failed":
                    p["attempts"] = 0
                    p.pop("status", None)
                    self._dirty.add((checkin, phase))
                    n += 1
        self.save()
        return n

    def mark_day(self, key: str) -> None:
        self._dirty_days.add(key)

    def save(self) -> None:
        disk = self._read()
        for checkin, phase in self._dirty:
            entry = self.data["cars"].get(checkin, {}).get("phases", {}).get(phase)
            if entry is not None:
                car = disk["cars"].setdefault(checkin, {"phases": {}})
                car.setdefault("phases", {})[phase] = entry
        for key in self._dirty_days:
            if key in self.data["days"]:
                disk["days"][key] = self.data["days"][key]
        for k, v in self.data.items():
            if k not in ("cars", "days"):
                disk[k] = v
        self.data = disk
        write_atomic(self.path, disk)
        self._dirty.clear()
        self._dirty_days.clear()


# ── Sperre: nur ein Orchestrator je Run ─────────────────────────────────────

def _lock_holder(path: Path) -> int:
    """PID des lebenden Sperrhalters, sonst 0."""
    if not path.exists():
        return 0
    try:
        pid = int(json.loads(path.read_text()).get("pid", 0))
    except Exception:
        return 0
    if not pid:
        return 0
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 0                                  # es gibt den Prozess nicht mehr
    except PermissionError:
        pass                                      # existiert, gehört nur anderen
    return pid


def acquire_lock(path: Path) -> bool:
    """Sperre je PHASE, nicht je Run.

    Zwei Sitzungen an verschiedenen Phasen sind erwünscht — während FocalX noch
    läuft, können die Piktogramme schon die fertigen Autos abarbeiten. Nur
    dieselbe Phase zweimal wäre gefährlich (zwei Prozesse am selben Auto)."""
    pid = _lock_holder(path)
    if pid:
        print(f"  Phase {path.stem} läuft schon (PID {pid}) — übersprungen.",
              flush=True)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(path, {"pid": os.getpid(),
                        "since": datetime.now().isoformat(timespec="seconds")})
    return True


# ── Autos finden ────────────────────────────────────────────────────────────

def parse_days(spec: str) -> list[str]:
    """'2026-07-17..2026-07-20' oder '2026-07-17,2026-07-19' → Liste von Tagen."""
    if not spec:
        return []
    if ".." in spec:
        a, b = spec.split("..", 1)
        d0, d1 = date.fromisoformat(a.strip()), date.fromisoformat(b.strip())
        return [(d0 + timedelta(days=i)).isoformat()
                for i in range((d1 - d0).days + 1)]
    return [s.strip() for s in spec.split(",") if s.strip()]


def gt_file(plate: str) -> Path | None:
    pk = plate_key(plate)
    f = GT_DIR / f"{pk}.json"
    if f.exists():
        return f
    return next((p for p in GT_DIR.glob("*.json") if pk in p.stem.upper()), None)


def has_damage(plate: str) -> bool:
    """Exterior-Schaden in SHARK? Ohne GT-Datei: unbekannt → mitnehmen."""
    f = gt_file(plate)
    if f is None:
        return True
    try:
        return bool(load_truths(f, exterior_only=True))
    except Exception:
        return True


def discover_cars(opt: Opt) -> list[dict]:
    """Check-in-Ordner aus data/raw — die Grundgesamtheit des Stapels."""
    days = set(parse_days(opt.days))
    out = []
    for day_dir in sorted(RAW.iterdir()):
        if not day_dir.is_dir() or (days and day_dir.name not in days):
            continue
        for c in sorted(day_dir.iterdir()):
            if not c.is_dir():
                continue
            if opt.cars and not any(s in c.name for s in opt.cars):
                continue
            plate = c.name.split("__")[0]
            if opt.only_damaged and not has_damage(plate):
                continue
            # Ohne Walkaround-Bilder ist nichts zu analysieren. Rund jeder achte
            # Check-in hat nur ein „extra"-Foto — solche Autos gehören nicht in
            # die Grundgesamtheit, sonst zählen sie als Dauerfehler.
            if not images_for(c):
                continue
            out.append({"checkin": c.name, "plate": plate, "day": day_dir.name,
                        "dir": c})
    return out


# ── Ist die Phase für dieses Auto erledigt? (aus den Ablagen gelesen) ───────

def result_of(results: Path, checkin: str) -> dict | None:
    f = results / f"{checkin}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None                                   # kaputt → Phase neu machen


def phase_done(phase: str, car: dict, run_dir: Path, results: Path,
               strategy: str) -> bool:
    r = result_of(results, car["checkin"])
    if phase == "focalx":
        return bool(r) and (r.get("skipped") or "findings" in r)
    if r is None:
        return False                    # ohne FocalX-Ergebnis nichts erledigt
    if r.get("skipped"):
        return True                     # Auto ohne Fotos/Schaden → nichts zu tun
    if phase == "cluster":
        return bool(r.get("finding_clusters")) or not r.get("findings")
    if phase == "truths":
        return bool(r.get("gt_clusters")) or not r.get("truths")
    if phase == "pictograms":
        want = r.get("finding_clusters") or [[f["key"]] for f in r.get("findings", [])]
        if not want:
            return True
        d = run_dir / "pictograms_llm_focalx" / plate_key(car["plate"])
        return d.exists() and len(list(d.glob("*.json"))) >= len(want)
    if phase == "mapping":
        want = r.get("gt_clusters") or [[str(t["damage_id"])]
                                       for t in r.get("truths") or []]
        if not want:
            # Alle DB-Schäden des Autos sind ausgeschlossen (meist erst nach dem
            # Check-in erfasst). run_strategy schreibt dann keine Datei — das ist
            # kein Fehler, es gibt schlicht nichts zu urteilen.
            return True
        f = run_dir / "strategies" / strategy / f"{car['checkin']}.json"
        if not f.exists():
            return False
        try:
            props = json.loads(f.read_text()).get("proposals") or {}
        except Exception:
            return False
        judged = sum(1 for p in props.values()
                     if p.get("via") in ("ai", "ai_rejected"))
        return judged >= len(want)
    return False


# ── Ein Auto durch eine Phase (je Unterprozess, isoliert) ───────────────────

def py_for(phase: str) -> str:
    """Interpreter dieser Phase. EINE Quelle, damit die Vorflugkontrolle genau
    das prüft, was später wirklich läuft — sonst ist sie grün und der Lauf
    trotzdem blind (s. PIL_PHASES)."""
    if phase in PIL_PHASES and VENV_PY.exists():
        return str(VENV_PY)
    return PY


def command_for(phase: str, car: dict, opt: Opt) -> list[str]:
    ck, plate, rid = car["checkin"], car["plate"], opt.run
    py = py_for(phase)
    if phase == "focalx":
        return [py, "-u", "-m", "eval.pipeline", "--run", rid,
                "--inspect-only", "--no-pictograms", ck]
    if phase == "cluster":
        return [py, "-u", "scripts/cluster_findings.py", "--run", rid, ck]
    if phase == "truths":
        return [py, "-u", "scripts/cluster_truths.py", "--run", rid, ck]
    if phase == "pictograms":
        return [py, "-u", "scripts/locate_pictograms.py", "--source", "focalx",
                "--run", rid, plate]
    if phase == "mapping":
        return [py, "-u", "scripts/run_strategy.py", opt.strategy, "--run", rid,
                "--all-cars", ck]
    raise ValueError(phase)


def run_car(phase: str, car: dict, opt: Opt, logs: Path) -> tuple[bool, float, str]:
    cmd = command_for(phase, car, opt)
    log = logs / phase / f"{car['checkin']}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    limit = 60 * (opt.timeout_min or PHASE_TIMEOUT_MIN.get(phase, 60))
    t0 = time.monotonic()
    try:
        with log.open("a") as fh:
            fh.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} "
                     f"{' '.join(cmd)}\n")
            fh.flush()
            try:
                p = subprocess.run(cmd, cwd=str(ROOT), stdout=fh,
                                   stderr=subprocess.STDOUT, timeout=limit)
            except subprocess.TimeoutExpired:
                # Der Unterprozess ist beim Timeout schon getötet. Was er bis
                # dahin geschrieben hat, bleibt gültig — der nächste Lauf setzt
                # dort auf.
                fh.write(f"=== ABGEBROCHEN nach {limit/60:.0f} min (Zeitlimit)\n")
                return False, time.monotonic() - t0, f"Zeitlimit {limit/60:.0f} min"
        secs = time.monotonic() - t0
        if p.returncode != 0:
            return False, secs, f"exit {p.returncode} (siehe {log.name})"
        return True, secs, ""
    except Exception as e:
        return False, time.monotonic() - t0, str(e)


def progress_note(phase: str, car: dict, run_dir: Path, results: Path,
                  strategy: str) -> str:
    """Wie weit ist eine unfertige Phase? Zahlen statt Rätselraten im Log."""
    r = result_of(results, car["checkin"]) or {}
    if phase == "pictograms":
        want = r.get("finding_clusters") or [[f["key"]] for f in r.get("findings", [])]
        d = run_dir / "pictograms_llm_focalx" / plate_key(car["plate"])
        have = len(list(d.glob("*.json"))) if d.exists() else 0
        return f"{have}/{len(want)} Schäden lokalisiert"
    if phase == "mapping":
        f = run_dir / "strategies" / strategy / f"{car['checkin']}.json"
        props = {}
        if f.exists():
            try:
                props = json.loads(f.read_text()).get("proposals") or {}
            except Exception:
                pass
        want = r.get("gt_clusters") or [[str(t["damage_id"])]
                                       for t in r.get("truths") or []]
        have = sum(1 for p in props.values() if p.get("via") in ("ai", "ai_rejected"))
        return f"{have}/{len(want)} Urteile"
    return "kein Ergebnis geschrieben"


def prereq_of(phase: str) -> str | None:
    """Was eine Phase WIRKLICH braucht — keine starre Kette.

    Piktogramme und Mapping sind zwei unabhängige Zweige auf denselben
    Findings-Clustern: das Mapping vergleicht sie mit den SHARK-Wahrheiten, die
    Piktogramm-Phase zeichnet sie an. Als Kette gedacht müsste das Mapping auf
    tausende Piktogramm-Aufrufe warten, obwohl es keine davon liest."""
    return PREREQ.get(phase)


def split_cars(phase: str, cars: list[dict], opt: Opt, man: Manifest,
               run_dir: Path, results: Path) -> tuple[list[dict], int, int, int]:
    """→ (zu tun, fertig, wartet auf Vorstufe, Quarantäne)"""
    todo, done, waiting, quar = [], 0, 0, 0
    pre = prereq_of(phase)
    for car in cars:
        if phase_done(phase, car, run_dir, results, opt.strategy):
            done += 1
        elif pre and not phase_done(pre, car, run_dir, results, opt.strategy):
            waiting += 1
        elif man.blocked(car["checkin"], phase, opt.max_attempts):
            quar += 1
        else:
            todo.append(car)
    return todo, done, waiting, quar


def do_phase(phase: str, cars: list[dict], opt: Opt, man: Manifest,
             run_dir: Path, results: Path) -> None:
    todo, done, waiting, skipped = split_cars(phase, cars, opt, man, run_dir, results)
    if opt.limit:
        todo = todo[:opt.limit]
    print(f"\n━━ Phase {phase}: {len(todo)} offen · {done} fertig"
          + (f" · {waiting} warten auf {prereq_of(phase)}" if waiting else "")
          + (f" · {skipped} in Quarantäne" if skipped else ""), flush=True)
    if opt.dry_run or not todo:
        return

    logs = run_dir / "logs"
    n_ok = n_bad = 0
    t_phase = time.monotonic()

    def one(car: dict) -> tuple[dict, bool, float, str]:
        ok, secs, note = run_car(phase, car, opt, logs)
        # Die Ablage entscheidet, nicht der Rückgabewert: ein Skript kann 0
        # liefern und trotzdem unvollständig sein (KI-Fehler bei einzelnen
        # Schäden). Solche Teilergebnisse bleiben liegen, der nächste Lauf holt
        # nur die Lücken nach.
        really = phase_done(phase, car, run_dir, results, opt.strategy)
        if not note and not really:
            note = f"unvollständig ({progress_note(phase, car, run_dir, results, opt.strategy)})"
        return car, (ok and really), secs, note

    workers = (min(opt.workers, PHASE_MAX_WORKERS.get(phase, MAX_WORKERS))
               if phase in WORKER_PHASES else 1)
    if workers > 1:
        with cf.ThreadPoolExecutor(workers) as ex:
            futs = {ex.submit(one, c): c for c in todo}
            for fut in cf.as_completed(futs):
                car, ok, secs, note = fut.result()
                man.record(car["checkin"], phase, ok, secs, note)
                n_ok += ok
                n_bad += not ok
                print(f"  {'✓' if ok else '✗'} {car['plate']:11} {hhmm(secs):>7}"
                      f"{'  ' + note if note else ''}", flush=True)
                if _stop:
                    break
    else:
        for i, car in enumerate(todo, 1):
            car, ok, secs, note = one(car)
            man.record(car["checkin"], phase, ok, secs, note)
            n_ok += ok
            n_bad += not ok
            eta = ((time.monotonic() - t_phase) / i) * (len(todo) - i)
            print(f"  [{i}/{len(todo)}] {'✓' if ok else '✗'} {car['plate']:11} "
                  f"{hhmm(secs):>7} · Rest ~{hhmm(eta)}"
                  f"{'  ' + note if note else ''}", flush=True)
            if _stop:
                break
    print(f"  Phase {phase} fertig: {n_ok} ok · {n_bad} Fehler · "
          f"{hhmm(time.monotonic() - t_phase)}", flush=True)


# ── Lynx-Phase mit Token-Warteschleife ─────────────────────────────────────

def token_fresh() -> bool:
    return (TOKEN.exists()
            and (time.time() - TOKEN.stat().st_mtime) < TOKEN_TTL_S)


def wait_for_token(opt: Opt, why: str) -> bool:
    """Blockiert, bis eine frische .lynx_token daliegt. True = kann weitergehen."""
    if token_fresh():
        return True
    print(f"\n🔑 Frischer Lynx-Token nötig ({why}).\n"
          f"   Token in {TOKEN.relative_to(ROOT)} ablegen — ich mache dann von "
          f"selbst weiter. (Strg-C beendet sauber.)", flush=True)
    t0 = time.time()
    while not _stop:
        if token_fresh():
            print("   Token erkannt, weiter.", flush=True)
            return True
        if opt.token_timeout and (time.time() - t0) > opt.token_timeout:
            print("   Zeitlimit für den Token erreicht — Phase abgebrochen.",
                  flush=True)
            return False
        time.sleep(5)
    return False


def lynx(cmd: list[str], logs: Path, name: str) -> bool:
    log = logs / "fetch" / f"{name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as fh:
        fh.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} "
                 f"{' '.join(cmd)}\n")
        fh.flush()
        return subprocess.run(cmd, cwd=str(ROOT), stdout=fh,
                              stderr=subprocess.STDOUT).returncode == 0


def day_photos_complete(branch: str, day: str) -> bool:
    """Liegt der Abruf dieses Branch-Tages schon samt aller Fotos auf Platte?

    Dann ist kein Token nötig. Wichtig für Tage, die vor dem Stapel von Hand
    geholt wurden — die stehen in keinem Manifest."""
    f = ROOT / "data" / "lynx_raw" / f"{branch}_{day}.json"
    if not f.exists():
        return False
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from download_photos import jobs_from_file
        jobs = list(jobs_from_file(f))
    except Exception:
        return False
    # Über die ANZAHL je Check-in prüfen, nicht über die Dateinamen: die Ordner
    # der ersten Runs wurden auf die custom_-Namen umbenannt, ein Namensabgleich
    # hielte sie fälschlich für unvollständig.
    want: dict[Path, int] = {}
    for dest, _url in jobs:
        want[dest.parent] = want.get(dest.parent, 0) + 1
    return bool(want) and all(
        d.exists() and len(list(d.glob("*.jpg"))) >= n for d, n in want.items())


def do_fetch(opt: Opt, man: Manifest, run_dir: Path) -> None:
    days = parse_days(opt.days)
    if not days and not opt.gt_only:
        sys.exit("fetch braucht --days, z. B. --days 2026-07-17..2026-07-20")
    logs = run_dir / "logs"
    if opt.gt_only:
        print("\n━━ Phase fetch (--gt-only): SHARK-Wahrheiten + GT-Fotos",
              flush=True)
        days = []
    else:
        print(f"\n━━ Phase fetch: {len(days)} Tag(e), Branch {opt.branch}", flush=True)

    # Erst ALLE Tage abrufen, dann die Fotos laden. Ein Tagesabruf dauert
    # Sekunden, der Foto-Download Minuten — in einem Token-Fenster von 5 min
    # passen so alle Abrufe statt nur zwei. Die presigned URLs (~15 min) halten
    # das aus, weil die Downloads unmittelbar folgen und keinen Token brauchen.
    fetched: list[str] = []
    for day in days:
        if _stop:
            break
        # Schlüssel MIT Branch: zwei Filialen am selben Tag sind zwei Abrufe.
        day_key = f"{opt.branch}/{day}"
        man.mark_day(day_key)
        st = man.data["days"].setdefault(day_key, {})
        if st.get("status") == "done":
            print(f"  {day}: schon geholt", flush=True)
            continue
        if day_photos_complete(opt.branch, day):
            st.update({"status": "done", "seconds": 0,
                       "at": datetime.now().isoformat(timespec="seconds"),
                       "note": "war schon vollständig"})
            man.save()
            print(f"  ✓ {day}: Fotos schon vollständig — kein Token nötig", flush=True)
            continue
        if not wait_for_token(opt, f"Check-ins {opt.branch}/{day}"):
            return
        t0 = time.monotonic()
        tag = f"{opt.branch}_{day}"
        ok = lynx([PY, "-u", "scripts/lynx_fetch.py", opt.branch, day], logs, tag)
        st.update({"status": "fetched" if ok else "failed",
                   "seconds": round(time.monotonic() - t0, 1),
                   "at": datetime.now().isoformat(timespec="seconds")})
        man.save()
        print(f"  {'✓' if ok else '✗'} Abruf {day} · {hhmm(st['seconds'])}", flush=True)
        if ok:
            fetched.append(day)

    for day in fetched:
        if _stop:
            break
        tag = f"{opt.branch}_{day}"
        man.mark_day(f"{opt.branch}/{day}")
        st = man.data["days"][f"{opt.branch}/{day}"]
        t0 = time.monotonic()
        ok = lynx([PY, "-u", "scripts/download_photos.py", tag], logs, tag)
        st["status"] = "done" if ok else "failed"
        st["seconds"] = round(st.get("seconds", 0) + time.monotonic() - t0, 1)
        man.save()
        n = len(list((RAW / day).iterdir())) if (RAW / day).exists() else 0
        print(f"  {'✓' if ok else '✗'} Fotos {day} · {hhmm(st['seconds'])} · "
              f"{n} Check-ins an diesem Tag", flush=True)

    # SHARK-Wahrheiten je Kennzeichen — einzeln, damit die Token-Wartezeit
    # zwischen zwei Kennzeichen liegen kann und nichts halb Geholtes entsteht.
    # Hier NICHT nach Schaden filtern: ob ein Auto Schäden hat, weiß man erst
    # nach diesem Abruf.
    if opt.days_only:
        print("  (--days-only: SHARK-Wahrheiten und GT-Fotos später)", flush=True)
        return

    want = set(days) if days else None
    plates = sorted({c.name.split("__")[0]
                     for d in RAW.iterdir() if d.is_dir()
                     and (want is None or d.name in want)
                     for c in d.iterdir() if c.is_dir()}
                    - {p.stem.upper() for p in GT_DIR.glob("*.json")})
    plates = [p for p in plates if not gt_file(p)]
    if plates:
        print(f"  SHARK-Wahrheiten fehlen für {len(plates)} Kennzeichen", flush=True)
    for i, plate in enumerate(plates, 1):
        if _stop:
            break
        if not wait_for_token(opt, f"GT {plate}"):
            return
        ok = lynx([PY, "-u", "scripts/fetch_damages.py", plate], logs, "gt")
        print(f"  [{i}/{len(plates)}] {'✓' if ok else '✗'} GT {plate}", flush=True)

    # GT-Fotos: presigned ~30 min, deshalb direkt hinterher. --refetch holt
    # abgelaufene URLs nach, braucht also wieder einen frischen Token.
    # Das Skript wartet selbst auf frische Token und löst jede URL sofort ein,
    # deshalb hier nur anstoßen und das Ergebnis melden.
    if not _stop and wait_for_token(opt, "GT-Fotos"):
        ok = lynx([str(VENV_PY) if VENV_PY.exists() else PY, "-u",
                   "scripts/download_gt_photos.py", "--refetch"], logs, "gt_photos")
        print(f"  {'✓' if ok else '✗'} GT-Fotos "
              f"{'geladen' if ok else 'unvollständig'} (Details im Log)", flush=True)


# ── Vorflugkontrolle ────────────────────────────────────────────────────────

def _check(name: str, fn) -> bool:
    """Eine Prüfung ausführen und einzeilig berichten."""
    try:
        ok, info = fn()
    except Exception as e:
        ok, info = False, f"{type(e).__name__}: {e}"
    print(f"  {'✓' if ok else '✗'} {name:22} {info}", flush=True)
    return ok


def do_preflight(opt: Opt, results: Path) -> bool:
    """Alles prüfen, was einen mehrtägigen Lauf still scheitern lassen kann.

    Lieber jetzt zwei Minuten kosten, als nach acht Stunden feststellen, dass
    die FocalX-Anmeldung abgelaufen ist oder Pillow im venv fehlt."""
    print("\n━━ Vorflugkontrolle", flush=True)
    from eval.pipeline import _env

    def creds():
        missing = [k for k in ("FOCALX_PRECISE_USERNAME", "FOCALX_PRECISE_PASSWORD",
                               "LLM_GW_API_KEY") if not _env(k)]
        return not missing, "in .env vollständig" if not missing else \
            f"fehlt: {', '.join(missing)}"

    def focalx():
        from eval.focalx import FocalxClient
        c = FocalxClient(_env("FOCALX_PRECISE_USERNAME"),
                         _env("FOCALX_PRECISE_PASSWORD"))
        labels = c.valid_labels()
        return bool(labels), f"Anmeldung ok · {len(labels)} Positionslabels"

    def gateway():
        from eval.judge import MODEL, _post_with_retry, _text
        body = json.dumps({"model": MODEL, "max_tokens": 2000, "temperature": 0,
                           "messages": [{"role": "user",
                                         "content": [_text("Antworte nur: OK")]}]})
        out = _post_with_retry(_env("LLM_GW_API_KEY"), body, attempts=2)
        return bool(out), f"{MODEL} antwortet" if out else "keine Antwort"

    def pictograms():
        if not VENV_PY.exists():
            return False, ".venv/bin/python fehlt (Pillow nötig)"
        code = ("import sys; sys.path[:0]=['.', 'scripts'];"
                "import locate_pictograms as L;"
                "t=L._grid_template('DRIVER_SIDE', None);"
                "print('ok' if t else 'keine Vorlage')")
        p = subprocess.run([str(VENV_PY), "-c", code], cwd=str(ROOT),
                           capture_output=True, text=True)
        out = (p.stdout or p.stderr).strip().splitlines()[-1:] or [""]
        return out[0] == "ok", f"Vorlagen + Pillow im venv: {out[0]}"

    def pillow_je_phase():
        """Jede Pillow-Phase mit IHREM Interpreter prüfen.

        Die Kontrolle oben prüft das venv — der Judge lief im Pilotlauf aber mit
        sys.executable. Grüner Haken, blindes Mapping. Hier zählt nur, was
        py_for() tatsächlich startet."""
        bad = []
        for phase in sorted(PIL_PHASES):
            py = py_for(phase)
            p = subprocess.run([py, "-c", "import PIL"], cwd=str(ROOT),
                               capture_output=True, text=True)
            if p.returncode != 0:
                bad.append(f"{phase} → {Path(py).name} ohne Pillow")
        return not bad, ("; ".join(bad) if bad else
                         f"Pillow in allen {len(PIL_PHASES)} Bildphasen")

    def strategy():
        p = ROOT / "strategies" / opt.strategy / "prompt.txt"
        return p.exists(), f"{opt.strategy}: {'Prompt vorhanden' if p.exists() else 'FEHLT'}"

    def storage():
        st = os.statvfs(ROOT)
        free = st.f_bavail * st.f_frsize / 1e9
        return free > 20, f"{free:.0f} GB frei"

    def data():
        n = sum(1 for d in RAW.iterdir() if d.is_dir()) if RAW.exists() else 0
        g = len(list(GT_DIR.glob("*.json"))) if GT_DIR.exists() else 0
        return bool(n and g), f"{n} Check-in-Tag(e) · {g} SHARK-Antworten"

    checks = [("Zugangsdaten", creds), ("FocalX-API", focalx),
              ("LLM-Gateway", gateway), ("Piktogramme", pictograms),
              ("Pillow je Phase", pillow_je_phase),
              ("Strategie", strategy), ("Rohdaten", data),
              ("Plattenplatz", storage)]
    ok = all(_check(n, f) for n, f in checks)
    print(f"  → {'alles bereit' if ok else 'NICHT bereit — oben stehen die Gründe'}",
          flush=True)
    return ok


# ── Ergebnisse eines anderen Runs übernehmen ───────────────────────────────

def do_seed(opt: Opt, run_dir: Path, results: Path) -> None:
    """Fertige FocalX-Ergebnisse aus einem anderen Run übernehmen.

    Sinnvoll nur bei IDENTISCHER Detection-Konfiguration (gleiche Positions-
    labels, gleiches FocalX-Modell) — die Finding-Keys wandern ja mit. Erspart
    das erneute Analysieren schon gerechneter Autos. Vorhandenes wird nie
    überschrieben. Strategie-Vorschläge kommen bewusst NICHT mit: deren
    Arbeitsliste kann aus dem Gold-Standard stammen und dann nicht zu den hier
    gerechneten `gt_clusters` passen."""
    import shutil
    src = runs_mod.results_dir(opt.from_run)
    if not opt.from_run or not src.exists():
        sys.exit("seed braucht --from <run-id> mit vorhandenen Ergebnissen.")
    if src.resolve() == results.resolve():
        sys.exit("Quelle und Ziel sind derselbe Run.")
    src_pic = src.parent / "pictograms_llm_focalx"
    dst_pic = run_dir / "pictograms_llm_focalx"
    print(f"\n━━ Phase seed: {src.relative_to(ROOT)} → {results.relative_to(ROOT)}",
          flush=True)
    n_res = n_pic = n_skip = 0
    for f in sorted(src.glob("*.json")):
        dst = results / f.name
        if dst.exists():
            n_skip += 1
            continue
        try:
            d = json.loads(f.read_text())
        except Exception as e:
            print(f"  ✗ {f.stem}: unlesbar ({e})", flush=True)
            continue
        write_atomic(dst, d)                       # bewusst über tmp+replace
        n_res += 1
        pk = plate_key(d.get("plate", ""))
        if pk and (src_pic / pk).exists() and not (dst_pic / pk).exists():
            shutil.copytree(src_pic / pk, dst_pic / pk)
            n_pic += 1
    print(f"  {n_res} Ergebnis(se) und {n_pic} Piktogramm-Ordner übernommen"
          f" · {n_skip} waren schon da", flush=True)


# ── Bericht ─────────────────────────────────────────────────────────────────

def do_status(cars: list[dict], opt: Opt, man: Manifest, run_dir: Path,
              results: Path) -> None:
    print(f"\nRun {opt.run} · {len(cars)} Autos in der Grundgesamtheit"
          + (" (nur mit Exterior-Schaden)" if opt.only_damaged else ""))
    print(f"{'Phase':12} {'fertig':>7} {'offen':>6} {'wartet':>7} {'Quar.':>6} "
          f"{'Ø/Auto':>8} {'Summe':>8} {'Rest':>8}")
    for phase in PHASES[1:]:
        todo, done, waiting, quar = split_cars(phase, cars, opt, man, run_dir,
                                               results)
        secs = [man.phase(c["checkin"], phase).get("seconds") or 0 for c in cars
                if man.phase(c["checkin"], phase).get("status") == "done"]
        avg = sum(secs) / len(secs) if secs else 0
        offen = len(todo) + waiting
        print(f"{phase:12} {done:7} {len(todo):6} {waiting:7} {quar:6} "
              f"{hhmm(avg) if secs else '—':>8} "
              f"{hhmm(sum(secs)) if secs else '—':>8} "
              f"{hhmm(avg * offen) if avg and offen else '—':>8}")
    bad = [(c["plate"], p, man.phase(c["checkin"], p).get("note", ""))
           for c in cars for p in PHASES[1:]
           if man.blocked(c["checkin"], p, opt.max_attempts)]
    if bad:
        print(f"\nQuarantäne ({len(bad)}) — mit --retry-failed erneut versuchen:")
        for plate, phase, note in bad[:15]:
            print(f"  {plate:11} {phase:11} {note}")


# ── Hauptteil ───────────────────────────────────────────────────────────────

def main() -> None:
    opt = Opt(sys.argv[1:])
    if not opt.run:
        sys.exit(__doc__)
    if opt.run == "v1":
        sys.exit("v1 ist der eingefrorene Original-Run — dort schreibt der "
                 "Stapel nicht. Neuen Run wählen, z. B. --run fl500.")
    phases = opt.phases or ["status"]
    if "all" in phases:
        phases = [p for p in PHASES if p != "fetch"]
    extra = ["status", "all", "seed", "preflight"]
    unknown = [p for p in phases if p not in PHASES + extra]
    if unknown:
        sys.exit(f"Unbekannte Phase(n): {', '.join(unknown)}. "
                 f"Erlaubt: {', '.join(PHASES)}, {', '.join(extra)}")

    # Label nur beim Anlegen setzen — ein bestehender Run behält seine Beschriftung.
    known = runs_mod.get_run(opt.run) is not None
    runs_mod.ensure_run(opt.run, None if known else f"{opt.run} — Stapellauf")
    results = runs_mod.results_dir(opt.run)
    run_dir = results.parent
    man = Manifest(run_dir / "manifest.json")
    man.data["run"] = opt.run
    man.data.setdefault("created", datetime.now().isoformat(timespec="seconds"))

    if opt.retry_failed:
        n = man.clear_failures()
        print(f"Quarantäne geleert: {n} Phase(n) werden erneut versucht.")

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    # tmux kill-session schickt SIGHUP — auch das soll sauber beenden statt
    # mitten im Schreiben abzusterben.
    signal.signal(signal.SIGHUP, _on_signal)

    if phases == ["preflight"]:
        sys.exit(0 if do_preflight(opt, results) else 1)

    cars = discover_cars(opt)
    if not cars and phases != ["fetch"]:
        print("Keine Autos in data/raw für diese Auswahl — erst `fetch`.")
    if phases == ["status"]:
        do_status(cars, opt, man, run_dir, results)
        return

    # Vor einer langen Kette lieber jetzt scheitern als nach acht Stunden.
    if len([p for p in phases if p in PHASES]) > 1 and "--skip-preflight" not in sys.argv:
        if not do_preflight(opt, results):
            sys.exit("Abbruch vor dem Stapel — erst die Vorflugkontrolle klären "
                     "(oder mit --skip-preflight erzwingen).")

    locks = run_dir / "locks"
    held: list[Path] = []
    t0 = time.monotonic()
    try:
        for phase in phases:
            if _stop:
                break
            if phase == "status":
                do_status(cars, opt, man, run_dir, results)
                continue
            lock = locks / f"{phase}.lock"
            if not acquire_lock(lock):
                continue
            held.append(lock)
            try:
                if phase == "seed":
                    do_seed(opt, run_dir, results)
                elif phase == "fetch":
                    do_fetch(opt, man, run_dir)
                    cars = discover_cars(opt)    # neue Autos einsammeln
                else:
                    do_phase(phase, cars, opt, man, run_dir, results)
            finally:
                lock.unlink(missing_ok=True)
                held.remove(lock)
    finally:
        for lock in held:
            lock.unlink(missing_ok=True)
        man.save()
    print(f"\n═══ Stapel {'abgebrochen' if _stop else 'durch'} nach "
          f"{hhmm(time.monotonic() - t0)}", flush=True)
    do_status(cars, opt, man, run_dir, results)


if __name__ == "__main__":
    main()
