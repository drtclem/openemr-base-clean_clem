"""Phase 5: a small, curated drug-class cross-reactivity table.

`check_allergy_conflict` (app/tools.py) matches a proposed medication against
a patient's *recorded* allergy text -- it has no way to know, from patient
data alone, that amoxicillin and penicillin are the same drug class and
therefore cross-reactive. That relationship is general pharmacology, not
something any FHIR resource for this patient will ever state. This module
supplies just enough of that general knowledge for check_allergy_conflict to
also catch a same-class conflict, not only an exact/substring name match.

**This is explicitly NOT a production drug-interaction database and must
never be presented or relied on as one.** A real deployment needs a licensed,
maintained clinical reference (First Databank, Medi-Span, Multum, or
equivalent) integrated at the EHR/pharmacy layer, with actual pharmacist-
reviewed cross-reactivity data, allergy-severity grading, and routine
updates. This table is a scoped stand-in sized only to what this project's
demo patients and eval cases need -- see `evals/fixtures.py` /
`evals/golden_facts.py` for pid1's documented penicillin allergy, the case
this table exists to cover. Expand it only when a new demo patient or eval
case actually needs another class; it is not meant to grow into general
clinical decision support.
"""

from __future__ import annotations

# Each class lists the drugs this table recognizes as members of it. A
# patient's allergy to any one member should also flag a proposed medication
# that's a *different* member of the same class -- e.g. a penicillin allergy
# flagging amoxicillin. Scoped to what the demo data (pid1's documented,
# uncoded penicillin allergy) actually exercises; add a class only when a
# real demo patient or eval case needs it.
DRUG_CLASSES: dict[str, list[str]] = {
    "penicillins": [
        "penicillin",
        "amoxicillin",
        "ampicillin",
        "amoxicillin-clavulanate",
        "piperacillin",
        "nafcillin",
        "oxacillin",
        "dicloxacillin",
    ],
}


def _class_for_drug(name: str) -> str | None:
    """Which curated class `name` belongs to, if any. Substring match,
    deliberately as loose as check_allergy_conflict's own existing name
    matching (app/tools.py) -- this table is a stand-in for the same
    demo-scoped purpose, not a stricter parser layered on top of it."""
    normalized = name.strip().lower()
    if not normalized:
        return None
    for drug_class, members in DRUG_CLASSES.items():
        if any(normalized in member or member in normalized for member in members):
            return drug_class
    return None


def cross_reactive_class(allergy_text: str, medication_name: str) -> str | None:
    """Returns the shared curated drug class name if `allergy_text` (a
    recorded allergy) and `medication_name` (a proposed medication) are
    different drugs in the same class per DRUG_CLASSES above -- e.g.
    ("penicillin", "amoxicillin") -> "penicillins". Returns None if either
    drug isn't in any curated class, or if they're not in the same one.
    Does not itself decide whether this is a "direct" name match vs. a
    cross-class one -- callers (app/tools.py) should only treat this as a
    cross-reactive finding when a direct substring match didn't already
    fire, since a class also containing the exact allergy name would
    otherwise report every self-match as "cross-reactive" too.
    """
    allergy_class = _class_for_drug(allergy_text)
    if allergy_class is None:
        return None
    medication_class = _class_for_drug(medication_name)
    if medication_class == allergy_class:
        return allergy_class
    return None
