"""戦略とバックテストのテスト。小さな人工データで、手計算した結果と照合する。"""
import pandas as pd
import pytest

from stockalert.backtest import Costs, cagr, max_drawdown, run_backtest
from stockalert.strategy import StrategyParams, position_size, rank_candidates

# 2024-01-05(金) | 08(月) 09(火) 10(水) 11(木) 12(金) | 15(月) 16(火)
DATES = ["2024-01-05", "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12", "2024-01-15", "2024-01-16"]
NO_COST = Costs(commission=0.0, slippage=0.0)
PARAMS = StrategyParams(top_n=1, min_turnover=0, lot_size=1)


def make_panels(symbols: dict) -> dict:
    """symbols: {銘柄: {項目: 値 or 日ごとのリスト}}。省略した項目は「買い条件を満たす」既定値。"""
    base = {"adj_open": 110.0, "adj_high": 112.0, "adj_low": 108.0, "adj_close": 110.0, "close": 110.0,
            "sma50": 100.0, "sma200": 90.0, "atr14": 5.0, "mom_6_1": 0.2, "mom_12_1": 0.2, "avg_turnover20": 1e9}
    panels = {}
    for f, default in base.items():
        cols = {}
        for sym, over in symbols.items():
            v = over.get(f, default)
            cols[sym] = v if isinstance(v, list) else [v] * len(DATES)
        panels[f] = pd.DataFrame(cols, index=DATES)
    return panels


def test_rank_candidates_filters_and_orders():
    snap = pd.DataFrame(
        {"close": [110, 110, 110, 80, 110, 95], "sma50": [100, 100, 100, 100, 85, 100],
         "sma200": [90, 90, 90, 90, 90, 90], "mom_6_1": [0.1, 0.3, 0.5, 0.9, 0.9, 0.9],
         "avg_turnover20": [1e9, 1e9, 1e6, 1e9, 1e9, 1e9], "atr14": [5.0] * 6},
        index=["A", "B", "LOWLIQ", "BELOW200", "SMA50_BELOW_200", "BELOW50"],
    )
    assert rank_candidates(snap, StrategyParams(min_turnover=1e8)) == ["B", "A"]


def test_position_size_risks_one_percent():
    p = StrategyParams(lot_size=100)
    # 資金300万円の1% = 3万円。ATR=70円 → 損切り幅140円 → 214.2株 → 100株単位で200株
    assert position_size(3_000_000, 70, p) == 200
    assert position_size(3_000_000, 2000, p) == 0  # 値動きが大きすぎて1単元も買えない


def test_entry_at_next_open_and_stop_loss_costs_about_one_percent():
    low = [108.0, 108.0, 108.0, 99.0, 108.0, 108.0, 108.0, 108.0]  # 水曜に安値99
    opn = [110.0, 110.0, 111.0, 110.0, 110.0, 110.0, 110.0, 110.0]  # 火曜の始値111
    res = run_backtest(make_panels({"A": {"adj_low": low, "adj_open": opn}}), PARAMS, NO_COST,
                       DATES[0], DATES[4], 10_000)
    # 月曜の終値で判断 → 火曜の始値111で 10000×1%÷(2×5)=10株 買う。損切り = 111−10 = 101
    # 水曜に安値99 → 101で決済。損失 = 10株×10 = 100（資金の1%）
    assert len(res.trades) == 1
    t = res.trades[0]
    assert (t["entry_date"], t["exit_date"], t["reason"]) == ("2024-01-09", "2024-01-10", "stop")
    assert t["pnl"] == pytest.approx(-100.0)
    assert res.equity["2024-01-08"] == 10_000          # シグナル当日はまだ買っていない
    assert res.equity["2024-01-09"] == pytest.approx(10_000 - 10 * 1)  # 111で買って終値110
    assert res.equity["2024-01-11"] == pytest.approx(9_900)


def test_gap_down_exits_at_open_not_at_stop():
    opn = [110.0, 110.0, 110.0, 90.0, 110.0, 110.0, 110.0, 110.0]
    low = [108.0, 108.0, 108.0, 88.0, 108.0, 108.0, 108.0, 108.0]
    res = run_backtest(make_panels({"A": {"adj_open": opn, "adj_low": low}}), PARAMS, NO_COST,
                       DATES[0], DATES[4], 10_000)
    # 損切り100だが水曜は90で寄り付いた → 90でしか売れない。損失 = 10株×20 = 200
    assert res.trades[0]["pnl"] == pytest.approx(-200.0)


def test_exit_when_close_below_sma50_executes_next_open():
    close = [110.0, 110.0, 110.0, 104.0, 106.0, 106.0, 106.0, 106.0]
    sma50 = [100.0, 100.0, 100.0, 105.0, 105.0, 105.0, 105.0, 105.0]  # 水曜に終値 < SMA50
    opn = [110.0, 110.0, 110.0, 110.0, 107.0, 110.0, 110.0, 110.0]
    low = [103.0] * 8
    res = run_backtest(make_panels({"A": {"adj_close": close, "close": close, "sma50": sma50,
                                          "adj_open": opn, "adj_low": low}}),
                       PARAMS, NO_COST, DATES[0], DATES[5], 10_000)
    t = res.trades[0]
    assert (t["exit_date"], t["reason"]) == ("2024-01-11", "below_sma")  # 木曜の始値107で売る
    assert t["pnl"] == pytest.approx(10 * (107 - 110))


def test_weekly_rebalance_sells_rank_out_and_buys_new_leader():
    mom_a = [0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.1, 0.1]
    mom_b = [0.2] * 8  # 2週目の月曜に B が A を逆転
    res = run_backtest(make_panels({"A": {"mom_6_1": mom_a}, "B": {"mom_6_1": mom_b}}),
                       PARAMS, NO_COST, DATES[0], DATES[7], 10_000)
    assert [(t["symbol"], t["exit_date"], t["reason"]) for t in res.trades] == [("A", "2024-01-16", "rank_out")]
    assert res.open_positions == 1  # 火曜に B を買っている


def test_costs_reduce_pnl():
    costs = Costs(commission=0.01, slippage=0.01)
    res = run_backtest(make_panels({"A": {"mom_6_1": [0.3] * 6 + [float("nan")] * 2}}), PARAMS, costs,
                       DATES[0], DATES[7], 10_000)
    # 110で10株買い → 110で売り。価格は同じでも往復コスト分だけ負ける
    buy = 10 * 110 * 1.01 * 1.01
    sell = 10 * 110 * 0.99 * 0.99
    assert res.trades[0]["pnl"] == pytest.approx(sell - buy)


def test_lot_size_and_cash_limit():
    # 100株単位: 資金10万円 → 1%÷(2×5)=100株 だが 110円×100株=11,000円は買える
    res = run_backtest(make_panels({"A": {}}), StrategyParams(top_n=1, lot_size=100), NO_COST,
                       DATES[0], DATES[3], 100_000)
    assert res.open_positions == 1 and res.equity.iloc[-1] == pytest.approx(100_000)
    # 資金5,000円では1単元(11,000円)が買えない → 見送り
    res = run_backtest(make_panels({"A": {}}), StrategyParams(top_n=1, lot_size=100), NO_COST,
                       DATES[0], DATES[3], 5_000)
    assert res.open_positions == 0 and res.skipped_buys == 1


def test_cagr_and_max_drawdown_by_hand():
    s = pd.Series([100.0, 120.0, 90.0, 121.0], index=["2022-01-01", "2022-07-01", "2023-01-01", "2024-01-01"])
    assert cagr(s) == pytest.approx(0.1, abs=1e-3)       # 2年で1.21倍 = 年率10%
    assert max_drawdown(s) == pytest.approx(90 / 120 - 1)  # 120 → 90 で −25%


def test_hold_rank_keeps_position_that_slipped_slightly():
    """上位1で買い、2位以内なら持ち続ける設定では、順位が1つ落ちただけでは売らない。"""
    mom_a = [0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.1, 0.1]
    mom_b = [0.2] * 8  # 2週目の月曜に B が A を逆転
    params = StrategyParams(top_n=1, hold_rank=2, lot_size=1)
    res = run_backtest(make_panels({"A": {"mom_6_1": mom_a}, "B": {"mom_6_1": mom_b}}),
                       params, NO_COST, DATES[0], DATES[7], 10_000)
    assert res.trades == [] and res.open_positions == 1  # A を持ったまま。枠は1なので B は買わない


def test_monthly_rebalance_and_exit_sma_option():
    from stockalert.backtest import is_rebalance_day

    assert is_rebalance_day("2024-01-31", "2024-02-01", "monthly")      # 2/1 は月の最初の営業日
    assert not is_rebalance_day("2024-01-26", "2024-01-29", "monthly")  # 週の最初だが月の最初ではない
    assert is_rebalance_day("2024-01-26", "2024-01-29", "weekly")       # 1/29(月) は週の最初の営業日
    assert not is_rebalance_day("2024-01-29", "2024-01-30", "weekly")

    # 終値104は SMA50(105) を下回るが SMA200(90) より上 → exit_sma="sma200" なら売らない
    close = [110.0, 110.0, 110.0, 104.0, 106.0, 106.0, 106.0, 106.0]
    sma50 = [100.0, 100.0, 100.0, 105.0, 105.0, 105.0, 105.0, 105.0]
    panels = make_panels({"A": {"adj_close": close, "close": close, "sma50": sma50, "adj_low": [103.0] * 8}})
    res = run_backtest(panels, StrategyParams(top_n=1, exit_sma="sma200"), NO_COST, DATES[0], DATES[5], 10_000)
    assert res.trades == [] and res.open_positions == 1
