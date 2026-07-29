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

RAW = ROOT / "data" / "raw"
GT_DIR = ROOT / "data" / "ground_truth"
TOKEN = ROOT / ".lynx_token"
TOKEN_TTL_S = 240          # der Token lebt ~5 min — mit Sicherheitsabstand
PY = sys.executable or "python3"
VENV_PY = ROOT / ".venv" / "bin" / "python"      # Piktogramme brauchen Pillow
PHASES = ["fetch", "focalx", "cluster", "truths", "pictograms", "mapping"]
WORKER_PHASES = {"pictograms", "mapping"}        # nur diese parallelisieren
MAX_WORKERS = 3                                  # Rate-Limit des Gateways

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
               "--workers", "--max-attempts", "--token-timeout", "--from"]


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
        self.workers = min(int(self._val(argv, "--workers", "1")), MAX_WORKERS)
        self.max_attempts = int(self._val(argv, "--max-attempts", "3"))
        self.token_timeout = int(self._val(argv, "--token-timeout", "0"))
        # Vorgabe: nur Autos, die in SHARK einen Exterior-Schaden haben — bei
        # schadenfreien Autos gibt es nichts zu mappen. --include-clean nimmt alle.
        self.only_damaged = "--include-clean" not in argv
        self.retry_failed = "--retry-failed" in argv
        self.dry_run = "--dry-run" in argv

    @staticmethod
    def _val(argv: list[str], flag: str, default: str) -> str:
        return argv[argv.index(flag) + 1] if flag in argv else default


# ── Manifest (Buchhaltung: Versuche, Zeiten, Quarantäne) ────────────────────

class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.data = (json.loads(path.read_text()) if path.exists()
                     else {"cars": {}, "days": {}})
        self.data.setdefault("cars", {})
        self.data.setdefault("days", {})

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
        self.save()

    def blocked(self, checkin: str, phase: str, max_attempts: int) -> bool:
        p = self.phase(checkin, phase)
        return (p.get("status") == "failed"
                and int(p.get("attempts", 0)) >= max_attempts)

    def clear_failures(self) -> int:
        n = 0
        for c in self.data["cars"].values():
            for p in c.get("phases", {}).values():
                if p.get("status") == "failed":
                    p["attempts"] = 0
                    p.pop("status", None)
                    n += 1
        self.save()
        return n

    def save(self) -> None:
        write_atomic(self.path, self.data)


# ── Sperre: nur ein Orchestrator je Run ─────────────────────────────────────

def acquire_lock(path: Path) -> None:
    if path.exists():
        try:
            pid = int(json.loads(path.read_text()).get("pid", 0))
        except Exception:
            pid = 0
        if pid:
            alive = True
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False                         # es gibt den Prozess nicht mehr
            except PermissionError:
                alive = True                          # existiert, gehört nur anderen
            if alive:
                sys.exit(f"Es läuft schon ein Stapel für diesen Run (PID {pid}). "
                         f"Erst beenden oder {path.relative_to(ROOT)} löschen.")
    write_atomic(path, {"pid": os.getpid(),
                        "since": datetime.now().isoformat(timespec="seconds")})


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
        f = run_dir / "strategies" / strategy / f"{car['checkin']}.json"
        if not f.exists():
            return False
        try:
            props = json.loads(f.read_text()).get("proposals") or {}
        except Exception:
            return False
        want = r.get("gt_clusters") or [[str(t["damage_id"])]
                                       for t in r.get("truths") or []]
        if not want:
            return True
        judged = sum(1 for p in props.values()
                     if p.get("via") in ("ai", "ai_rejected"))
        return judged >= len(want)
    return False


# ── Ein Auto durch eine Phase (je Unterprozess, isoliert) ───────────────────

def command_for(phase: str, car: dict, opt: Opt) -> list[str]:
    ck, plate, rid = car["checkin"], car["plate"], opt.run
    if phase == "focalx":
        return [PY, "-u", "-m", "eval.pipeline", "--run", rid,
                "--inspect-only", "--no-pictograms", ck]
    if phase == "cluster":
        return [PY, "-u", "scripts/cluster_findings.py", "--run", rid, ck]
    if phase == "truths":
        return [PY, "-u", "scripts/cluster_truths.py", "--run", rid, ck]
    if phase == "pictograms":
        py = str(VENV_PY) if VENV_PY.exists() else PY
        return [py, "-u", "scripts/locate_pictograms.py", "--source", "focalx",
                "--run", rid, plate]
    if phase == "mapping":
        return [PY, "-u", "scripts/run_strategy.py", opt.strategy, "--run", rid,
                "--all-cars", ck]
    raise ValueError(phase)


def run_car(phase: str, car: dict, opt: Opt, logs: Path) -> tuple[bool, float, str]:
    cmd = command_for(phase, car, opt)
    log = logs / phase / f"{car['checkin']}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        with log.open("a") as fh:
            fh.write(f"\n=== {datetime.now().isoformat(timespec='seconds')} "
                     f"{' '.join(cmd)}\n")
            fh.flush()
            p = subprocess.run(cmd, cwd=str(ROOT), stdout=fh,
                               stderr=subprocess.STDOUT)
        secs = time.monotonic() - t0
        if p.returncode != 0:
            return False, secs, f"exit {p.returncode} (siehe {log.name})"
        return True, secs, ""
    except Exception as e:
        return False, time.monotonic() - t0, str(e)


def prereq_of(phase: str) -> str | None:
    """Jede Phase baut auf der vorherigen auf — ohne FocalX-Ergebnis gibt es
    nichts zu clustern, ohne Cluster keine Piktogramme."""
    i = PHASES.index(phase)
    return PHASES[i - 1] if i > 1 else None


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
        # liefern und trotzdem nichts geschrieben haben (und umgekehrt).
        really = phase_done(phase, car, run_dir, results, opt.strategy)
        return car, (ok and really), secs, note or ("" if really else "nichts geschrieben")

    workers = opt.workers if phase in WORKER_PHASES else 1
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


def do_fetch(opt: Opt, man: Manifest, run_dir: Path) -> None:
    days = parse_days(opt.days)
    if not days:
        sys.exit("fetch braucht --days, z. B. --days 2026-07-17..2026-07-20")
    logs = run_dir / "logs"
    print(f"\n━━ Phase fetch: {len(days)} Tag(e), Branch {opt.branch}", flush=True)

    for day in days:
        if _stop:
            break
        st = man.data["days"].setdefault(day, {})
        if st.get("status") == "done":
            print(f"  {day}: schon geholt", flush=True)
            continue
        if not wait_for_token(opt, f"Check-ins {day}"):
            return
        t0 = time.monotonic()
        ok = lynx([PY, "-u", "scripts/lynx_fetch.py", opt.branch, day], logs, day)
        # Foto-URLs sind ~15 min gültig → sofort laden, nicht erst am Ende.
        if ok:
            ok = lynx([PY, "-u", "scripts/download_photos.py", day], logs, day)
        st.update({"status": "done" if ok else "failed",
                   "seconds": round(time.monotonic() - t0, 1),
                   "at": datetime.now().isoformat(timespec="seconds")})
        man.save()
        print(f"  {'✓' if ok else '✗'} {day} · {hhmm(st['seconds'])}", flush=True)

    # SHARK-Wahrheiten je Kennzeichen — einzeln, damit die Token-Wartezeit
    # zwischen zwei Kennzeichen liegen kann und nichts halb Geholtes entsteht.
    # Hier NICHT nach Schaden filtern: ob ein Auto Schäden hat, weiß man erst
    # nach diesem Abruf.
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
    if not _stop and wait_for_token(opt, "GT-Fotos"):
        lynx([str(VENV_PY) if VENV_PY.exists() else PY, "-u",
              "scripts/download_gt_photos.py", "--refetch"], logs, "gt_photos")
        print("  ✓ GT-Fotos geladen (Details im Log)", flush=True)


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
    unknown = [p for p in phases if p not in PHASES + ["status", "all", "seed"]]
    if unknown:
        sys.exit(f"Unbekannte Phase(n): {', '.join(unknown)}. "
                 f"Erlaubt: {', '.join(PHASES)}, seed, all, status")

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

    cars = discover_cars(opt)
    if not cars and phases != ["fetch"]:
        print("Keine Autos in data/raw für diese Auswahl — erst `fetch`.")
    if phases == ["status"]:
        do_status(cars, opt, man, run_dir, results)
        return

    acquire_lock(run_dir / "batch.lock")
    t0 = time.monotonic()
    try:
        for phase in phases:
            if _stop:
                break
            if phase == "status":
                do_status(cars, opt, man, run_dir, results)
            elif phase == "seed":
                do_seed(opt, run_dir, results)
            elif phase == "fetch":
                do_fetch(opt, man, run_dir)
                cars = discover_cars(opt)        # neue Autos einsammeln
            else:
                do_phase(phase, cars, opt, man, run_dir, results)
    finally:
        (run_dir / "batch.lock").unlink(missing_ok=True)
        man.save()
    print(f"\n═══ Stapel {'abgebrochen' if _stop else 'durch'} nach "
          f"{hhmm(time.monotonic() - t0)}", flush=True)
    do_status(cars, opt, man, run_dir, results)


if __name__ == "__main__":
    main()
