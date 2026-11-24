# From apps/orders/choices.py in the private repository (selected parts).
# Shown here as a sample: the rest of the system is in the private repo.

from django.db import models
from django.utils.translation import gettext_lazy as _

class Status(models.TextChoices):
    """The life of an order. Changed only by orders/services.py."""

    NEW = "new", _("new")
    PLANNED = "planned", _("planned")
    LOADING = "loading", _("loading")
    IN_TRANSIT = "in_transit", _("in transit")
    DELIVERED = "delivered", _("delivered")
    INVOICED = "invoiced", _("invoiced")
    PAID = "paid", _("paid")
    CANCELLED = "cancelled", _("cancelled")


PIPELINE = [
    Status.NEW,
    Status.PLANNED,
    Status.LOADING,
    Status.IN_TRANSIT,
    Status.DELIVERED,
    Status.INVOICED,
    Status.PAID,
]
