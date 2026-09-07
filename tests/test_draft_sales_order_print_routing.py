import ast
from pathlib import Path

UTILS = Path(__file__).resolve().parents[1] / "local_printers" / "utils.py"


def _function_source(name: str) -> str:
    source = UTILS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == name
    )
    return ast.get_source_segment(source, node) or ""


def test_event_handler_allows_only_draft_sales_order_without_pos_profile():
    source = _function_source("send_doc_details_on_event")
    assert "is_draft_sales_order_event" in source
    assert 'trigger_method == "after_insert"' in source
    assert "not has_pos_profile and not is_draft_sales_order_event" in source


def test_printer_settings_support_draft_sales_order_company_fallback():
    source = _function_source("get_printer_settings")
    assert 'doc.doctype == "Sales Order"' in source
    assert 'trigger_method == "after_insert"' in source
    assert 'filters["company"] = company' in source
    assert "distinct_pos_profiles" in source
