# From apps/documents/views.py in the private repository (selected parts).
# Shown here as a sample: the rest of the system is in the private repo.

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import FileResponse, Http404, HttpResponseBadRequest, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.formats import date_format
from django.utils.http import content_disposition_header
from django.utils.text import capfirst
from django.views import View
from django.views.decorators.http import require_safe
from django.views.decorators.vary import vary_on_headers

from apps.accounts.mixins import OfficeRoleRequiredMixin

from . import archive
from .exports import start_month_export, stream_zip
from .models import Document


def private_headers(response):
    """The browser must take the stored type as it is and never guess one (a "PDF"
    that looks like HTML must not run as a page), and must not keep a copy in
    shared caches."""
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


def stored_file(field, *, filename, content_type):
    """A stored file as a download; 404 when the storage no longer has it."""
    try:
        stored = field.open("rb")
    except FileNotFoundError as error:
        raise Http404 from error
    return private_headers(
        FileResponse(stored, as_attachment=True, filename=filename, content_type=content_type)
    )


@login_not_required
@require_safe
def download(request, pk):
    """Send one stored document as a download, after the rights check.

    Not logged in: 404, as if the address did not exist. A redirect to the
    login page would tell a stranger that the document exists.

    For now every office user with the view permission may open the documents
    of every order: the roles have no per-order limits yet. Per-order checks
    (no IDOR) come with the phase-2 pages, together with the order list each
    user may see.

    Archived documents are still downloadable: they are history.
    Nothing here is logged: file names and order data are personal or business data.
    """
    if not request.user.is_authenticated:
        raise Http404
    if not request.user.has_perm("documents.view_document"):
        raise PermissionDenied
    document = get_object_or_404(Document.objects.select_related("order"), pk=pk)
    return stored_file(
        document.file, filename=document.download_name, content_type=document.content_type
    )
