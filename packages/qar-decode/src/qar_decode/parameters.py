"""FAP-driven parameter extraction.

Takes a FrameSet and an AcquiredParameter and returns engineering values at
the parameter's own rate. Nothing here reshapes rates to a common grid --
that decision belongs to the schema, and doing it here would throw away the
information needed to make it well.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from qar_decode.fap.ags import AcquiredParameter, Conversion

__all__ = ["Series", "decode"]


@dataclass
class Series:
    """One parameter's decoded samples for a whole recording."""

    mnemonic: str
    values: np.ndarray        # float64, NaN where invalid
    valid: np.ndarray         # bool, same shape
    rate: int                 # samples per second
    unit: str | None
    raw: np.ndarray           # counts, before conversion
    signed: bool
    bits: int
    labels: dict[int, str] | None = None

    @property
    def coverage(self) -> float:
        return float(self.valid.mean()) if self.valid.size else 0.0


def _assemble(fs, sample) -> np.ndarray:
    """Build one sample's raw counts across every frame."""
    out = np.zeros(fs.n_frames, dtype=np.int64)
    cursor = 0
    for part in sample.parts:
        if not (1 <= part.word <= fs.subframe_words):
            continue
        lsb = (part.tgt_lsb - 1) if part.tgt_lsb > 0 else cursor
        subframe = part.subframe or sample.subframe
        word = fs.data[:, subframe - 1, part.word - 1].astype(np.int64)
        field = (word >> (part.src_lsb - 1)) & ((1 << part.length) - 1)
        out |= field << lsb
        cursor = lsb + part.length
    return out


def _apply(conversions: tuple[Conversion, ...], raw: np.ndarray) -> np.ndarray:
    """Counts to engineering units.

    With several Conversions, each carries the raw range it covers; samples
    outside every range fall through to the first, which is what AGS does.
    """
    values = raw.astype(np.float64)
    if not conversions:
        return values

    def evaluate(conv: Conversion, x: np.ndarray) -> np.ndarray:
        acc = np.zeros_like(x)
        for power, coeff in enumerate(conv.coefficients):
            if coeff:
                acc += coeff * (x ** power if power else 1.0)
        return acc

    if len(conversions) == 1:
        return evaluate(conversions[0], values)

    out = evaluate(conversions[0], values)
    for conv in conversions[1:]:
        inside = (values >= conv.lo) & (values <= conv.hi)
        if inside.any():
            out[inside] = evaluate(conv, values[inside])
    return out


def _twos(raw: np.ndarray, bits: int) -> np.ndarray:
    sign = np.int64(1) << (bits - 1)
    return (raw ^ sign) - sign


def _resolve_signed(param: AcquiredParameter, raw: np.ndarray, bits: int) -> bool:
    """Decide whether the counts are two's complement.

    The FAP's PRM_PARAMETER_SIGNED is authoritative when set, but it is set
    on five parameters out of 2,693 -- and pitch, roll and lateral
    acceleration are not among them, though all three are plainly signed.
    Their operating range is, so we use it: decode both ways and keep the
    reading that puts more samples inside the range the FAP itself declares.

    Getting this wrong is not subtle. An unsigned read of a two's-complement
    pitch turns every nose-down attitude into roughly +180 degrees.
    """
    if param.signed:
        return True
    if param.is_discrete or bits < 2:
        return False
    if param.min_op is None or param.max_op is None or param.min_op >= 0:
        return False

    def fits(values: np.ndarray) -> float:
        eng = _apply(param.conversions, values) * param.slope + param.offset
        inside = (eng >= param.min_op) & (eng <= param.max_op)
        return float(inside.mean())

    return fits(_twos(raw, bits)) > fits(raw) + 1e-9


def decode(fs, param: AcquiredParameter) -> Series:
    """Decode one parameter across a whole recording."""
    bits = param.bits
    if bits == 0 or not param.samples:
        empty = np.zeros(0)
        return Series(param.mnemonic, empty, empty.astype(bool), 0, param.unit,
                      empty.astype(np.int64), False, 0)

    columns: list[np.ndarray] = []
    masks: list[np.ndarray] = []

    for sample in param.samples:
        raw = _assemble(fs, sample)
        ok = fs.valid[:, sample.subframe - 1].copy()

        if param.superframe and sample.modulo:
            # The sample exists only on the frame whose superframe counter
            # matches. Hold the last value across the cycle rather than
            # emitting NaN for fifteen of every sixteen seconds -- these are
            # slowly-varying quantities (gross weight, tail number) and a
            # gappy column is harder to use than a held one.
            present = fs.sfc == (sample.modulo % 16)
            idx = np.flatnonzero(present)
            if idx.size:
                take = np.searchsorted(idx, np.arange(fs.n_frames), side="right") - 1
                before_first = take < 0
                raw = raw[idx[np.clip(take, 0, None)]]
                ok = ok & ~before_first
            else:
                ok = np.zeros_like(ok)

        columns.append(raw)
        masks.append(ok)

    raw_all = np.stack(columns, axis=1).ravel()
    valid = np.stack(masks, axis=1).ravel()

    signed = _resolve_signed(param, raw_all, bits)
    counts = _twos(raw_all, bits) if signed else raw_all

    # An all-ones field is the recorder's no-computed-data marker -- but
    # only for an unsigned field. In two's complement all-ones is -1, a
    # perfectly ordinary small value, and discarding it punches holes in
    # exactly the quietest part of the flight: pitch and lateral
    # acceleration sit near zero for hours of level cruise.
    if not signed:
        valid = valid & (raw_all != (1 << bits) - 1)

    values = _apply(param.conversions, counts) * param.slope + param.offset
    values = np.where(valid, values, np.nan)

    return Series(
        mnemonic=param.mnemonic,
        values=values,
        valid=valid,
        rate=len(param.samples),
        unit=param.unit,
        raw=raw_all,
        signed=signed,
        bits=bits,
        labels=param.discretes or None,
    )
