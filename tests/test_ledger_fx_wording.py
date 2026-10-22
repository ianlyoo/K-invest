"""The return tools' FX wording follows fx.policy (Toss rate, yfinance labelled fallback).

Live get_portfolio_performance runs the real PortfolioCollector.build_fx over the scripted Toss
GET and stub yfinance (test_pos_fx_policy); replay and history use test_ledger_returns' wired
synthetic store. Synthetic rates and values only.
"""
import json

from test_ledger_returns import wired  # noqa: F401 - pytest fixture
from test_pos_fx_policy import FakeToss, _collector, _holdings, _yfinance

from portfolio_model import FX_POLICY, FX_VALUATION_NOTE
from portfolio_sources import Collection

STORED = "stored snapshot NAVs keep the FX each snapshot was valued at"
PRE_F4 = "current spot rate (yfinance <CUR>KRW=X)"


class LiveCollector:
    """Synthetic positions and an empty ledger; FX from the real collector over a scripted Toss."""

    def __init__(self, toss):
        self.fx_collector = _collector(toss)

    def collect_positions(self, *args):
        return Collection(holdings=_holdings(), health={'synthetic': {'status': 'ok'}},
                          fetched_at='2026-01-01T09:10:05+00:00')

    def collect_transactions(self, *args):
        return Collection(health={'synthetic': {'status': 'ok'}})

    def build_fx(self, currencies):
        return self.fx_collector.build_fx(currencies)


def live(wired, monkeypatch, toss, **kwargs):  # noqa: F811
    monkeypatch.setattr(wired.server, '_get_collector', lambda: LiveCollector(toss))
    envelope = wired.server.get_portfolio_performance(**kwargs)
    assert envelope['ok']
    return envelope['data']


def test_live_performance_names_the_toss_policy_and_carries_the_fx_it_used(wired, monkeypatch):  # noqa: F811
    yf_calls = _yfinance(monkeypatch)
    data = live(wired, monkeypatch, FakeToss(), detail_level='full')
    methodology = data['methodology']
    assert methodology['fx_valuation'] == FX_VALUATION_NOTE
    assert methodology['fx_stored_nav'].startswith(STORED)
    # The pointer resolves: the response carries the policy and the per-currency source it names.
    assert data['fx']['policy'] == FX_POLICY and data['fx']['sources']['USD']['source'] == 'toss'
    assert yf_calls == [] and PRE_F4 not in json.dumps(data)
    # The default (summary) body keeps the fx block too.
    assert live(wired, monkeypatch, FakeToss())['fx']['policy']['name'] == 'toss_rate_v1'


def test_live_performance_labels_a_yfinance_fallback(wired, monkeypatch):  # noqa: F811
    yf_calls = _yfinance(monkeypatch)
    data = live(wired, monkeypatch, FakeToss(RuntimeError('toss down')), detail_level='full')
    assert yf_calls == ['USDKRW=X']
    assert data['fx']['sources']['USD']['source'] == 'yfinance' and data['fx']['sources']['USD']['fallback'] is True
    assert data['methodology']['fx_valuation'] == FX_VALUATION_NOTE


def test_replay_and_history_state_stored_snapshot_fx_never_live(wired):  # noqa: F811
    replay = wired.server.get_portfolio_performance(input_snapshot_ids=['synthetic'], detail_level='full')['data']
    assert replay['methodology']['fx_valuation'].startswith('no live FX in snapshot replay; ' + STORED)
    assert 'fx' not in replay  # nothing is converted at a live rate in replay
    history = wired.server.get_portfolio_history(input_snapshot_ids=['synthetic'], detail_level='full')['data']
    assert history['methodology']['fx_valuation'].startswith(STORED)
    for data in (replay, history):
        assert 'fx.policy' in data['methodology']['fx_valuation'] and PRE_F4 not in json.dumps(data)
