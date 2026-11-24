"""Line-by-line comparison of a real bill against the app's own numbers for the same cycle, plus
a warning when the bill's rates differ from what the app has been using.

Fixture: the Aug 19 - Sep 17 2026 bill (redacted; same one test_unit.py parses).
"""

from __future__ import annotations

from datetime import datetime

import pytest

from franklinwh_scraper import billcompare, tou
from franklinwh_scraper.billparse import parse_bill_text

SEP_BILL = """\
Electric Service - Solar Billing Plan
Rate: Time of Use - EVTOU5-Residential Climate Zone: Inland
System Size: 3.484 kW
Export Pricing: Legacy 2024 Pricing
Billing Period: 8/19/26 - 9/17/26 Total Days: 30
Meter Number: 00000000 (Next scheduled read date Oct 16, 2026) Cycle: 12
ELECTRIC CHARGES AND CREDITS
Electricity Import kWh Current Charges Summary
Total Import 189
On-Peak 3
Off-Peak 14
Super Off-Peak 172
Non-Nettable Charges $24.36
Delivery Import Charges $15.86
Electricity Export Applied Credits
Total Export kWh −126
Delivery Export Credits -$30.17 Delivery Export Credits -$30.17
Total Electric Service $10.05
Community Choice Aggregation (CCA) Electric Generation Charges
Bill Date: Sep 17, 2026 Billing Period: 8/19/26 - 9/17/26
Amount($)
Generation On-Peak Summer 3 kWh X $0.38242 .97
Generation Off-Peak Summer 14 kWh X $0.11828 1.63
Generation Super Off-Peak Summer 172 kWh X $0.0368 6.33
Generation Electricity Export Credits -126 kWh X $0.26621 -33.63
Generation Electricity Export Credits Adder -126 kWh X $0.0075 -.95
Credited to SBP Balance 25.63
State Surcharge Tax .02
Total CCA Electric Generation Charges $.00
Your CCA rate is SBP EV-TOU-5 - 2021 Vintage - PowerBase.
Your cumulative SBP Balance credit is now $105.47.
OTHER CHARGES & CREDITS Amount($)
California Climate Credit -49.36
Total Other Charges & Credits -$49.36
Total Current Charges -$39.31
"""


@pytest.fixture(autouse=True)
def _default_rates():
    tou.set_learned_import(None)
    tou.set_learned_export_scale(None, None)
    yield
    tou.set_learned_import(None)
    tou.set_learned_export_scale(None, None)


# ---------------------------------------------------------------- the parser's new fields

def test_parser_reads_the_sbp_credit_bank_and_generation_charge():
    b = parse_bill_text(SEP_BILL)
    assert b.sbp_credited == 25.63 and b.sbp_balance == 105.47
    assert b.generation_charge == pytest.approx(0.97 + 1.63 + 6.33, abs=0.005)


def test_a_bill_without_an_sbp_line_has_none_not_zero():
    text = SEP_BILL.replace("Credited to SBP Balance 25.63\n", "").replace(
        "Your cumulative SBP Balance credit is now $105.47.\n", "")
    b = parse_bill_text(text)
    assert b.sbp_credited is None and b.sbp_balance is None


# ---------------------------------------------------------------- the model's side of the cycle

def _iv(day, hour, grid_kw, hours=1.0):
    return (datetime(2026, 9, day, hour), hours, grid_kw, 1.0, 0.0)


def test_model_side_splits_import_by_period_and_prices_each_interval():
    # Wed Sep 2: 17:00 is on-peak, 08:00 is off-peak, 02:00 is super-off-peak; the sun exports at 13:00.
    ivs = [_iv(2, 17, 2.0), _iv(2, 8, 1.5), _iv(2, 2, 4.0), _iv(2, 13, -3.0)]
    m = billcompare.model_side(ivs, days=30)
    assert m.import_by_period == {"on_peak": 2.0, "off_peak": 1.5, "super_off_peak": 4.0}
    assert m.import_kwh == 7.5 and m.export_kwh == 3.0
    assert m.import_cost == pytest.approx(2.0 * tou.rate_at(datetime(2026, 9, 2, 17)) + 1.5 * tou.rate_at(datetime(2026, 9, 2, 8))
                                          + 4.0 * tou.rate_at(datetime(2026, 9, 2, 2)), abs=0.01)
    assert m.export_credit == pytest.approx(3.0 * tou.export_rate_at(datetime(2026, 9, 2, 13)), abs=0.01)
    assert m.base == pytest.approx(tou.base_service_cost(30), abs=0.01)
    assert m.net == pytest.approx(m.import_cost - m.export_credit + m.base, abs=0.01)


# ---------------------------------------------------------------- the comparison

def _model(**kw):
    base = dict(import_by_period={"on_peak": 3.0, "off_peak": 14.0, "super_off_peak": 172.0}, import_kwh=189.0,
                export_kwh=126.0, import_cost=0.0, export_credit=0.0, base=24.36, net=0.0)
    base.update(kw)
    return billcompare.ModelSide(**base)


def test_comparison_lines_carry_bill_model_and_diff():
    bill = parse_bill_text(SEP_BILL)
    model = _model(import_cost=24.0, export_credit=62.0, net=24.0 - 62.0 + 24.36)
    cmp_ = billcompare.compare(bill, model)
    by = {ln.key: ln for ln in cmp_.lines}
    assert by["import_kwh"].bill == pytest.approx(188.32, abs=0.05) and by["import_kwh"].model == 189.0
    assert by["export_kwh"].bill == 126.0
    assert by["import_cost"].bill == pytest.approx(15.86 + 0.97 + 1.63 + 6.33, abs=0.01)     # delivery + generation
    assert by["export_credit"].bill == pytest.approx(64.75, abs=0.01)
    assert by["fixed"].bill == 24.36 and by["fixed"].model == 24.36 and by["fixed"].diff == 0
    assert all(ln.diff == pytest.approx(ln.bill - ln.model, abs=0.005) for ln in cmp_.lines)
    assert cmp_.bill_total == pytest.approx(-15.58, abs=0.01)
    assert cmp_.model_total == pytest.approx(model.net, abs=0.01)


def test_the_other_line_carries_what_the_model_has_no_term_for():
    """Tax and rounding: whatever is left between the bill's total and its itemised lines."""
    bill = parse_bill_text(SEP_BILL)
    cmp_ = billcompare.compare(bill, _model(net=0.0))
    other = next(ln for ln in cmp_.lines if ln.key == "other")
    itemised = (15.86 + 0.97 + 1.63 + 6.33) - 64.75 + 24.36
    assert other.bill == pytest.approx(bill.comparable_amount - itemised, abs=0.01)
    assert abs(other.bill) < 0.1


def test_metering_gap_is_called_out_when_kwh_disagree():
    bill = parse_bill_text(SEP_BILL)
    cmp_ = billcompare.compare(bill, _model(import_kwh=170.0, import_by_period={"on_peak": 3.0, "off_peak": 10.0, "super_off_peak": 157.0}))
    text = " ".join(cmp_.findings)
    assert "import" in text.lower() and "meter" in text.lower() and "%" in text


def test_a_rate_gap_is_called_out_when_the_kwh_agree_but_the_dollars_do_not():
    bill = parse_bill_text(SEP_BILL)
    cmp_ = billcompare.compare(bill, _model(import_cost=30.0, export_credit=64.75, net=0.0))
    text = " ".join(cmp_.findings)
    assert "$/kWh" in text and "import" in text.lower()


def test_a_close_match_says_so_instead_of_inventing_a_cause():
    bill = parse_bill_text(SEP_BILL)
    bill_import = 15.86 + 0.97 + 1.63 + 6.33
    model = _model(import_cost=bill_import, export_credit=64.75, net=bill.comparable_amount)
    cmp_ = billcompare.compare(bill, model)
    assert cmp_.findings == ["Everything lines up within a few percent."]


def test_the_table_is_plain_aligned_text():
    bill = parse_bill_text(SEP_BILL)
    text = billcompare.format_comparison(billcompare.compare(bill, _model(import_cost=24.0, export_credit=62.0, net=-14.0)))
    assert "Import kWh" in text and "Export kWh" in text and "Fixed charges" in text and "Total" in text
    assert text.splitlines()[0].lstrip().startswith("bill") or "bill" in text.splitlines()[0].lower()


# ---------------------------------------------------------------- rate changes

def test_no_rate_change_when_the_bill_matches_what_the_app_uses():
    assert billcompare.rate_changes(parse_bill_text(SEP_BILL)) == []


def test_a_higher_generation_rate_is_flagged_with_both_numbers():
    higher = SEP_BILL.replace("$0.38242 .97", "$0.42000 1.07")
    changes = billcompare.rate_changes(parse_bill_text(higher))
    assert any("on-peak" in c.lower() and "0.42" in c and "0.38242" in c for c in changes), changes


def test_a_changed_fixed_charge_is_flagged():
    changes = billcompare.rate_changes(parse_bill_text(SEP_BILL.replace("$24.36", "$27.00")))
    assert any("fixed" in c.lower() for c in changes), changes


def test_an_export_scale_that_moved_is_flagged_against_the_previous_one():
    bill = parse_bill_text(SEP_BILL)
    changes = billcompare.rate_changes(bill, new_scale={"scale": 1.20}, prior_scale={"scale": 1.035})
    assert any("export" in c.lower() and "1.20" in c for c in changes), changes
    assert billcompare.rate_changes(bill, new_scale={"scale": 1.04}, prior_scale={"scale": 1.035}) == []


def test_delivery_changes_can_be_skipped_for_a_bill_from_before_the_tariff():
    bill = parse_bill_text(SEP_BILL.replace("$15.86", "$30.00"))
    assert any("delivery" in c.lower() for c in billcompare.rate_changes(bill))
    assert not any("delivery" in c.lower() for c in billcompare.rate_changes(bill, check_delivery=False))
