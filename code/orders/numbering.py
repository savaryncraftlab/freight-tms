# From apps/orders/services.py and apps/orders/models.py in the private repository (selected parts).
# Shown here as a sample: the rest of the system is in the private repo.

from django.db import models
from django.utils.translation import gettext_lazy as _


class OrderCounter(models.Model):
    """The last order number used in a month. Numbers restart at 00001 every month."""

    period = models.DateField(_("month"), unique=True, help_text=_("First day of the month."))
    last_value = models.PositiveIntegerField(_("last number"), default=0)

    class Meta:
        verbose_name = _("order counter")
        verbose_name_plural = _("order counters")

    def __str__(self):
        return f"{self.period:%Y-%m}: {self.last_value}"



def next_number(trip_date):
    """Take the next number of the trip's month (of trip_date): "2026-10-00007".

    INSERT ... ON CONFLICT DO NOTHING makes sure the month's row exists even
    when two orders are the first of a new month at the same moment; then
    SELECT ... FOR UPDATE makes the second one wait for the first.
    Call inside transaction.atomic().
    """
    period = trip_date.replace(day=1)
    OrderCounter.objects.bulk_create([OrderCounter(period=period)], ignore_conflicts=True)
    counter = OrderCounter.objects.select_for_update().get(period=period)
    counter.last_value += 1
    counter.save(update_fields=["last_value"])
    return f"{period:%Y-%m}-{counter.last_value:05d}"
