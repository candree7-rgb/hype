"""Donchian trend ensemble: equal-weight average of the parameter plateau
(entry_n x exit_n combos, all atr_mult 3.0) found on IS. Averaging across the
plateau instead of picking the single best IS cell reduces selection risk.
Members: 55/20, 70/20, 100/20, 55/30, 70/30, 100/50.
"""
import pandas as pd

import importlib.util as _ilu
from pathlib import Path as _Path

_spec = _ilu.spec_from_file_location(
    "tsmom_donchian", _Path(__file__).parent / "tsmom_donchian.py")
_base = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_base)

DEFAULT_PARAMS = {
    "members": [(55, 20), (70, 20), (100, 20), (55, 30), (70, 30), (100, 50)],
    "atr_mult": 3.0,
    "atr_window": 20,
    "vol_window": 30,
    "asset_vol_target": 0.20,
    "max_asset_lev": 1.0,
    "warmup": 90,
    "ppy": 365,
}


def target_weights(panel: dict, params: dict) -> pd.DataFrame:
    acc = None
    for en, ex in params["members"]:
        p = {**_base.DEFAULT_PARAMS,
             **{k: v for k, v in params.items() if k != "members"},
             "entry_n": en, "exit_n": ex}
        w = _base.target_weights(panel, p)
        acc = w if acc is None else acc + w
    return acc / len(params["members"])
