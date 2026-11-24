# From apps/orders/tests/test_numbers.py in the private repository.
# Shown here as a sample: the rest of the system is in the private repo.

"""Order numbers: "2026-10-00007", a counter per month, never twice the same."""

import datetime
import threading

import pytest
from django.db import connection, transaction

from apps.clients.tests.factories import ClientFactory
from apps.orders.services import create_order, next_number
from apps.orders.tests.factories import OrderFactory

pytestmark = pytest.mark.django_db


def new_order(loading_date):
    return OrderFactory.build(
        number="",
        loading_date=loading_date,
        unloading_date=loading_date + datetime.timedelta(days=1),
        client=ClientFactory(),
    )


def test_numbers_count_up_within_a_month(dispatcher):
    first = create_order(new_order(datetime.date(2026, 10, 5)), user=dispatcher)
    second = create_order(new_order(datetime.date(2026, 10, 30)), user=dispatcher)

    assert (first.number, second.number) == ("2026-10-00001", "2026-10-00002")


def test_every_month_starts_at_one(dispatcher):
    create_order(new_order(datetime.date(2026, 10, 5)), user=dispatcher)

    november = create_order(new_order(datetime.date(2026, 11, 2)), user=dispatcher)

    assert november.number == "2026-11-00001"


def test_the_month_is_the_month_of_the_last_unloading(dispatcher):
    # Open question 13, decided on 30.10.2026: loaded in September, unloaded in October.
    order = new_order(datetime.date(2026, 9, 30))
    order.unloading_date = datetime.date(2026, 10, 2)

    assert create_order(order, user=dispatcher).number == "2026-10-00001"


def test_the_number_stays_when_the_unloading_moves_to_another_month(dispatcher):
    order = create_order(new_order(datetime.date(2026, 10, 30)), user=dispatcher)
    number = order.number

    order.unloading_date = datetime.date(2026, 11, 3)
    order.full_clean()
    order.save()

    order.refresh_from_db()
    assert order.number == number == "2026-10-00001"
    # November still starts at 1: nothing was taken from its counter.
    november = create_order(new_order(datetime.date(2026, 11, 2)), user=dispatcher)
    assert november.number == "2026-11-00001"


@pytest.mark.django_db(transaction=True)
def test_orders_created_at_the_same_moment_get_different_numbers():
    """Eight orders are the first of a new month at once: no number twice, none skipped.

    Real threads with their own database connections, so the row lock and
    ON CONFLICT DO NOTHING in next_number() are really tested (PostgreSQL only).
    """
    workers = 8
    start = threading.Barrier(workers)
    numbers, errors = [], []

    def take_number():
        try:
            start.wait()
            with transaction.atomic():
                numbers.append(next_number(datetime.date(2027, 1, 15)))
        except Exception as error:  # noqa: BLE001 - collected and asserted below
            errors.append(error)
        finally:
            connection.close()

    threads = [threading.Thread(target=take_number) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(numbers) == [f"2027-01-{i:05d}" for i in range(1, workers + 1)]
