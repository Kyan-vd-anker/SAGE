import math
import re

_T0 = 273.15

# Offline fallback, used only when CoolProp is not installed. Liquid at about 1 atm.
# Columns: temperature degC, density kg/m3, cp J/(kg K), dynamic viscosity mPa s, conductivity W/(m K)
_WATER = [
    (0, 999.8, 4217, 1.792, 0.561), (10, 999.7, 4192, 1.306, 0.580), (20, 998.2, 4182, 1.002, 0.598),
    (30, 995.6, 4178, 0.797, 0.615), (40, 992.2, 4179, 0.653, 0.631), (50, 988.0, 4181, 0.547, 0.644),
    (60, 983.2, 4185, 0.467, 0.654), (70, 977.8, 4190, 0.404, 0.663), (80, 971.8, 4197, 0.355, 0.670),
    (90, 965.3, 4205, 0.315, 0.676), (100, 958.4, 4216, 0.282, 0.679),
]
_ETHANOL = [
    (0, 806, 2290, 1.78, 0.173), (20, 789, 2440, 1.20, 0.169), (40, 772, 2590, 0.83, 0.163),
    (60, 754, 2760, 0.59, 0.157), (80, 735, 2950, 0.44, 0.151),
]
_TABLES = {'water': _WATER, 'ethanol': _ETHANOL}


def _round(value):
    return float(f'{value:.6g}')


def _interpolate(table, t_c):
    if t_c < table[0][0] or t_c > table[-1][0]:
        raise ValueError(f'Offline fallback only covers {table[0][0]} to {table[-1][0]} degC for this fluid. Install CoolProp for other conditions.')
    for a, b in zip(table, table[1:]):
        if a[0] <= t_c <= b[0]:
            f = (t_c - a[0]) / (b[0] - a[0])
            rho, cp, k = (a[i] + f * (b[i] - a[i]) for i in (1, 2, 4))
            mu = math.exp(math.log(a[3]) + f * (math.log(b[3]) - math.log(a[3]))) / 1000  # viscosity falls exponentially
            return rho, cp, mu, k
    raise ValueError('Temperature is outside the fallback table')


def _air(t_c, p_pa):
    if not -50 <= t_c <= 600:
        raise ValueError('Offline fallback for air only covers -50 to 600 degC. Install CoolProp for other conditions.')
    t = t_c + _T0
    rho = p_pa * 0.028964 / (8.314462 * t)  # ideal gas
    mu = 1.716e-5 * (t / _T0) ** 1.5 * (_T0 + 110.4) / (t + 110.4)  # Sutherland
    k = 0.0241 * (t / _T0) ** 0.85
    cp = 1005 + 0.07 * (t - 300)
    return rho, cp, mu, k


def _coolprop(fluid, t_c, p_pa):
    from CoolProp.CoolProp import PropsSI, PhaseSI
    t = t_c + _T0
    try:
        rho = PropsSI('D', 'T', t, 'P', p_pa, fluid)
        cp = PropsSI('C', 'T', t, 'P', p_pa, fluid)
        mu = PropsSI('V', 'T', t, 'P', p_pa, fluid)
        k = PropsSI('L', 'T', t, 'P', p_pa, fluid)
        phase = PhaseSI('T', t, 'P', p_pa, fluid)
    except ValueError as exc:
        raise ValueError(f'CoolProp could not evaluate {fluid} at {t_c} degC and {p_pa / 1000:g} kPa: {exc}')
    return rho, cp, mu, k, phase


def run(arguments, context):
    fluid = str(arguments['fluid']).strip()
    if not re.fullmatch(r'[A-Za-z0-9()\-]{1,40}', fluid):
        raise ValueError('Fluid name may only contain letters, digits, hyphens and brackets')
    t_c = float(arguments['temperature_c'])
    p_kpa = float(arguments.get('pressure_kpa', 101.325))
    if not -200 <= t_c <= 2000:
        raise ValueError('Temperature must be between -200 and 2000 degC')
    if not 0 < p_kpa <= 100000:
        raise ValueError('Pressure must be above 0 and at most 100000 kPa')
    p_pa = p_kpa * 1000

    try:
        rho, cp, mu, k, phase = _coolprop(fluid, t_c, p_pa)
        source = 'CoolProp (reference equations of state)'
        note = ''
    except ImportError:
        key = fluid.lower()
        if key in _TABLES:
            rho, cp, mu, k = _interpolate(_TABLES[key], t_c)
        elif key == 'air':
            rho, cp, mu, k = _air(t_c, p_pa)
        else:
            raise ValueError('CoolProp is not installed, so only water, ethanol and air are available. Run: pip install CoolProp')
        phase = 'gas' if key == 'air' else 'liquid'
        source = 'offline fallback table'
        note = 'Approximate (about 5 percent). Install CoolProp (pip install CoolProp) for report-quality values.'
        if key in _TABLES and abs(p_kpa - 101.325) > 50:
            note += ' Liquid values ignore the pressure you gave.'

    result = {
        'fluid': fluid, 'temperature_c': t_c, 'pressure_kpa': p_kpa, 'phase': phase,
        'density_kg_m3': _round(rho), 'cp_j_kg_k': _round(cp),
        'dynamic_viscosity_pa_s': _round(mu), 'dynamic_viscosity_mpa_s': _round(mu * 1000),
        'kinematic_viscosity_m2_s': _round(mu / rho), 'thermal_conductivity_w_m_k': _round(k),
        'prandtl': _round(cp * mu / k), 'source': source,
    }
    if note:
        result['note'] = note
    return result
