"""Unit tests for Outlook attachment download filtering.

Covers slice 16 F9: small inline signature images are skipped via the shared
``_is_inline_signature_image`` predicate while real attachments are kept.
"""

import pytest


@pytest.fixture
def patch_graph(monkeypatch):
    """Patch the graph_request that _download_attachments imports at call time."""
    from nymeria.tools import outlook_email as oe

    def _install(payload):
        def fake_graph_request(user_id, method, path, account_id=None, params=None):
            return True, payload

        monkeypatch.setattr(oe, "graph_request", fake_graph_request)

    return _install


def test_download_attachments_skips_small_inline_images(patch_graph):
    from nymeria.tools import outlook_attachments as oa

    patch_graph(
        {
            "value": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "logo.png",
                    "contentType": "image/png",
                    "isInline": True,
                    "contentBytes": "A" * 1000,  # base64 length 1000 < threshold
                },
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "report.csv",
                    "contentType": "text/csv",
                    "isInline": False,
                    "contentBytes": "B" * 2000,
                },
            ]
        }
    )

    attachments, skipped = oa._download_attachments("alice", "email-1")

    assert skipped == 1
    names = [a["name"] for a in attachments]
    assert "logo.png" not in names
    assert "report.csv" in names


def test_download_attachments_keeps_large_inline_images(patch_graph):
    from nymeria.tools import outlook_attachments as oa

    # An inline image above the byte threshold is a real image, not a signature.
    patch_graph(
        {
            "value": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": "photo.png",
                    "contentType": "image/png",
                    "isInline": True,
                    "contentBytes": "C" * 60000,  # > _INLINE_IMAGE_SKIP_BYTES
                },
            ]
        }
    )

    attachments, skipped = oa._download_attachments("alice", "email-1")

    assert skipped == 0
    assert [a["name"] for a in attachments] == ["photo.png"]


# --- _extract_xlsx markdown-table rendering (slice 16 F8) ------------------


def _xlsx_b64(sheets):
    """Build a base64-encoded .xlsx. ``sheets`` maps sheet name -> list of rows."""
    import base64
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # drop the default sheet so we control names/order
    for name, rows in sheets.items():
        ws = wb.create_sheet(title=name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def test_extract_xlsx_renders_single_sheet_table():
    from nymeria.tools import outlook_attachments as oa

    out = oa._extract_xlsx(
        _xlsx_b64({"Data": [["Name", "Role"], ["Alice", "Dev"]]}), "people.xlsx"
    )

    # Single sheet: no **Sheet:** heading, just the GFM table.
    assert out == "| Name | Role |\n| --- | --- |\n| Alice | Dev |"


def test_extract_xlsx_escapes_pipe_in_cell():
    from nymeria.tools import outlook_attachments as oa

    out = oa._extract_xlsx(
        _xlsx_b64({"Data": [["Header"], ["Dev|Ops"]]}), "x.xlsx"
    )

    assert "| Dev\\|Ops |" in out
    assert "| Dev|Ops |" not in out


def test_extract_xlsx_multi_sheet_keeps_sheet_headings():
    from nymeria.tools import outlook_attachments as oa

    out = oa._extract_xlsx(
        _xlsx_b64(
            {
                "First": [["a"], ["1"]],
                "Second": [["b"], ["2"]],
            }
        ),
        "two.xlsx",
    )

    assert out == (
        "**Sheet: First**\n\n| a |\n| --- |\n| 1 |"
        "\n\n"
        "**Sheet: Second**\n\n| b |\n| --- |\n| 2 |"
    )
