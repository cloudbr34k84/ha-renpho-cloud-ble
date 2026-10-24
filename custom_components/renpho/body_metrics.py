"""Body-composition math for Renpho / QingNiu / LeFu scales.

Ported from renpho-escs20m 0.8.0 (``body_metrics.py``, MIT, (c) Ron —
https://github.com/ronnnnnnnnnnnnn). The scale only reports weight and
impedance; everything else is derived here from the person's profile.
"""

from __future__ import annotations

from datetime import date
from math import floor

MALE = 0
FEMALE = 1


def _round(x: float, ndigits: int) -> float:
    scale = 10**ndigits
    return floor(x * scale + 0.5) / scale


def age_on(birthday: date, today: date) -> int:
    years = today.year - birthday.year
    if (today.month, today.day) < (birthday.month, birthday.day):
        years -= 1
    return max(years, 0)


_ALGO_0X04 = {
    (MALE, False): (1.524, 0.103, -21.992),
    (FEMALE, False): (1.545, 0.097, -12.689),
    (MALE, True): (0.7678, 0.0292, -6.5417),
    (FEMALE, True): (0.9310, 0.0326, -4.5527),
}
_ALGO_0X03_NONATH = {
    MALE: (0.0009, 0.392, -0.00095, -0.0693, 2.877),
    FEMALE: (0.00089, 0.39, -0.001, -0.08, -3.3 + 1.662),
}
_ALGO_0X03_ATH = {
    (MALE, True): (-0.0088225, 1.1402243, 0.023917, 0.003917, -0.004927, -7.809911),
    (MALE, False): (-0.027341, 2.0040585, 0.0282436, 0.02382, -0.019248, -17.06006),
    (FEMALE, True): (-0.0060424, 0.999226, 0.03461369, 0.0179066, -0.044369, 5.04487),
    (FEMALE, False): (-0.02172, 1.62807, 0.045364, 0.0857724, -0.09616912, 2.5939906),
}


def calculate_body_fat(
    weight_kg: float,
    height_m: float,
    age: int,
    sex: int,
    resistance: int,
    *,
    algorithm: int = 0x04,
    athlete: bool = False,
) -> float:
    """Approximate the scale firmware's body-fat % from weight + impedance."""
    bmi = weight_kg / (height_m**2)
    height_cm = height_m * 100
    if algorithm == 0x04:
        c_bmi, c_age, c_int = _ALGO_0X04[(sex, athlete)]
        bf = c_bmi * bmi + c_age * age + c_int
        if not athlete:
            bf -= 500.0 / resistance
        return _round(bf, 1)
    if not athlete:
        c_h2, c_w, c_r, c_age, c_int = _ALGO_0X03_NONATH[sex]
        lbm = c_h2 * height_cm**2 + c_w * weight_kg + c_r * resistance + c_age * age + c_int
        return _round((weight_kg - lbm) / weight_kg * 100, 1)
    c_bmi2, c_bmi, c_age, c_w, c_h, c_int = _ALGO_0X03_ATH[(sex, bmi >= 25)]
    return _round(
        c_bmi2 * bmi * bmi + c_bmi * bmi + c_age * age + c_w * weight_kg + c_h * height_cm + c_int,
        1,
    )


def derive_metrics(
    weight_kg: float, height_m: float, sex: int, body_fat: float | None
) -> dict[str, float]:
    """Return BMI plus, when body fat is known, the derived composition panel."""
    out: dict[str, float] = {"bmi": _round(weight_kg / (height_m**2), 1)}
    if body_fat is None:
        return out
    bf = body_fat
    out["body_fat"] = bf
    out["fat_free_mass"] = max(5.0, min(200.0, _round(weight_kg * (100.0 - bf) / 100.0, 2)))
    out["body_water"] = max(20.0, min(80.0, _round([72.202, 68.651][sex] + [-0.72223, -0.68725][sex] * bf, 1)))
    out["skeletal_muscle"] = max(17.5, min(70.0, _round([64.713, 58.390][sex] + [-0.65508, -0.58654][sex] * bf, 1)))
    soft_lean_pct = _round([94.992, 93.988][sex] + [-0.94969, -0.93960][sex] * bf, 1)
    soft_lean_kg = max(3.75, min(110.0, _round(weight_kg * soft_lean_pct / 100.0, 2)))
    bf_kg = bf * weight_kg / 100.0
    bone = max(1.0, min(7.0, _round(weight_kg - soft_lean_kg - bf_kg, 2)))
    out["bone_mass"] = bone
    out["muscle_mass"] = _round(weight_kg - bone - bf_kg, 2)
    out["protein"] = max(5.0, min(24.0, _round([22.787, 25.340][sex] + [-0.22735, -0.30245][sex] * bf, 1)))
    out["bmr"] = max(900, min(2500, int(_round([372.7023, 370.5818][sex] + [430.9015, 359.6167][sex] * bone, 0))))
    return out
