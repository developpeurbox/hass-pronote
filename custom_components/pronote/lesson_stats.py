"""Statistiques des cours (annulés, prof absent, grève, blocus...) par matière sur l'année."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from datetime import date, datetime, time, timedelta

from .pronote_formatter import format_displayed_lesson

# "Prof. absent", "Prof./pers. absent", "Absence du professeur", "Classe absente"...
_ABSENT = re.compile(r"absen[tc]", re.IGNORECASE)
_DATE = re.compile(r"(\d{1,2})/(\d{1,2})")
_TIME = re.compile(r"(\d{1,2})(?:[h:](\d{2})?)?\s*-\s*(\d{1,2})(?:[h:](\d{2})?)?")


def _plain(text: str) -> str:
    """Minuscules sans accents, pour reconnaître 'matin', 'après-midi'..."""
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------------------
# Jours de grève / blocus saisis dans les options de l'intégration
# ---------------------------------------------------------------------------


def _resolve_date(day: int, month: int, start_day: date) -> date:
    """Jour/mois -> date dans l'année scolaire qui commence à start_day."""
    year = start_day.year if (month, day) >= (start_day.month, start_day.day) else start_day.year + 1
    return date(year, month, day)


def parse_disruptions(text: str | None, start_day: date):
    """Lit le texte des jours perturbés.

    Une ligne (ou un élément séparé par ; ou ,) = une perturbation :
        04/11                       journée entière
        12/10 au 16/10              plusieurs jours
        10/11 matin 8-13            plage horaire (8h-13h)
        09/11 après-midi 13-18      plage horaire (13h-18h)
        12/11 matin                 sans heures : matin = avant 13h, après-midi = dès 13h

    Renvoie (fenêtres, lignes_ignorées). Une fenêtre = (début, fin, ligne d'origine).
    """
    windows: list[tuple[datetime, datetime, str]] = []
    ignored: list[str] = []

    for raw in re.split(r"[\n;,]", text or ""):
        line = raw.strip()
        if not line:
            continue
        try:
            dates = _DATE.findall(line)
            if not dates or len(dates) > 2:
                raise ValueError("date")
            first = _resolve_date(int(dates[0][0]), int(dates[0][1]), start_day)
            last = (
                _resolve_date(int(dates[-1][0]), int(dates[-1][1]), start_day)
                if len(dates) == 2
                else first
            )
            if last < first:
                raise ValueError("ordre des dates")

            rest = _plain(_DATE.sub(" ", line))
            hours = _TIME.search(rest)
            if hours:
                h1, m1, h2, m2 = hours.groups()
                begin = time(int(h1), int(m1 or 0))
                end = time(int(h2), int(m2 or 0)) if int(h2) < 24 else time(23, 59, 59)
            elif "apres" in rest and "midi" in rest:
                begin, end = time(13, 0), time(23, 59, 59)
            elif "matin" in rest:
                begin, end = time(0, 0), time(13, 0)
            else:
                begin, end = time(0, 0), time(23, 59, 59)
            if end <= begin:
                raise ValueError("heures")

            day = first
            while day <= last:
                windows.append(
                    (datetime.combine(day, begin), datetime.combine(day, end), line)
                )
                day += timedelta(days=1)
        except ValueError:
            ignored.append(line)

    return windows, ignored


# ---------------------------------------------------------------------------
# Statistiques
# ---------------------------------------------------------------------------


def compact_lesson(lesson) -> tuple:
    """Réduit un cours pronotepy à ce qui sert aux statistiques (léger à mettre en cache)."""
    status = (lesson.status or "").strip()
    absent = bool(_ABSENT.search(status))
    class_absent = absent and "classe" in status.lower()
    teacher_absent = absent and not class_absent
    return (
        lesson.id,
        lesson.start,
        lesson.end,
        format_displayed_lesson(lesson),
        bool(lesson.canceled),
        status,
        teacher_absent,
        class_absent,
    )


def _empty() -> dict:
    return {
        "cours": 0,
        "tenus": 0,
        "annules": 0,
        "prof_absent": 0,
        "classe_absente": 0,
        "greve_blocus": 0,
        "perdus": 0,
        "heures": 0.0,
        "heures_tenues": 0.0,
        "heures_annulees": 0.0,
        "heures_prof_absent": 0.0,
        "heures_classe_absente": 0.0,
        "heures_greve_blocus": 0.0,
        "heures_perdues": 0.0,
    }


def _add(bucket, hours, canceled, teacher_absent, class_absent, blocked) -> None:
    lost = canceled or teacher_absent or class_absent or blocked
    bucket["cours"] += 1
    bucket["heures"] += hours
    for key, flag in (
        ("annules", canceled),
        ("prof_absent", teacher_absent),
        ("classe_absente", class_absent),
        ("greve_blocus", blocked),
    ):
        if flag:
            bucket[key] += 1
    for key, flag in (
        ("annulees", canceled),
        ("prof_absent", teacher_absent),
        ("classe_absente", class_absent),
        ("greve_blocus", blocked),
    ):
        if flag:
            bucket[f"heures_{key}"] += hours
    if lost:
        bucket["perdus"] += 1
        bucket["heures_perdues"] += hours
    else:
        bucket["tenus"] += 1
        bucket["heures_tenues"] += hours


def _rounded(bucket: dict) -> dict:
    return {k: round(v, 2) if isinstance(v, float) else v for k, v in bucket.items()}


def aggregate(records, windows=()) -> dict:
    """Agrège les cours compactés par matière.

    - annules        : cours marqué annulé par Pronote
    - prof_absent    : statut « prof absent » (pas forcément marqué annulé)
    - classe_absente : statut « classe absente »
    - greve_blocus   : cours qui tombe (même en partie) dans un jour de grève/blocus saisi
    - perdus         : cours non tenu (une des causes ci-dessus), sans double compte
    - tenus          : cours - perdus
    Les compteurs « heures_* » sont les mêmes, en heures (durée réelle des cours).
    Les causes ne s'excluent pas : un cours annulé un jour de grève compte dans les deux.
    """
    seen = set()
    subjects: dict[str, dict] = {}
    statuses: Counter = Counter()
    totals = _empty()
    per_window: Counter = Counter()

    for lesson_id, start, end, subject, canceled, status, teacher_absent, class_absent in records:
        key = (lesson_id, start)
        if lesson_id is not None:
            if key in seen:
                continue
            seen.add(key)

        hours = max((end - start).total_seconds(), 0) / 3600 if end and start else 0.0

        blocked = False
        for w_start, w_end, origin in windows:
            if start < w_end and end > w_start:
                blocked = True
                per_window[origin] += 1
                break

        bucket = subjects.setdefault(subject, _empty() | {"statuts": Counter()})
        _add(bucket, hours, canceled, teacher_absent, class_absent, blocked)
        _add(totals, hours, canceled, teacher_absent, class_absent, blocked)
        if status:
            bucket["statuts"][status] += 1
            statuses[status] += 1

    ordered = {
        name: _rounded(bucket) | {"statuts": dict(bucket["statuts"])}
        for name, bucket in sorted(
            subjects.items(), key=lambda item: (-item[1]["perdus"], item[0])
        )
    }
    origins = list(dict.fromkeys(origin for _, _, origin in windows))
    return {
        "matieres": ordered,
        "totaux": _rounded(totals),
        "statuts": dict(statuses),
        "perturbations": {origin: per_window.get(origin, 0) for origin in origins},
    }
