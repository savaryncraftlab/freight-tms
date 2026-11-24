# From apps/documents/cmr_cutoff.py in the private repository.
# Shown here as a sample: the rest of the system is in the private repo.

"""When a CMR counts as the signed delivery CMR of a leg: the one rule.

The owner's decisions (18.11.2026 and 19.11.2026): only a CMR added after the
driver pressed "Loaded" for the leg is the signed delivery CMR. One added earlier,
e.g. the loading CMR the office uploaded with the kind "CMR", is not it. Only the
driver's live "Loaded" step (the newest one, if the office undid and the driver
pressed it again) sets the cut-off. A leg without it, e.g. a trip the office ran
alone, has no cut-off: the office sees what it uploads, and any live CMR counts.

The rule has two forms side by side, which a test keeps in step:
- Python, for rows already loaded: loaded_at() and counts_after(). It asks the
  database nothing when the caller prefetched loaded_steps_prefetch() (the Documents
  page, the order card, the client letter: documents/missing.py signed_cmrs()).
- Database, for a query: signed_cmrs_query() (the bot, the carriers' rating, the
  CMR reminders).
"""

from django.db.models import Exists, OuterRef, Prefetch, Q

from apps.orders.choices import LegStep
from apps.orders.delivery import live_steps

from .models import Document

# The name the live "Loaded" steps of a leg are loaded under. Not "progress": that name
# belongs to all the steps, and a filtered list under it would surprise a later reader.
LOADED_STEPS = "live_loaded_steps"


def loaded_steps_prefetch():
    """The live "Loaded" steps of legs, as leg.live_loaded_steps (all loaded_at() reads)."""
    return Prefetch(
        "progress",
        queryset=live_steps().filter(step=LegStep.LOADED),
        to_attr=LOADED_STEPS,
    )


def loaded_at(leg):
    """When the driver loaded the leg (its newest live "Loaded" step), or None.
    The leg must have loaded_steps_prefetch() loaded."""
    return max((step.created_at for step in getattr(leg, LOADED_STEPS)), default=None)


def counts_after(uploaded_at, loaded):
    """Does a CMR uploaded at this time count, for a leg loaded at `loaded` (or None)?"""
    return loaded is None or uploaded_at > loaded


def live_cmrs(leg_id, order_id):
    """The CMRs that cover a leg: on the leg itself, or on the whole order (one scan of
    all the CMRs covers every leg). Archived ones do not count. The ids may be values
    or OuterRef() of the query this one is used in."""
    return Document.objects.filter(
        Q(leg_id=leg_id) | Q(leg__isnull=True, order_id=order_id),
        kind=Document.Kind.CMR,
        is_archived=False,
    )


def signed_cmrs_query(leg_id, order_id):
    """The signed delivery CMRs of a leg, as a query set: live_cmrs() uploaded after
    the leg's newest live "Loaded" step. That is counts_after() in SQL: no live
    "Loaded" step at or after the upload. The ids may be values or OuterRef()."""
    # Inside this query the Loaded steps are one level deeper: an OuterRef must go up twice.
    deeper = OuterRef(leg_id) if isinstance(leg_id, OuterRef) else leg_id
    loaded_later = live_steps().filter(
        leg_id=deeper, step=LegStep.LOADED, created_at__gte=OuterRef("uploaded_at")
    )
    return live_cmrs(leg_id, order_id).filter(~Exists(loaded_later))
