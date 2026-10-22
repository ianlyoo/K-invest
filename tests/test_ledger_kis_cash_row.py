"""A foreign cash row KIS closes after unsettled trades is not booked as a withdrawal.

Synthetic captures in the stored kis_capture shape (test_kis_flow_inference's builder plus the
``cash_basis`` / ``overseas.cash_check`` fields kis_nav_capture stores) through the real
infer_kis_flows path. Synthetic amounts only.
"""
from test_kis_flow_inference import DAYS, account, by_currency, infer


def capture(day, *, usd, basis, unsettled):
    entry = account(day, usd=usd)
    entry['cash_basis'] = {'KRW': 'TTTC8434R.output2.prvs_rcdl_excc_amt (D+2)', 'USD': basis}
    entry['overseas'] = {'cash_check': {'check': 'CTRP6504R.output3.frcr_evlu_tota', 'unsettled_trades': unsettled,
                                        'decided': 'frcr_dncl_amt_2', 'reason': None, 'advisories': []}}
    return entry


def test_a_cash_row_closed_after_unsettled_trades_blocks_the_interval():
    records = by_currency(infer(capture(DAYS[0], usd='100', basis='frcr_dncl_amt_2', unsettled=True),
                                capture(DAYS[1], usd='0', basis='no_cash_row_zero', unsettled=False)))
    usd, krw = records['USD'], records['KRW']
    assert usd['flow_native'] is None and usd['confidence'] == 'unresolved' and usd['events'] == []
    assert 'KIS_CASH_ROW_CLOSED_AFTER_SETTLEMENT' in usd['reasons']
    assert usd['diagnostics']['classification'] == 'unresolved' and usd['diagnostics']['residual_native'] == '-100'
    assert krw['flow_native'] is None and 'CROSS_CURRENCY_RESIDUAL_UNRESOLVED' in krw['reasons']


def test_without_pending_trades_or_with_the_row_kept_the_residual_rule_is_unchanged():
    settled = by_currency(infer(capture(DAYS[0], usd='100', basis='frcr_dncl_amt_2', unsettled=False),
                                capture(DAYS[1], usd='0', basis='no_cash_row_zero', unsettled=False)))
    assert settled['USD']['diagnostics']['classification'] == 'withdrawal'
    assert settled['USD']['flow_native'] == '-100'
    kept = by_currency(infer(capture(DAYS[0], usd='100', basis='frcr_dncl_amt_2', unsettled=True),
                             capture(DAYS[1], usd='100', basis='frcr_dncl_amt_2', unsettled=False)))
    assert kept['USD']['flow_native'] == '0' and kept['USD']['reasons'] == ['DIVIDENDS_COUNTED_AS_FLOWS']
