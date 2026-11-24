"""Line-by-line comparison of a real bill against the app's own numbers for the same cycle, and a
check of the bill's rates against the ones the app has been using.

Pure: the bill is a parsed `ParsedBill`, the app's side is built from the cycle's integrated
intervals. Nothing here touches the network, the DB or the state file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .tou import (BASE_SERVICE_DAILY, TouPeriod, _current_adder, _gen_rate, base_service_cost, export_rate_at,
                  period_at, rate_at)

_KEYS = {TouPeriod.ON_PEAK: "on_peak", TouPeriod.OFF_PEAK: "off_peak", TouPeriod.SUPER_OFF_PEAK: "super_off_peak"}
_PERIOD_OF = {v: k for k, v in _KEYS.items()}
_KWH_TOLERANCE_PCT = 3.0        # SDG&E's meter and the gateway never agree exactly
_RATE_TOLERANCE_PCT = 3.0
_GEN_RATE_TOLERANCE_PCT = 2.0
_FIXED_TOLERANCE = 0.50         # $
_ADDER_TOLERANCE = 0.004        # $/kWh
_SCALE_TOLERANCE_PCT = 3.0


@dataclass
class ModelSide:
    """What the app's own model says the cycle came to, from the gateway's readings."""
    import_by_period: dict[str, float]
    import_kwh: float
    export_kwh: float
    import_cost: float
    export_credit: float
    base: float
    net: float


def model_side(intervals, days: float) -> ModelSide:
    """Price a cycle's (dt, hours, grid_kw, ...) intervals the way the app does everywhere else."""
    by = {"on_peak": 0.0, "off_peak": 0.0, "super_off_peak": 0.0}
    imp_cost = exp_credit = exp_kwh = 0.0
    for dt, hours, grid_kw, *_rest in intervals:
        if grid_kw > 0:
            kwh = grid_kw * hours
            by[_KEYS[period_at(dt)]] += kwh
            imp_cost += kwh * rate_at(dt)
        elif grid_kw < 0:
            kwh = -grid_kw * hours
            exp_kwh += kwh
            exp_credit += kwh * export_rate_at(dt)
    base = base_service_cost(days)
    return ModelSide(import_by_period={k: round(v, 3) for k, v in by.items()}, import_kwh=round(sum(by.values()), 3),
                     export_kwh=round(exp_kwh, 3), import_cost=round(imp_cost, 2), export_credit=round(exp_credit, 2),
                     base=round(base, 2), net=round(imp_cost - exp_credit + base, 2))


@dataclass
class Line:
    key: str
    label: str
    unit: str            # "kWh" or "$"
    bill: float
    model: float

    @property
    def diff(self) -> float:
        return self.bill - self.model


@dataclass
class Comparison:
    lines: list[Line]
    bill_total: float
    model_total: float
    findings: list[str] = field(default_factory=list)


def _pct(a: float, b: float) -> float:
    """How far `a` is from `b`, as a percent of `b`."""
    return (a - b) / abs(b) * 100.0 if b else 0.0


def compare(bill, model: ModelSide) -> Comparison:
    bill_import_cost = round(bill.delivery_import + bill.generation_charge, 2)
    bill_export = bill.export_credit_total
    other = round(bill.comparable_amount - (bill_import_cost - bill_export + bill.nonnettable), 2)
    lines = [
        Line("import_kwh", "Import kWh", "kWh", round(bill.import_kwh, 2), model.import_kwh),
        Line("export_kwh", "Export kWh", "kWh", round(bill.export_kwh, 2), model.export_kwh),
        Line("import_cost", "Import charges", "$", bill_import_cost, model.import_cost),
        Line("export_credit", "Export credits", "$", bill_export, model.export_credit),
        Line("fixed", "Fixed charges", "$", bill.nonnettable, model.base),
        Line("other", "Other (tax)", "$", other, 0.0),
    ]
    by = {ln.key: ln for ln in lines}
    findings: list[str] = []

    for key, word in (("import_kwh", "import"), ("export_kwh", "export")):
        ln = by[key]
        pct = _pct(ln.model, ln.bill)               # the gateway relative to the utility's meter
        if ln.bill and abs(pct) >= _KWH_TOLERANCE_PCT:
            findings.append(f"SDG&E's meter counted {abs(ln.bill - ln.model):.0f} kWh ({abs(pct):.0f}%) "
                            f"{'more' if ln.bill > ln.model else 'less'} {word} than the gateway: a metering difference.")
    import_kwh_ok = not by["import_kwh"].bill or abs(_pct(by["import_kwh"].model, by["import_kwh"].bill)) < _KWH_TOLERANCE_PCT
    export_kwh_ok = not by["export_kwh"].bill or abs(_pct(by["export_kwh"].model, by["export_kwh"].bill)) < _KWH_TOLERANCE_PCT
    if import_kwh_ok and bill.import_kwh > 0 and model.import_kwh > 0 and model.import_cost > 0:
        b, m = bill_import_cost / bill.import_kwh, model.import_cost / model.import_kwh
        if abs(_pct(b, m)) >= _RATE_TOLERANCE_PCT:
            findings.append(f"Import $/kWh: bill ${b:.3f} vs app ${m:.3f} ({_pct(b, m):+.0f}%): a rate difference, not metering.")
    if export_kwh_ok and bill.export_kwh > 0 and model.export_kwh > 0 and model.export_credit > 0:
        b, m = bill_export / bill.export_kwh, model.export_credit / model.export_kwh
        if abs(_pct(b, m)) >= _RATE_TOLERANCE_PCT:
            findings.append(f"Export credit $/kWh: bill ${b:.3f} vs app ${m:.3f} ({_pct(b, m):+.0f}%): "
                            f"the credit rate differs from the app's, not the kWh.")
    if abs(by["fixed"].diff) >= _FIXED_TOLERANCE:
        findings.append(f"Fixed charges differ by ${abs(by['fixed'].diff):.2f} (bill ${bill.nonnettable:.2f}, app ${model.base:.2f}).")
    if abs(other) >= 1.0:
        findings.append(f"${abs(other):.2f} of the bill is outside the model (taxes, rounding or a charge it has no term for).")
    if not findings:
        findings.append("Everything lines up within a few percent.")
    return Comparison(lines=lines, bill_total=bill.comparable_amount, model_total=model.net, findings=findings)


def _usd(x: float) -> str:
    return f"-${abs(x):.2f}" if x < 0 else f"${x:.2f}"


def format_comparison(cmp_: Comparison) -> str:
    """Aligned plain text: bill, app, and the gap (bill minus app) for every line."""
    rows = [f"{'':<16}{'bill':>10}{'app':>10}{'gap':>9}"]
    for ln in cmp_.lines:
        if ln.key == "other" and abs(ln.bill) < 0.005:
            continue
        fmt = (lambda v: f"{v:.1f}") if ln.unit == "kWh" else _usd
        gap = f"{ln.diff:+.1f}" if ln.unit == "kWh" else (f"+{_usd(ln.diff)}" if ln.diff >= 0 else _usd(ln.diff))
        rows.append(f"{ln.label:<16}{fmt(ln.bill):>10}{fmt(ln.model):>10}{gap:>9}")
    gap = cmp_.bill_total - cmp_.model_total
    rows.append(f"{'Total':<16}{_usd(cmp_.bill_total):>10}{_usd(cmp_.model_total):>10}"
                f"{('+' if gap >= 0 else '') + _usd(gap):>9}")
    return "\n".join(rows)


def rate_changes(bill, new_scale: dict | None = None, prior_scale: dict | None = None,
                 check_delivery: bool = True) -> list[str]:
    """Where the bill's rates differ from the ones the app is using now, so a tariff revision shows
    up the cycle it happens instead of silently skewing every estimate. Call BEFORE the bill's
    values are learned, or there is nothing left to differ from."""
    out: list[str] = []
    for season, rates in bill.gen_rates_by_season.items():
        for period, rate in rates.items():
            used = _gen_rate(season, _PERIOD_OF[period])
            if used and abs(_pct(rate, used)) > _GEN_RATE_TOLERANCE_PCT:
                out.append(f"SDCP {period.replace('_', '-')} generation ({season}): bill ${rate:.5f}/kWh vs app "
                           f"${used:.5f}/kWh ({_pct(rate, used):+.0f}%)")
    daily, used_daily = bill.base_daily, base_service_cost(1.0) or BASE_SERVICE_DAILY
    if used_daily and abs(_pct(daily, used_daily)) > _GEN_RATE_TOLERANCE_PCT:
        out.append(f"Fixed charges: bill ${daily:.4f}/day vs app ${used_daily:.4f}/day ({_pct(daily, used_daily):+.0f}%)")
    adder = bill.implied_pcia_adder if check_delivery else None
    if adder is not None and abs(adder - _current_adder()) > _ADDER_TOLERANCE:
        out.append(f"Delivery import (PCIA etc.): bill implies ${adder:.4f}/kWh over the tariff table vs app "
                   f"${_current_adder():.4f}/kWh")
    if new_scale and prior_scale and prior_scale.get("scale") and new_scale.get("scale"):
        new, old = float(new_scale["scale"]), float(prior_scale["scale"])
        if abs(_pct(new, old)) > _SCALE_TOLERANCE_PCT:
            out.append(f"Export credits vs the hourly schedule: scale {old:.3f} before, {new:.2f} on this bill")
    return out
