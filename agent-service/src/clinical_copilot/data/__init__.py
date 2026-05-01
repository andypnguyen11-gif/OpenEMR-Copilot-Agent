"""FHIR/REST data layer.

Talks to OpenEMR's FHIR R4 surface (per ARCHITECTURE §5: agent service has
*no* direct MariaDB access — every read goes through FHIR or REST). The
tool layer in :mod:`clinical_copilot.tools` projects these transport
shapes into the stable record contract in
:mod:`clinical_copilot.tools.records`.
"""
