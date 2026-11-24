# From apps/bot/tests/test_carrier_drivers.py in the private repository (selected parts).
# Shown here as a sample: the rest of the system is in the private repo.

import datetime
from decimal import Decimal
from unittest import mock

import pytest
from django.conf import settings
from django.utils import timezone

from apps.bot import notices, services
from apps.bot.models import DriverNotice
from apps.bot.tests.fake_telegram import TELEGRAM_ID
from apps.bot.tests.helpers import card_text
from apps.carriers.tests.factories import CarrierFactory, CarrierVehicleFactory
from apps.clients.tests.factories import ClientFactory
from apps.fleet.models import Driver
from apps.fleet.tests.factories import DriverFactory, TruckFactory
from apps.orders.choices import LegStep, Status
from apps.orders.models import LegProgressEvent, Order, OrderLeg, OrderStatusEvent
from apps.orders.services import change_status
from apps.orders.tests.factories import OrderFactory, OrderLegFactory


AT_LOADING, LOADED, UNLOADED = LegStep.AT_LOADING, LegStep.LOADED, LegStep.UNLOADED


OTHER_ID = TELEGRAM_ID + 1


@pytest.fixture
def carrier(db):
    return CarrierFactory(name=CARRIER_NAME)


@pytest.fixture
def vehicle(carrier):
    return CarrierVehicleFactory(carrier=carrier, name="Volvo 1")


@pytest.fixture
def carrier_driver(vehicle):
    """A linked driver of the carrier's regular truck."""
    return DriverFactory(
        first_name="Jan",
        last_name="Przewoźnik",
        carrier_vehicle=vehicle,
        telegram_user_id=TELEGRAM_ID,
    )


def carrier_trip(driver, *, status=Status.PLANNED, carrier=None, **order):
    """A priced order of a named client, driven by the carrier of the driver's truck."""
    vehicle = driver.carrier_vehicle
    fields = {
        "status": status,
        "number": "2026-10-00042",
        "client": ClientFactory(name=CLIENT_NAME),
        "client_ref": "LOAD-7731",
        "client_price": Decimal("1987.65"),
        "currency": "EUR",
        "loading_postal_code": "85-001",
        "loading_city": "Bydgoszcz",
        "loading_address": "ul. Fabryczna 1",
        "loading_date": datetime.date(2026, 10, 5),
        "unloading_postal_code": "10115",
        "unloading_city": "Berlin",
        "unloading_address": "Lagerstraße 5",
        "unloading_date": datetime.date(2026, 10, 6),
        "cargo_description": "Sofas on 12 pallets",
        **order,
    }
    return OrderLegFactory(
        order=OrderFactory(**fields),
        position=1,
        carrier=carrier or vehicle.carrier,
        carrier_vehicle=vehicle,
        carrier_price=Decimal("1234.56"),
        carrier_currency="EUR",
        carrier_vat_rate="23",
        carrier_truck_plate=vehicle.truck_plate,
        driver=driver,
        driver_note="Gate 4, call the warehouse 30 minutes before.",
    )


def status_of(leg):
    return Order.objects.get(pk=leg.order_id).status


@pytest.mark.django_db
def test_a_driver_never_gets_another_drivers_leg(carrier, carrier_driver):
    """IDOR: the same carrier's other driver, another carrier's driver and our own
    driver get nothing of this leg: not the trip, not a button, not a photo."""
    leg = carrier_trip(carrier_driver)
    others = [
        DriverFactory(
            carrier_vehicle=CarrierVehicleFactory(carrier=carrier), telegram_user_id=OTHER_ID
        ),
        DriverFactory(carrier_vehicle=CarrierVehicleFactory(), telegram_user_id=OTHER_ID + 1),
        DriverFactory(telegram_user_id=OTHER_ID + 2),
    ]

    for other in others:
        assert services.active_leg(other) is None
        assert services.photo_leg(other) is None
        assert services.keyboard_state(other).step is None
        assert services.my_trip(other.telegram_user_id).text is None
        # A button naming this leg's trip, pressed on another phone: nothing recorded.
        result = services.record_step(
            other.telegram_user_id, AT_LOADING, *services.button_trip(leg)
        )
        assert result.reason == "no_trip"
    assert not LegProgressEvent.objects.exists()
    assert status_of(leg) == Status.PLANNED


@pytest.mark.django_db
def test_a_leg_given_to_another_driver_leaves_the_first_one(carrier, carrier_driver):
    leg = carrier_trip(carrier_driver)
    leg.driver = DriverFactory(carrier_vehicle=CarrierVehicleFactory(carrier=carrier))
    leg.full_clean()
    leg.save()

    assert services.active_leg(carrier_driver) is None
