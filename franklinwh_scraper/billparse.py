"""Parse the text of an SDG&E / SDCP solar-billing-plan bill.

The app models the bill (TOU import rates, export credits, fixed fee, cycle
dates) and every one of those drifts from reality as SDG&E/SDCP revise
rates and the meter read date moves. Pasting the bill's text in gives the
ground truth; `bill-record --from-text` uses this to recalibrate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

from .tou import DELIVERY_ON_OFF, DELIVERY_SUPER_OFF

_PERIODS = {"on-peak": "on_peak", "off-peak": "off_peak", "super off-peak": "super_off_peak"}

# A dollar/number token as SDG&E prints them: "-$30.17", ".97", "$.00", "1,234.50",
# with an ASCII or unicode minus.
_NUM = r"[-−]?\$?[-−]?(?:\d[\d,]*)?\.?\d+"


class BillParseError(ValueError):
    """The text didn't contain enough recognizable bill lines."""


def _num(tok: str) -> float:
    tok = tok.replace("−", "-").replace("$", "").replace(",", "")
    neg = tok.startswith("-") or tok.endswith("-") or "-" in tok
    return -abs(float(tok.replace("-", "") or 0)) if neg else float(tok)


def _mdy(s: str) -> date:
    return datetime.strptime(s, "%m/%d/%y").date()


@dataclass
class ParsedBill:
    period_start: date
    period_end: date
    days: int
    next_read: date | None
    season: str                                  # "summer" | "winter"
    pcia_vintage: int | None
    gen_rates: dict[str, float]                  # dominant season's SDCP $/kWh actually billed, by period
    gen_kwh: dict[str, float]                    # period -> kWh over ALL seasons (charge / rate; bill prints them rounded)
    export_kwh: float
    delivery_import: float                       # SDG&E "Delivery Import Charges"
    nonnettable: float                           # fixed/non-nettable charges
    delivery_export_credit: float                # positive $
    total_electric_service: float                # SDG&E electric total (after export credit)
    gen_export_credit: float = 0.0               # positive $ (SDCP export credits + adder)
    gen_export_adder_rate: float | None = None   # SDCP "Export Credits Adder" $/kWh
    gen_export_adder_credit: float = 0.0         # positive $ of that adder
    generation_net: float = 0.0                  # SDCP net (negative = credit banked to SBP)
    climate_credit: float = 0.0                  # positive $; NOT part of the usage bill
    gen_rates_by_season: dict[str, dict[str, float]] = field(default_factory=dict)  # a cycle can straddle Oct/Nov
    export_pricing_year: int | None = None       # "Export Pricing: Legacy 2024 Pricing" -> 2024
    notes: list[str] = field(default_factory=list)

    @property
    def comparable_amount(self) -> float:
        """What the app's estimate models: SDG&E electric service plus SDCP's
        net generation (a banked credit counts at face value). Excludes the
        California Climate Credit, which isn't tied to usage."""
        return round(self.total_electric_service + self.generation_net, 2)

    @property
    def export_credit_total(self) -> float:
        return round(self.delivery_export_credit + self.gen_export_credit, 2)

    @property
    def export_credit_ex_adder(self) -> float:
        """Export credits that follow the hourly schedule: delivery + SDCP
        generation credits, without the flat per-kWh adder."""
        return round(self.export_credit_total - self.gen_export_adder_credit, 2)

    @property
    def export_rate(self) -> float | None:
        """Effective $/kWh over all exports this cycle, or None if none."""
        if self.export_kwh <= 0 or self.export_credit_total <= 0:
            return None
        return round(self.export_credit_total / self.export_kwh, 4)

    @property
    def base_daily(self) -> float:
        return round(self.nonnettable / self.days, 5)

    @property
    def import_kwh(self) -> float:
        return sum(self.gen_kwh.values())

    @property
    def implied_pcia_adder(self) -> float | None:
        """Per-imported-kWh residual of the bill's delivery import charge over
        the tariff's UDC + WF-NBC rates (tou.DELIVERY_*) — i.e. PCIA and
        whatever else the tariff table doesn't itemize. Absorbs any delivery
        rate change since the table's date; tou bounds it before use."""
        if self.import_kwh <= 0:
            return None
        on_off = self.gen_kwh.get("on_peak", 0.0) + self.gen_kwh.get("off_peak", 0.0)
        expected = on_off * DELIVERY_ON_OFF + self.gen_kwh.get("super_off_peak", 0.0) * DELIVERY_SUPER_OFF
        return round((self.delivery_import - expected) / self.import_kwh, 5)

    def learned_import(self) -> dict:
        """The `learned_import` state entry (see tou.set_learned_import).
        Every season the bill priced is included, so a cycle that straddles
        the summer/winter change teaches both."""
        out: dict = {"gen_by_season": {k: dict(v) for k, v in self.gen_rates_by_season.items()},
                     "base_daily": self.base_daily}
        if self.implied_pcia_adder is not None:
            out["pcia_adder"] = self.implied_pcia_adder
        return out


def parse_bill_text(text: str) -> ParsedBill:
    """Extract the numbers the app needs from pasted bill text.

    Raises BillParseError naming every required piece it couldn't find, so a
    format change shows up as a clear message instead of wrong numbers.
    """
    missing: list[str] = []

    def find(pattern: str, label: str, flags: int = re.I):
        m = re.search(pattern, text, flags)
        if not m:
            missing.append(label)
        return m

    per = find(r"Billing Period:\s*(\d{1,2}/\d{1,2}/\d{2})\s*-\s*(\d{1,2}/\d{1,2}/\d{2})", "billing period")
    days_m = find(r"Total Days:\s*(\d+)", "total days")
    nxt = re.search(r"Next scheduled read date\s+([A-Za-z]{3,9}\.? \d{1,2}, \d{4})", text, re.I)
    delivery_import = find(rf"Delivery Import Charges\s+({_NUM})", "delivery import charges")
    nonnet = find(rf"Non-Nettable Charges\s+({_NUM})", "non-nettable charges")
    total_es = find(rf"Total Electric Service\s+({_NUM})", "total electric service")
    exp_kwh = re.search(rf"Total Export kWh\s+({_NUM})", text, re.I)
    deliv_exp = re.search(rf"Delivery Export Credits\s+({_NUM})", text, re.I)

    by_season: dict[str, dict[str, float]] = {}
    season_kwh: dict[str, float] = {}
    gen_kwh: dict[str, float] = {}
    gen_charge = 0.0
    for m in re.finditer(
            rf"Generation\s+(On-Peak|Off-Peak|Super Off-Peak)\s+(Summer|Winter)\s+({_NUM})\s*kWh\s*X\s*\$?({_NUM})\s+({_NUM})",
            text, re.I):
        period = _PERIODS[m.group(1).lower()]
        rate, charge = _num(m.group(4)), _num(m.group(5))
        seas = m.group(2).lower()
        kwh = charge / rate if rate > 0 else _num(m.group(3))
        by_season.setdefault(seas, {})[period] = rate
        season_kwh[seas] = season_kwh.get(seas, 0.0) + kwh
        gen_kwh[period] = gen_kwh.get(period, 0.0) + kwh     # accumulate: never overwrite the other season's usage
        gen_charge += charge
    if not by_season:
        missing.append("generation (SDCP) usage lines")
    season = max(season_kwh, key=season_kwh.get) if season_kwh else None
    gen_rates = dict(by_season.get(season, {}))

    if missing:
        raise BillParseError("Couldn't find in the pasted text: " + ", ".join(missing)
                             + ". Paste the full 'Electric Service' and CCA generation pages.")

    delivery_export = abs(_num(deliv_exp.group(1))) if deliv_exp else 0.0

    gen_export = 0.0
    adder_rate, adder_credit = None, 0.0
    gen_export_kwh = 0.0
    for m in re.finditer(rf"Generation Electricity Export Credits( Adder)?\s+({_NUM})\s*kWh\s*X\s*\$?({_NUM})\s+({_NUM})",
                         text, re.I):
        credit = abs(_num(m.group(4)))
        gen_export += credit
        gen_export_kwh = max(gen_export_kwh, abs(_num(m.group(2))))
        if m.group(1):
            adder_rate, adder_credit = _num(m.group(3)), credit
    # Prefer the SDG&E "Total Export kWh" line; fall back to the kWh on SDCP's
    # export-credit lines when a bill layout omits it.
    export_kwh = abs(_num(exp_kwh.group(1))) if exp_kwh else gen_export_kwh
    tax = re.search(rf"State Surcharge Tax\s+({_NUM})", text, re.I)
    generation_net = gen_charge - gen_export + (_num(tax.group(1)) if tax else 0.0)

    vint = re.search(r"(\d{4})\s+Vintage", text, re.I)
    export_year = re.search(r"Export Pricing:\s*Legacy\s+(\d{4})", text, re.I)
    climate = re.search(rf"California Climate Credit\s+({_NUM})", text, re.I)

    return ParsedBill(
        period_start=_mdy(per.group(1)), period_end=_mdy(per.group(2)), days=int(days_m.group(1)),
        next_read=datetime.strptime(nxt.group(1).replace(".", ""), "%b %d, %Y").date() if nxt else None,
        season=season, pcia_vintage=int(vint.group(1)) if vint else None,
        gen_rates=gen_rates, gen_kwh=gen_kwh, export_kwh=export_kwh,
        gen_rates_by_season=by_season, export_pricing_year=int(export_year.group(1)) if export_year else None,
        delivery_import=_num(delivery_import.group(1)), nonnettable=_num(nonnet.group(1)),
        delivery_export_credit=delivery_export, total_electric_service=_num(total_es.group(1)),
        gen_export_credit=round(gen_export, 2), generation_net=round(generation_net, 2),
        gen_export_adder_rate=adder_rate, gen_export_adder_credit=round(adder_credit, 2),
        climate_credit=abs(_num(climate.group(1))) if climate else 0.0,
    )
