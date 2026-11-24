# From apps/documents/tests/test_download.py in the private repository.
# Shown here as a sample: the rest of the system is in the private repo.

"""The download view: only logged-in office users, always as an attachment."""

import pytest
from django.urls import reverse

from apps.documents.services import add_document, archive_document
from apps.documents.tests.factories import PDF, upload
from apps.documents.tests.helpers import download_url
from apps.orders.tests.factories import OrderFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def document(dispatcher):
    return add_document(
        order=OrderFactory(),
        kind="cmr",
        uploaded_file=upload("scan from phone.pdf", PDF),
        user=dispatcher,
    )


def test_anonymous_gets_404_as_if_nothing_were_there(client, document):
    existing = client.get(download_url(document))
    missing = client.get(reverse("documents:download", args=[document.pk + 1000]))

    # No redirect: it would reveal that the document exists and where the admin is.
    assert existing.status_code == missing.status_code == 404
    assert "Location" not in existing


def test_dispatcher_downloads_the_file_as_an_attachment(client, dispatcher, document):
    client.force_login(dispatcher)

    response = client.get(download_url(document))

    assert b"".join(response.streaming_content) == PDF
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"] == (
        f'attachment; filename="{document.order.number}_CMR_{document.pk}.pdf"'
    )
    assert response["X-Content-Type-Options"] == "nosniff"
    assert response["Cache-Control"] == "private, no-store"


def test_the_uploaded_name_is_not_used_for_the_download(client, dispatcher, document):
    client.force_login(dispatcher)

    response = client.get(download_url(document))

    assert "scan from phone" not in response["Content-Disposition"]


def test_archived_document_can_still_be_downloaded(client, dispatcher, document):
    archive_document(document=document, user=dispatcher, reason="Wrong order.")
    client.force_login(dispatcher)

    assert client.get(download_url(document)).status_code == 200


def test_unknown_document_is_404(client, dispatcher):
    client.force_login(dispatcher)

    assert client.get(reverse("documents:download", args=[999999])).status_code == 404


def test_missing_file_is_404_not_an_error(client, dispatcher, document):
    document.file.storage.delete(document.file.name)
    client.force_login(dispatcher)

    assert client.get(download_url(document)).status_code == 404
