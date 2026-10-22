"""Invented statement values only; no owner documents, accounts or live calls."""
from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from importlib import import_module

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from portfolio_model import FxRates, Transaction, TxType


def module():
    return import_module("statement_import")


def synthetic_pdf(pages):
    """Small Unicode text PDF; no external font/renderer or captured PDF bytes."""
    writer = PdfWriter()
    cmap = DecodedStreamObject()
    cmap.set_data(b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def /CMapName /Synthetic def /CMapType 2 def 1 begincodespacerange <0000> <FFFF> endcodespacerange 1 beginbfrange <0000> <FFFF> <0000> endbfrange endcmap CMapName currentdict /CMap defineresource pop end end")
    cid = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/CIDFontType2"),
        NameObject("/BaseFont"): NameObject("/Synthetic"), NameObject("/DW"): NumberObject(500),
        NameObject("/CIDSystemInfo"): DictionaryObject()})
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type0"),
        NameObject("/BaseFont"): NameObject("/Synthetic"), NameObject("/Encoding"): NameObject("/Identity-H"),
        NameObject("/DescendantFonts"): ArrayObject([writer._add_object(cid)]),
        NameObject("/ToUnicode"): writer._add_object(cmap)})
    for text in pages:
        page = writer.add_blank_page(1600, 850)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data("\n".join(f"BT /F1 8 Tf 1 0 0 1 20 {800 - i * 12} Tm <{line.encode('utf-16-be').hex()}> Tj ET" for i, line in enumerate(text.splitlines())).encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_statement_tools_are_full_catalog_only():
    import server
    from service_auth import SERVICE_READ_TOOLS
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    assert tools["preview_statement_import"].annotations.readOnlyHint is True
    hints = tools["apply_statement_import"].annotations
    assert hints.readOnlyHint is False and hints.destructiveHint is False and hints.idempotentHint is True
    assert not {"preview_statement_import", "apply_statement_import"} & SERVICE_READ_TOOLS


@pytest.mark.parametrize("name", ["../x.pdf", "a/b.pdf", "a\\b.pdf", "..", "/x.pdf"])
def test_inbox_rejects_paths(name, tmp_path):
    with pytest.raises(module().StatementError):
        module().read_inbox(tmp_path, name)


def test_inbox_rejects_links_nonregular_and_oversize(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic")
    (tmp_path / "link.pdf").symlink_to(source)
    (tmp_path / "directory.pdf").mkdir()
    for name in ("link.pdf", "directory.pdf"):
        with pytest.raises(module().StatementError):
            module().read_inbox(tmp_path, name)
    with pytest.raises(module().StatementError, match="size"):
        module().read_inbox(tmp_path, "source.pdf", max_bytes=2)


def trade(side=TxType.BUY, symbol="SYN", qty=3, gross=60, fee=0):
    return Transaction("toss", "toss:synthetic", "invented", side, "2017-03-04", "USD", symbol=symbol,
                       quantity=qty, price=gross / qty, gross_amount=gross, fee=fee, tax=0,
                       fee_status="reported" if fee else "verified_zero", tax_status="verified_zero")


def test_dedup_multiset_conflicts_and_idempotent_undo(tmp_path):
    m = module()
    api = trade()
    fresh = replace(api, trade_date="2017-02-03", tx_id="older")
    conflict = replace(api, gross_amount=61, price=61 / 3)
    dedup = m.deduplicate([api, fresh, conflict], [api])
    assert dedup["matched"] == 1 and dedup["new"] == [fresh] and len(dedup["conflicting"]) == 1
    # Identical-looking real rows remain a multiset, never collapse by guessing.
    assert len(m.deduplicate([api, api], [api])["new"]) == 1
    store = m.StatementStore(tmp_path / "portfolio.db")
    result = store.apply("synthetic-sha", "toss:synthetic", [fresh], {"parser_version": m.PARSER_VERSION})
    assert result["written_count"] == 1
    assert store.apply("synthetic-sha", "toss:synthetic", [fresh], {})["written_count"] == 0
    assert len(store.load("2017-01-01", "2017-12-31")[0]) == 1
    assert store.undo(result["import_id"])["removed_count"] == 1
    assert not store.load("2017-01-01", "2017-12-31")[0]


def test_dedup_uses_reported_dates_only_without_guessed_settlement_offsets():
    m = module()
    api = replace(trade(), settle_date="2017-03-06")
    # Either actual reported date can label activity; neither is guessed T+N.
    assert m.deduplicate([trade()], [api])["matched"] == 1
    settled = replace(trade(), trade_date="2017-03-06")
    assert m.deduplicate([settled], [api])["matched"] == 1
    unknown_date = replace(trade(), trade_date="2017-03-05")
    assert m.deduplicate([unknown_date], [api])["matched"] == 0


def test_imported_buy_repairs_only_covered_sell_basis_and_quantity_break(tmp_path):
    import portfolio_engine as engine
    from portfolio_sources import Collection
    m = module()
    sell = replace(trade(TxType.SELL), trade_date="2017-05-06", gross_amount=90, price=30, tx_id="sale")
    unrelated = replace(sell, symbol="UNRELATED", tx_id="uncovered")
    before = engine.compute_realized_pnl([sell, unrelated], FxRates({"USD": 2}))
    assert before["incomplete_cost_basis"] == ["SYN", "UNRELATED"]
    store = m.StatementStore(tmp_path / "portfolio.db")
    store.apply("synthetic-sha", "toss:synthetic", [trade()], {"parser_version": m.PARSER_VERSION})
    ledger = Collection(transactions=[sell, unrelated], health={"toss:synthetic:orders": {"status": "ok"}}, fetch_complete=True)
    m.merge_statements(ledger, store, "2017-01-01", "2017-12-31", None, None)
    after = engine.compute_realized_pnl(ledger.transactions, FxRates({"USD": 2}))
    assert after["incomplete_cost_basis"] == ["UNRELATED"]
    assert next(s for s in after["symbols"] if s["symbol"] == "SYN")["cost_basis_complete"]
    result = engine.reconcile_quantities(ledger.transactions, [], known_accounts=["toss:synthetic"])
    assert [b["symbol"] for b in result["reconciliation_breaks"]] == ["UNRELATED"]
    filtered = Collection(transactions=[])
    m.merge_statements(filtered, store, "2017-01-01", "2017-12-31", ["toss:other"], None)
    assert not filtered.transactions
    assert not any(t.tx_type not in {TxType.BUY, TxType.SELL} for t in ledger.transactions)


def test_collector_hook_adds_statement_source_and_preserves_api_failure(tmp_path):
    from test_portfolio_sources import FakeToss, build
    m = module()
    store = m.StatementStore(tmp_path / "ledger.db")
    stored = replace(trade(), account="toss:primary")
    store.apply("invented-hook", "toss:primary", [stored], {})
    collector = build(toss_clients={"primary": FakeToss(orders=[])}, kis_accounts=[])
    collector._providers.statement_store = lambda: store
    result = collector.collect_transactions("2017-01-01", "2017-12-31")
    assert len(result.transactions) == 1
    assert result.source_metadata["statement:toss:invented-hook"]["result_count"] == 1
    assert result.source_metadata["statement:toss:invented-hook"]["historical_coverage"] == "unknown"
    assert result.fetch_complete
    # Adding a statement source does not certify a failed API read as empty.
    from portfolio_sources import Collection
    failed = Collection(health={"toss:primary:orders": {"status": "error"}}, failed_chunks=[{"source": "toss:primary:orders"}])
    m.merge_statements(failed, store, "2017-01-01", "2017-12-31", None, None)
    assert not failed.fetch_complete and failed.failed_sources == ["toss:primary:orders"]


def test_unknown_layout_and_wrong_broker_fail_explicitly():
    m = module()
    with pytest.raises(m.StatementError, match="layout"):
        m.TossPdfParser().parse(synthetic_pdf(["Invented unsupported document"]))
    with pytest.raises(m.StatementError):
        m.parser_for("other", "pdf")


def test_safe_errors_and_preview_expiry_without_writes(tmp_path, monkeypatch):
    m = module()
    manager = m.PreviewManager(m.StatementStore(tmp_path / "portfolio.db"), tmp_path, ttl=1)
    with pytest.raises(m.StatementError, match="expired"):
        manager.apply("no-such-preview", True)
    assert not (tmp_path / "portfolio.db").exists()


COLUMNS = ["거래일자", "거래구분", "종목명(종목코드)", "환율", "거래수량", "거래대금", "정산금액", "단가", "수수료", "거래세", "제세금", "인지/농특", "잔고", "잔액"]
WIDTHS = [12, 22, 35, 12, 14, 18, 18, 16, 12, 12, 12, 14, 14, 16]


def line(values):
    return "".join(str(v).ljust(width) for v, width in zip(values, WIDTHS)).rstrip()


HEADER = line(COLUMNS)
INTRO = "거래내역서\n성명 가상고객\n계좌 번호 643-21-867530\n조회 기간 2017년 1월 1일 ~ 2017년 12월 31일\n원화 거래내역\n" + HEADER
DOMESTIC = line(["2017.02.03", "매수", "가상주식(SYNKR)", "", "2", "24,000", "24,038", "12,000", "35", "3", "0", "0", "2", "51,234"])
FOREIGN = line(["2017.04.05", "매수(소수", "가상외국주식(SYNUS)", "2,000", "2", "110,000", "110,300", "55,000", "200", "40", "60", "0", "2", "57,891"])
WRAP = line(["", "점)"])
NATIVE = line(["", "", "", "", "", "($ 55.00)", "($ 55.15)", "($ 27.50)", "($ 0.10)", "($ 0.02)", "($ 0.03)", "($ 0.00)", "", "($ 31.24)"])


def test_layout_repeated_headers_wrapped_trade_both_currencies_and_signs():
    m = module()
    sell = line(["2017.08.09", "매도", "가상외국주식(SYNUS)", "2,001", "1", "80,040", "79,640", "80,040", "200", "0", "200", "0", "1", "87,654"])
    native = line(["", "", "", "", "", "($ 40.00)", "($ 39.80)", "($ 40.00)", "($ 0.10)", "($ 0.00)", "($ 0.10)", "($ 0.00)", "", "($ 55.55)"])
    data = synthetic_pdf([INTRO + "\n" + DOMESTIC + "\n외화 거래내역\n" + HEADER + "\n" + FOREIGN + "\n" + WRAP,
                          HEADER + "\n" + NATIVE + "\n" + sell + "\n" + native])
    result = m.TossPdfParser().parse(data)
    assert not result.issues
    assert result.page_count == 2 and len(result.rows) == 3
    assert result.covered_period == {"start": "2017-01-01", "end": "2017-12-31"}
    krw, usd, sold = result.rows
    assert (krw.currency, krw.quantity, krw.gross_amount, krw.fee, krw.tax, krw.net_amount) == ("KRW", 2, 24000, 35, 3, -24038)
    assert (usd.currency, usd.price, usd.gross_amount, usd.fee, usd.tax, usd.net_amount) == ("USD", 27.5, 55, .1, .05, -55.15)
    assert sold.tx_type == TxType.SELL and sold.net_amount == 39.8
    assert usd.statement_provenance["native_code"] == "SYNUS"
    assert usd.statement_provenance["parser_version"] == m.PARSER_VERSION
    assert usd.statement_provenance["original_amounts"]["KRW"]["gross_amount"] == "110000"
    assert usd.fx_rate is None  # Printed book conversion is not verified execution FX.
    assert usd.statement_provenance["statement_conversion_rate"] == "2000"


def test_missing_native_costs_remain_unknown_and_wrong_date_is_an_issue():
    m = module()
    missing = line(["", "", "", "", "", "($ 55.00)", "($ 55.15)", "($ 27.50)", "($ -)", "($ 0.02)", "($ -)", "($ 0.00)", "", "($ 31.24)"])
    parsed = m.TossPdfParser().parse(synthetic_pdf([INTRO + "\n외화 거래내역\n" + HEADER + "\n" + FOREIGN + "\n" + WRAP + "\n" + missing]))
    assert not parsed.issues
    assert parsed.rows[0].fee_status == "unknown" and parsed.rows[0].tax_status == "unknown"
    assert parsed.rows[0].to_dict()["fee"] is None and parsed.rows[0].to_dict()["tax"] is None
    assert parsed.rows[0].tax == .02  # Explicitly observed component remains diagnostic.
    wrong_date = DOMESTIC.replace("2017.02.03", "17.02.03")
    result = m.TossPdfParser().parse(synthetic_pdf([INTRO + "\n" + wrong_date]))
    assert result.issues and not result.rows


def test_nontrades_are_counted_only_and_ambiguous_trade_blocks_apply(tmp_path):
    m = module()
    skipped = ["배당금", "입금", "출금", "이자입금", "환전원화출금", "주식분할", "대체입고"]
    pages = INTRO + "\n" + DOMESTIC + "\n" + "\n".join(line(["2017.04.05", kind, "", "", "0", "17", "17", "0", "0", "0", "0", "0", "0", "21"]) for kind in skipped)
    parsed = m.TossPdfParser().parse(synthetic_pdf([pages]))
    assert parsed.skipped_by_type == dict.fromkeys(skipped, 1)
    assert len(parsed.rows) == 1 and not parsed.issues
    ambiguous = line(["2017.04.05", "알수없는매수", "가상주식(SYNKR)", "", "2", "24,000", "24,038", "12,000", "35", "3", "0", "0", "2", "51,234"])
    data = synthetic_pdf([pages + "\n" + ambiguous])
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    (inbox / "synthetic.pdf").write_bytes(data)
    manager = m.PreviewManager(m.StatementStore(tmp_path / "ledger.db"), inbox, labels=lambda: ["synthetic"])
    result = manager.preview("toss", "synthetic.pdf")
    assert result["parse_issues"]["count"] == 1
    assert manager.apply(result["preview_id"])["blocked"]
    with pytest.raises(m.StatementError, match="Parse issues"):
        manager.apply(result["preview_id"], True)
    assert not (tmp_path / "ledger.db").exists()
    assert "64321867530" not in str(result) and "643-21-867530" not in str(result)
    assert "가상고객" not in str(result)


def test_preview_plan_confirm_ttl_and_store_provenance(tmp_path, monkeypatch):
    m = module()
    from portfolio_sources import Collection
    from toss_accounts import TossAccountRegistry, TossCredentials
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    (inbox / "synthetic.pdf").write_bytes(synthetic_pdf([INTRO + "\n" + DOMESTIC]))
    store = m.StatementStore(tmp_path / "ledger.db")
    clock = [100]
    monkeypatch.setattr(m.time, "monotonic", lambda: clock[0])
    manager = m.PreviewManager(store, inbox, ledger=lambda *a: Collection(health={"toss:synthetic:orders": {"status": "ok"}}), labels=lambda: ["synthetic"], ttl=5,
        registry=lambda: TossAccountRegistry([TossCredentials('synthetic', 'invented-client', 'invented-secret', '64321867530')], cache_dir=tmp_path))
    preview = manager.preview("toss", "synthetic.pdf")
    assert preview["dedup"] == {"matched": 0, "new": 1, "conflicting": 0, "conflicts": []}
    assert manager.apply(preview["preview_id"])["would_write_count"] == 1
    assert not store.db_path.exists()
    applied = manager.apply(preview["preview_id"], True)
    assert applied["written_count"] == 1
    assert manager.apply(preview["preview_id"], True)["written_count"] == 0
    rows, _ = store.load("2017-01-01", "2017-12-31")
    assert rows[0].source == "statement:toss:" + preview["file_sha256"]
    assert rows[0].to_dict()["statement_provenance"]["parser_version"] == m.PARSER_VERSION
    clock[0] = 106
    with pytest.raises(m.StatementError, match="expired"):
        manager.apply(preview["preview_id"], True)


def test_summary_loads_imported_lots_before_reporting_year(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import server
    from portfolio_sources import Collection
    m = module()
    store = m.StatementStore(tmp_path / "ledger.db")
    buy = replace(trade(), trade_date="2016-02-03")
    sell = replace(trade(TxType.SELL), trade_date="2017-05-06", gross_amount=90, price=30, tx_id="sale")
    store.apply("invented-basis", "toss:synthetic", [buy], {})
    calls = []
    def transactions(start, end, *args):
        calls.append((start, end))
        c = Collection(transactions=[sell], requested_window={"start": start, "end": end}, health={"toss:synthetic:orders": {"status": "ok"}}, fetch_complete=True)
        return m.merge_statements(c, store, start, end, None, None)
    collector = SimpleNamespace(collect_positions=lambda: Collection(health={"toss:synthetic": {"status": "ok"}}, fetched_at="2017-12-31T00:00:00Z"),
        collect_transactions=transactions, build_fx=lambda _: FxRates({"USD": 2}), _providers=SimpleNamespace(statement_store=lambda: store))
    monkeypatch.setattr(server, "_get_collector", lambda: collector)
    monkeypatch.setattr(server.portfolio_engine, "resolve_period", lambda period: ("2016-01-01" if period == "ALL" else "2017-01-01", "2017-12-31"))
    result = server.get_portfolio_summary(snapshot=False)
    assert result["ok"] and result["data"]["realized_pnl_krw"] == 60
    assert result["data"]["realized_pnl_status"] == "complete"
    assert calls == [("2017-01-01", "2017-12-31")]


def test_confirmed_layout_purchase_sale_and_repayment_is_not_tax():
    columns = COLUMNS.copy()
    columns[11] = "변제/연체합"
    header = line(columns)
    # Explicit, invented repayment amount must never become tax/capitalized cost.
    buy = line(["2017.02.03", "구매", "가상주식(SYNKR)", "", "2", "24,000", "24,038", "12,000", "35", "3", "0", "7", "2", "51,234"])
    sell = line(["2017.03.04", "판매", "가상주식(SYNKR)", "", "1", "15,000", "14,960", "15,000", "35", "5", "0", "9", "1", "60,000"])
    parsed = module().TossPdfParser().parse(synthetic_pdf([INTRO.replace(HEADER, header) + "\n" + buy + "\n" + sell]))
    assert not parsed.issues and len(parsed.rows) == 2
    assert [t.tx_type for t in parsed.rows] == [TxType.BUY, TxType.SELL]
    assert [t.tax for t in parsed.rows] == [3, 5]
    assert parsed.rows[0].statement_provenance["non_tax_components"]["변제/연체합"] == "7"


def test_foreign_confirmed_columns_and_full_nontrade_vocabulary(tmp_path):
    m = module()
    columns = [c for c in COLUMNS if c != "거래세"]
    columns[columns.index("인지/농특")] = "변제/연체합"
    def foreign_line(values):
        return line([*values[:9], "", *values[9:]])
    header = foreign_line(columns)
    buy = foreign_line(["2017.03.04", "구매", "가상외국주식(SYNUS)", "2,000", "2", "110,000", "110,300", "55,000", "200", "100", "20", "2", "50,000"])
    native = foreign_line(["", "", "", "", "", "($ 55.00)", "($ 55.15)", "($ 27.50)", "($ 0.10)", "($ 0.05)", "($ 0.01)", "", "($ 25.00)"])
    skipped = ["이자입금", "이체입금", "이체출금(가상은행)", "환전원화출금", "환전원화입금", "캐시백입금", "배당금입금", "대출", "환전외화입금", "환전외화출금", "외화증권배당금입금", "배당세출금", "외화이자세금출금", "해외채권세금출금", "외화이자입금", "대차거래 대여출고", "대차거래 상환입고", "대여료 입금", "외화채권매수", "외화채권매도", "주식병합출고"]
    rows = [foreign_line(["2017.04.05", k, "", "", "0", "0", "0", "0", "0", "0", "0", "0", "0"]) for k in skipped]
    wrapped = foreign_line(["2017.04.05", "외화주식병합단주대금", "", "", "0", "0", "0", "0", "0", "0", "0", "0", "0"]) + "\n" + foreign_line(["", "입금", "", "", "", "($ 0.00)", "($ 0.00)", "($ 0.00)", "($ 0.00)", "($ 0.00)", "($ 0.00)", "", "($ 0.00)"])
    parsed = m.TossPdfParser().parse(synthetic_pdf([INTRO.replace(HEADER, header) + "\n" + buy + "\n" + native + "\n" + "\n".join(rows) + "\n" + wrapped]))
    assert not parsed.issues and len(parsed.rows) == 1
    assert parsed.rows[0].currency == "USD" and parsed.rows[0].tax == .05
    assert parsed.rows[0].statement_provenance["non_tax_components"]["변제/연체합"] == "0.01"
    assert parsed.skipped_by_type["외화주식병합단주대금입금"] == 1
    assert parsed.skipped_by_type["이체출금([COUNTERPARTY])"] == 1
    for kind in ("외화채권매수", "외화채권매도"):
        assert parsed.skipped_by_type[kind] == 1
        assert parsed.skipped_reasons_by_type[kind] == ["UNSUPPORTED_INSTRUMENT_NO_STABLE_ID"]
    assert sum(parsed.skipped_by_type.values()) == len(skipped) + 1


def test_unavailable_api_dedup_blocks_apply_and_marks_preview_partial(tmp_path):
    from portfolio_sources import Collection
    m = module()
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    (inbox / "synthetic.pdf").write_bytes(synthetic_pdf([INTRO + "\n" + DOMESTIC]))
    store = m.StatementStore(tmp_path / "ledger.db")
    manager = m.PreviewManager(store, inbox, ledger=lambda *a: Collection(health={"toss:synthetic:orders": {"status": "error"}}), labels=lambda: ["synthetic"])
    preview = manager.preview("toss", "synthetic.pdf")
    assert preview["result_status"] == "partial"
    assert manager.apply(preview["preview_id"])["blocked"]
    with pytest.raises(m.StatementError, match="API ledger"):
        manager.apply(preview["preview_id"], True)
    assert not store.db_path.exists()


def test_wrapped_instrument_code_shares_native_amount_line():
    columns = COLUMNS.copy()
    columns[11] = "변제/연체합"
    header = line(columns)
    buy = line(["2017.02.03", "구매", "가상 긴 상품 이름 2배", "2,000", "2", "110,000", "110,300", "55,000", "200", "40", "60", "0", "2", "57,891"])
    continuation = line(["", "", "(SYNWRAP)", "", "", "($ 55.00)", "($ 55.15)", "($ 27.50)", "($ 0.10)", "($ 0.02)", "($ 0.03)", "($ 0.00)", "", "($ 31.24)"])
    parsed = module().TossPdfParser().parse(synthetic_pdf([INTRO.replace(HEADER, header) + "\n" + buy + "\n" + continuation]))
    assert not parsed.issues and len(parsed.rows) == 1
    assert parsed.rows[0].symbol == "SYNWRAP" and parsed.rows[0].gross_amount == 55


def test_nontrade_counterparty_suffix_is_masked_in_preview(tmp_path):
    m = module()
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    nontrade = line(["2017.04.05", "이체입금(가상고객", "", "", "0", "0", "0", "0", "0", "0", "0", "0", "0", "0"]) + "\n" + line(["", "64321867530)"])
    (inbox / "synthetic.pdf").write_bytes(synthetic_pdf([INTRO + "\n" + nontrade]))
    manager = m.PreviewManager(m.StatementStore(tmp_path / "ledger.db"), inbox, labels=lambda: ["synthetic"])
    preview = manager.preview("toss", "synthetic.pdf")
    assert preview["skipped_by_type"] == {"이체입금([COUNTERPARTY])": 1}
    assert "가상고객" not in str(preview) and "64321867530" not in str(preview)


@pytest.mark.parametrize("balance_tail", ["-1765,432", "4,5678,901,234"])
def test_touching_balance_columns_do_not_obscure_trade_amounts(balance_tail):
    cols = COLUMNS.copy()
    cols[11] = "변제/연체합"
    header = line(cols)
    row = line(["2017.02.03", "판매", "가상주식(SYNKR)", "", "2", "30,000", "29,962", "15,000", "35", "3", "0", "0", "", balance_tail])
    parsed = module().TossPdfParser().parse(synthetic_pdf([INTRO.replace(HEADER, header) + "\n" + row]))
    assert not parsed.issues and len(parsed.rows) == 1
    assert parsed.rows[0].gross_amount == 30000 and parsed.rows[0].tax == 3
    assert parsed.rows[0].statement_provenance["balance_columns_status"] == "not_imported"


def test_converted_foreign_money_without_native_line_is_not_krw_trade():
    row = FOREIGN.replace("매수(소수", "구매     ")
    parsed = module().TossPdfParser().parse(synthetic_pdf([INTRO + "\n" + row]))
    assert parsed.issues and not parsed.rows


@pytest.mark.parametrize("class_name", ["주", "CLASS"])
def test_parentheses_inside_wrapped_name_are_not_the_instrument_code(class_name):
    cols = COLUMNS.copy()
    cols[11] = "변제/연체합"
    header = line(cols)
    buy = line(["2017.02.03", "구매", f"가상 펀드({class_name}) 2배 상품", "2,000", "2", "110,000", "110,300", "55,000", "200", "40", "60", "0", "2", "57,891"])
    continuation = line(["", "", "(SYNWRAP)", "", "", "($ 55.00)", "($ 55.15)", "($ 27.50)", "($ 0.10)", "($ 0.02)", "($ 0.03)", "($ 0.00)", "", "($ 31.24)"])
    parsed = module().TossPdfParser().parse(synthetic_pdf([INTRO.replace(HEADER, header) + "\n" + buy + "\n" + continuation]))
    assert not parsed.issues and len(parsed.rows) == 1
    assert parsed.rows[0].symbol == "SYNWRAP"
    assert parsed.rows[0].name == f"가상 펀드({class_name}) 2배 상품"
