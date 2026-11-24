# From apps/orders/services.py in the private repository (selected parts).
# Shown here as a sample: the rest of the system is in the private repo.

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from apps.bot import notices

from .choices import PIPELINE, Status
from .models import EXECUTOR_NEEDED_FROM, Order, OrderStatusEvent

SETTLEMENT_STATUSES = {Status.INVOICED, Status.PAID}


class StaleStatus(ValidationError):
    """The order is not in the status the page showed (someone changed it meanwhile)."""

    def __init__(self):
        super().__init__(_("The status has changed meanwhile: check the order and try again."))


@transaction.atomic
def change_status(
    *, order, to_status, user=None, driver=None, note="", source=None, expected_status=None
):
    """Move an order to another status and log who did it. Returns the order.

    Exactly one of user (office, web) and driver (Telegram bot, phase 3).
    expected_status: the status the person saw when they chose; given by the
    office's status form, so that a stale page cannot move an order that
    someone else has already moved.
    """
    if (user is None) == (driver is None):
        raise ValueError("Pass either user or driver.")
    order = Order.objects.select_for_update().get(pk=order.pk)
    current, target = Status(order.status), Status(to_status)
    note = note.strip()

    if expected_status is not None and Status(expected_status) != current:
        raise StaleStatus()
    if target == current:
        raise ValidationError(_("The order already has this status."))
    if target in SETTLEMENT_STATUSES:
        raise ValidationError(_("Invoiced and paid are set by recording the invoice or payment."))
    if changes_through_invoice(order):
        raise ValidationError(_("An invoiced order changes status through its invoice."))
    if reason_needed(current, target) and not note:
        raise ValidationError(_("Write down why: cancelling or going back needs a reason."))
    if Status.CANCELLED in (current, target):
        blocked = carrier_invoice_blocks(order)
        if blocked:
            raise ValidationError(blocked)
    if target in EXECUTOR_NEEDED_FROM:
        missing = executor_missing(order)
        if missing:
            raise ValidationError(missing)
    # Cancelled: the drivers who heard of the trip are told (product decision of
    # 03.11.2026), never why; who they are is read before the status changes.
    told = notices.told_drivers(order) if target == Status.CANCELLED else {}
    order = apply_status(order, target, user=user, driver=driver, note=note, source=source)
    notices.queue_trips_cancelled(told)
    # Brought back: those told of the cancellation get the trip again (review C2).
    if current == Status.CANCELLED and target in notices.TRIP_STATUSES:
        notices.queue_trips_restored(order)
    if current == Status.CANCELLED:
        # «CMR not needed» was said of this cancellation only (orders/no_cmr.py).
        # Imported here: no_cmr imports settlements, which imports this module.
        from .no_cmr import forget_mark

        forget_mark(order, user)
    return order


def changes_through_invoice(order):
    """Invoiced or paid, or a cancelled order whose cancellation was invoiced:
    from here on only recording or undoing the invoice moves the status."""
    return Status(order.status) in SETTLEMENT_STATUSES or client_invoiced(order)


def reason_needed(current, target):
    """Cancelling, going back a step and bringing back a cancelled order need a
    reason: the history must say why the order did not simply go forward."""
    if target == Status.CANCELLED or current == Status.CANCELLED:
        return True
    return PIPELINE.index(target) < PIPELINE.index(current)


def status_targets(order):
    """The statuses change_status() accepts for this order now, for the order page.

    A list of (status, needs a reason). change_status() checks the same rules
    again under the order's lock, so a page loaded a minute ago cannot slip
    past them; this list only saves the user from choosing a refused one.
    """
    if changes_through_invoice(order):
        return []
    current = Status(order.status)
    # A status refused for a missing leg or executor is left out; the page says
    # why (StatusChangeForm.missing_executor).
    blocked = set(EXECUTOR_NEEDED_FROM) if executor_missing(order) else set()
    # A recorded carrier's invoice: nothing into or out of "cancelled"
    # (StatusChangeForm.carrier_invoice_note says why).
    if carrier_invoice_blocks(order):
        if current == Status.CANCELLED:
            return []
        blocked.add(Status.CANCELLED)
    return [
        (status, reason_needed(current, status))
        for status in Status
        if status != current and status not in SETTLEMENT_STATUSES and status not in blocked
    ]


def apply_status(order, to_status, *, user=None, driver=None, note="", source=None):
    """Write the new status and its history row. Checks no rules: call it only
    from change_status() and orders/settlements.py, inside their transaction,
    with the order row locked."""
    source = source or (OrderStatusEvent.Origin.BOT if driver else OrderStatusEvent.Origin.WEB)
    from_status = order.status
    order.status = to_status
    order.save(update_fields=["status", "updated_at"])
    OrderStatusEvent.objects.create(
        order=order,
        from_status=from_status,
        to_status=to_status,
        source=source,
        changed_by_user=user,
        changed_by_driver=driver,
        note=note,
    )
    return order
