"""Deterministic matcher: FocalX findings ↔ ground-truth damages (DB records).

Port of the app's FindingMatcher, adapted for the SHARK damage-case model.
Signals per (finding, truth) pair:

  +3 same vehicle side (finding position label → side; truth projection → side)
  +2 adjacent corner (a rear-corner photo can show a side-pinned damage)
  +3 same damage type (normalised vocabulary)
  +2 same part class (bumper, door, windscreen, rim, …)
  +1 same front/rear qualifier of the part

Assignment: greedy best-score, each truth damage used once, MIN_SCORE gate.
Everything below the gate goes to the LLM judge (judge.py) as "ambiguous".
"""
from __future__ import annotations

from dataclasses import dataclass

MIN_SCORE = 5

# ── vocabulary ──────────────────────────────────────────────────────────────

PART_CLASSES: list[tuple[str, list[str]]] = [
    ("bumper", ["bumper", "loading strip"]),
    ("door", ["door"]),
    ("windscreen", ["windscreen", "windshield", "window", "glass"]),
    ("rim", ["rim", "wheel", "tyre", "tire", "curbrash"]),
    ("fender", ["fender", "wing", "quarter"]),
    ("bonnet", ["bonnet", "hood"]),
    ("mirror", ["mirror"]),
    ("sill", ["sill", "rocker", "running-board", "running board"]),
    ("light", ["light", "lamp"]),
    ("roof", ["roof"]),
    ("trunk", ["trunk", "tailgate", "boot"]),
    ("pillar", ["pillar"]),
    ("moulding", ["moulding", "molding", "trim"]),
]

TYPE_SYNONYMS = {
    "scratch": "scratch", "scratched": "scratch", "scuffed": "scratch", "scuff": "scratch",
    "stone_chip": "stone_chip", "stone chip": "stone_chip", "chip": "stone_chip",
    "dent": "dent", "dented": "dent",
    "crack": "crack", "cracked": "crack",
    "curbrash": "curbrash", "curb rash": "curbrash",
    "misaligned": "misaligned",
    "missing": "missing",
}

# position label → (side, zone). Sides: front/rear/left/right (left = driver, LHD).
LABEL_SIDE_ZONE = {
    "front": ("front", None), "afront": ("front", None), "b-front": ("front", None),
    "c-front": ("front", None),
    "front-left": ("left", "front"), "afront-left": ("left", "front"),
    "left-front": ("left", "front"), "aleft-front": ("left", "front"),
    "afront-left-wheel": ("left", "front"),
    "aleft-rear": ("left", "rear"), "left-rear": ("left", "rear"),
    "arear-left-wheel": ("left", "rear"), "rear-left": ("rear", "left"),
    "rear": ("rear", None), "arear": ("rear", None),
    "rear-right": ("rear", "right"), "right-rear": ("right", "rear"),
    "abcright-rear": ("right", "rear"), "arear-right-wheel": ("right", "rear"),
    "aright-front": ("right", "front"), "right-front": ("right", "front"),
    "afront-right-wheel": ("right", "front"), "front-right": ("right", "front"),
}

PROJECTION_SIDE = {
    "FRONT_SIDE": "front",
    "BACK_SIDE": "rear",
    "DRIVER_SIDE": "left",     # LHD
    "PASSENGER_SIDE": "right",
}


@dataclass
class Truth:
    """One ground-truth damage from the DB (SHARK damage-case damage)."""
    damage_id: str
    part: str
    damage_type: str          # localized_values.type, e.g. "scratch"
    side_attr: str            # localized_values.side, e.g. "Passenger side"
    projection: str           # coordinates[0].projection
    segment: str              # coordinates[0].segment
    severity: str | None = None
    case_number: str | None = None


@dataclass
class MatchResult:
    matched: list[tuple[str, str, int]]      # (finding_key, damage_id, score)
    ambiguous: list[tuple[str, str, int]]    # sub-threshold but nearby pairs → judge
    missed: list[str]                        # truth damage_ids with no finding
    extra: list[str]                         # finding_keys with no truth match


def norm_type(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    return TYPE_SYNONYMS.get(s, "other")


def part_class(raw: str | None) -> str | None:
    s = (raw or "").lower().replace("-", " ")
    for cls, keys in PART_CLASSES:
        if any(k in s for k in keys):
            return cls
    return None


def front_rear(raw: str | None) -> str | None:
    s = (raw or "").lower()
    if "front" in s:
        return "front"
    if "rear" in s or "back" in s:
        return "rear"
    return None


def finding_side_zone(position: str) -> tuple[str | None, str | None]:
    return LABEL_SIDE_ZONE.get(position, (None, None))


def truth_side(t: Truth) -> str | None:
    return PROJECTION_SIDE.get((t.projection or "").upper())


def nearness(f_side: str | None, f_zone: str | None, t_side: str | None) -> int:
    if f_side is None or t_side is None:
        return 1  # unknown geometry — keep it in play, weakly
    if f_side == t_side:
        return 3
    # adjacent corner: front/rear photo ↔ side-pinned truth and vice versa
    if f_side in ("front", "rear") and t_side in ("left", "right"):
        return 2
    if f_side in ("left", "right") and f_zone in ("front", "rear") and t_side == f_zone:
        return 2
    return 0


def score(finding_position: str, finding_part: str | None, finding_type: str,
          truth: Truth) -> int:
    f_side, f_zone = finding_side_zone(finding_position)
    s = nearness(f_side, f_zone, truth_side(truth))
    if s == 0:
        return 0
    if norm_type(finding_type) == norm_type(truth.damage_type):
        s += 3
    fc, tc = part_class(finding_part), part_class(truth.part)
    if fc is not None and fc == tc:
        s += 2
        fr = front_rear(finding_part)
        if fr is not None and fr == front_rear(truth.part):
            s += 1
    return s


def match(findings: list[tuple[str, str, str | None, str]],
          truths: list[Truth]) -> MatchResult:
    """`findings` = (key, position_label, part, damage_type) per finding."""
    pairs = []
    for key, pos, part, dtype in findings:
        for t in truths:
            s = score(pos, part, dtype, t)
            if s > 0:
                pairs.append((key, t.damage_id, s))
    pairs.sort(key=lambda p: -p[2])

    matched, used_f, used_t = [], set(), set()
    for key, tid, s in pairs:
        if s < MIN_SCORE or key in used_f or tid in used_t:
            continue
        matched.append((key, tid, s))
        used_f.add(key)
        used_t.add(tid)

    ambiguous = [(k, t, s) for k, t, s in pairs
                 if 0 < s < MIN_SCORE and k not in used_f and t not in used_t]
    missed = [t.damage_id for t in truths if t.damage_id not in used_t]
    extra = [k for k, *_ in findings if k not in used_f]
    return MatchResult(matched, ambiguous, missed, extra)
