"""Synthetic right-aligned statement money; no owner files or values."""
import re

import pytest
from test_owner_flows import HEADER_TEXT, module
from test_owner_flows_review3 import ingest
from test_statement_import import COLUMNS, HEADER, WIDTHS, synthetic_pdf


def right_aligned_statement(tmp_path, *, tail='12,345,678,901', balance='0', kind='이체입금', foreign=False, double_decimal=False):
    columns = [c for c in COLUMNS if not foreign or c != '거래세']
    widths = dict(zip(COLUMNS, WIDTHS))
    header = ''.join(c.ljust(widths[c]) for c in columns).rstrip()
    spans = [(m[0], m.start(), m.end()) for m in re.finditer(r'\S+', header)]
    cells = dict.fromkeys(columns, '0')
    cells.update(거래일자='2026.01.01', 거래구분=kind, 환율='', 거래대금='40,000',
                 정산금액='12,345', 잔고=balance, 잔액=tail)
    cells['종목명(종목코드)'] = ''
    if kind == '환전원화입금' or double_decimal:
        cells['환율'] = '1300.00'
    if double_decimal:
        cells['거래수량'] = '1234567890.000'

    def render(values):
        chars = [' '] * len(header)
        for i, (field, start, end) in enumerate(spans):
            value = values[field]
            offset = start if i < 3 else end - len(value)
            assert offset >= 0
            for j, char in enumerate(value, offset):
                assert chars[j] == ' '  # Adjacent tokens may touch; never overlap.
                chars[j] = char
        return ''.join(chars).rstrip()

    row = render(cells)
    # Verify the intended shape before PDF extraction.
    numeric = re.findall(r'(?<!\S)(?:[+-]?[\d,]+(?:\.\d+)?|—|-)(?!\S)', row[spans[3][1] - 10:])
    if double_decimal:
        assert len(numeric) == 8
    elif len(tail) == 14:
        assert len(numeric) == (10 if cells['환율'] else 9)
        assert numeric[-1].startswith(balance)
    else:
        assert len(numeric) == 10
        assert not numeric[-1].startswith('0')
    text = HEADER_TEXT.replace(HEADER, header)
    if foreign:
        text = text.replace('원화 거래내역', '외화 거래내역')
        money = set(columns[3:]) - {'환율', '거래수량', '잔고'}
        native = {c: '($ 0)' if c in money else '' for c in columns}
        native.update(거래대금='($ 3)', 정산금액='($ 2.50)', 잔액='($ 20)')
        row += '\n' + render(native)
    file = tmp_path / 'right-aligned.pdf'
    file.write_bytes(synthetic_pdf([text + '\n' + row]))
    return file


@pytest.mark.parametrize('kind,classification', [('이체입금', 'external_in'), ('이체출금', 'external_out'), ('환전원화입금', 'internal')])
def test_right_aligned_glued_zero_preserves_settlement(tmp_path, monkeypatch, kind, classification):
    captured = []
    original = module().TossFlowParser._amount_columns

    def capture(self, row):
        result = original(self, row)
        captured.append(result[0])
        return result

    monkeypatch.setattr(module().TossFlowParser, '_amount_columns', capture)
    counts, event, facts, _ = ingest(tmp_path, right_aligned_statement(tmp_path, kind=kind))
    assert counts[classification] == 1 and counts['unknown'] == 0
    assert event['amount'] == facts['native_amount'] == '12345'
    assert captured[0]['잔고'] == 0 and captured[0]['잔액'] == 12345678901
    assert facts['reason_codes'] == []


def test_glued_zero_noncanonical_remainder_drops_only_balances(tmp_path, monkeypatch):
    captured = []
    original = module().TossFlowParser._amount_columns

    def capture(self, row):
        result = original(self, row)
        captured.append(result[0])
        return result

    monkeypatch.setattr(module().TossFlowParser, '_amount_columns', capture)
    counts, event, facts, _ = ingest(tmp_path, right_aligned_statement(tmp_path, tail='1,2345,678,901'))
    assert counts['external_in'] == 1 and counts['unknown'] == 0
    assert event['amount'] == '12345'
    assert captured[0]['잔고'] is captured[0]['잔액'] is None
    assert facts['reason_codes'] == []


def test_two_decimal_glue_dividend_is_evidence_with_reason(tmp_path):
    counts, event, facts, coverage = ingest(tmp_path, right_aligned_statement(
        tmp_path, kind='외화증권배당금입금', tail='123,456', foreign=True, double_decimal=True))
    assert counts['evidence'] == 1 and counts['unknown'] == 0
    assert event['amount'] is None
    assert facts['reason_codes'] == ['OWNER_EXPORT_AMOUNT_UNPARSED']
    assert coverage['status'] == 'complete'


def test_normal_right_aligned_ten_tokens_unchanged(tmp_path):
    counts, event, facts, coverage = ingest(tmp_path, right_aligned_statement(tmp_path, tail='123,456'))
    assert counts['external_in'] == 1 and counts['unknown'] == 0
    assert event['amount'] == facts['native_amount'] == '12345'
    assert facts['reason_codes'] == [] and coverage['status'] == 'complete'


def test_nonzero_glued_balance_transfer_remains_unknown(tmp_path):
    counts, event, facts, coverage = ingest(tmp_path, right_aligned_statement(tmp_path, balance='7'))
    assert counts['unknown'] == 1 and event['amount'] is None
    assert facts['reason_codes'] == ['OWNER_EXPORT_AMOUNT_UNPARSED']
    assert coverage['status'] == 'partial'


@pytest.mark.parametrize('prefix', ['+', '-'])
def test_signed_zero_prefix_is_supported(prefix):
    # The optional sign belongs to the zero prefix; never guess a cash sign.
    fields = COLUMNS[3:]
    values = dict.fromkeys(fields, '0')
    values.update(환율='', 거래대금='40,000', 정산금액='12,345', 잔액=prefix + '012,345.67')
    row = dict(header={'columns': COLUMNS}, fx_cell_blank=True, native=[],
               body=' '.join(values[f] for f in fields if f not in {'환율', '잔고'}))
    base, _ = module().TossFlowParser()._amount_columns(row)
    assert base['정산금액'] == 12345 and base['잔고'] == 0
    assert str(base['잔액']) == '12345.67'
