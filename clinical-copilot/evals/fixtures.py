"""FHIR patient ids for the audit's fixture patients (audit-notes.md), on the
LOCAL docker/development-easy stack. These UUIDs are stack-specific -- they
will be different on the droplet, see README.md's deployment notes.
"""

PID1_ALICE = "98c4b82b-b07e-11f1-8334-022958ad0af8"  # normal: 2 problems, 2 meds, 1 (uncoded) allergy
PID2_BOB = "98c4c088-b07e-11f1-8334-022958ad0af8"  # normal: 1 problem, 1 med, 1 (uncoded) allergy
PID3_CAROL = "98c4c247-b07e-11f1-8334-022958ad0af8"  # empty chart
PID4_DAN = "98c4c335-b07e-11f1-8334-022958ad0af8"  # other-provider patient
PID5_ERIN = "98c4c3f8-b07e-11f1-8334-022958ad0af8"  # other-provider patient
PID6_ALICE_DUP = "98c4c4c8-b07e-11f1-8334-022958ad0af8"  # duplicate of pid1, conflicting metformin dose
