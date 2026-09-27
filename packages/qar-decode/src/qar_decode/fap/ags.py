"""Safran AGS frame layout documents.

An AGS FAP is a directory, not a file:

    <FAP>/<name>.lframe                    frame geometry
    <FAP>/AcquiredParameters/*.lacquired   one XML per parameter

Both are XML. Both declare ``encoding="utf-16"`` in the prolog and are in
fact UTF-8 with a BOM, so the declaration is ignored and the bytes are
decoded as ``utf-8-sig``. Trusting the prolog produces mojibake, which is
the first thing that goes wrong when reading these by hand.

The vocabulary, once translated:

    FRA_*   frame:     subframe size, superframe counter location
    PRA_*   parameter: rate, slope, offset, superframe flag
    LCP_*   location:  which word, which bits, which subframe, which sample
    EUC_*   conversion: polynomial from counts to engineering units
    TXD_*   text:      discrete value -> label

A parameter's rate is not stated as a number to trust; it is
``len(subframes) * len(samples)`` and PRM_RATE agrees with that. We compute
it and check PRM_RATE against it, because a FAP whose stated rate disagrees
with its own locations is a FAP worth failing loudly on.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from qar_decode.frames import FRAME_SECONDS

__all__ = [
    "AcquiredParameter",
    "BitPart",
    "Conversion",
    "FrameLayout",
    "Fap",
    "Sample",
    "load_fap",
]


def _subframes(spec: str) -> list[int]:
    """Which subframes a location spec names. Out-of-range entries are typos."""
    spec = (spec or "ALL").strip()
    if spec.upper() == "ALL":
        return [1, 2, 3, 4]
    found = [int(x) for x in spec.split(",") if x.strip().isdigit()]
    return [s for s in found if 1 <= s <= 4] or [1]


def _part(el: ET.Element, subframe: int | None = None) -> "BitPart":
    return BitPart(
        word=_int(el, "LCP_WORD_NB", 1),
        src_lsb=_int(el, "LCP_BIT_SOURCE_LSB", 1),
        length=_int(el, "LCP_BIT_LENGTH", 12),
        tgt_lsb=_int(el, "LCP_BIT_TARGET_LSB", 0),
        subframe=subframe,
    )


def _xml(path: Path) -> ET.Element:
    """Parse one AGS document.

    The prolog lies about the encoding; utf-8-sig is what the bytes are.
    """
    return ET.fromstring(path.read_text(encoding="utf-8-sig"))


def _int(el: ET.Element, key: str, default: int = 0) -> int:
    raw = el.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(el: ET.Element, key: str, default: float = 0.0) -> float:
    raw = el.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Frame geometry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FrameLayout:
    """The recording's shape, from the .lframe document."""

    name: str
    data_class: str
    subframe_words: int
    frames_per_superframe: int
    sfc_subframe: int
    sfc_word: int
    sfc_bit_lsb: int
    sfc_bit_length: int
    dfc_subframe: int
    dfc_word: int
    dfc_bit_lsb: int
    dfc_bit_length: int
    has_superframe: bool

    @classmethod
    def from_file(cls, path: Path) -> FrameLayout:
        r = _xml(path)
        return cls(
            name=r.get("FRA_NAME", path.stem),
            data_class=r.get("FRA_DATA_CLASS", "QAR"),
            subframe_words=_int(r, "FRA_SUBFRAME_WORD_NB", 1024),
            frames_per_superframe=_int(r, "FRA_NB_FRAMES_PER_SUPERFRAME", 16),
            sfc_subframe=_int(r, "FRA_SFC_LOC_F", 1),
            sfc_word=_int(r, "FRA_SFC_LOC_W", 1),
            sfc_bit_lsb=_int(r, "FRA_SFC_BIT_SOURCE_LSB", 1),
            sfc_bit_length=_int(r, "FRA_SFC_BIT_LENGTH", 4),
            dfc_subframe=_int(r, "FRA_DFC_LOC_F", 1),
            dfc_word=_int(r, "FRA_DFC_LOC_W", 1),
            dfc_bit_lsb=_int(r, "FRA_DFC_BIT_SOURCE_LSB", 1),
            dfc_bit_length=_int(r, "FRA_DFC_BIT_LENGTH", 12),
            has_superframe=_int(r, "FRA_SUPERFRAME_TYPE", 0) != 0,
        )


# ---------------------------------------------------------------------------
# Where the bits are
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BitPart:
    """One contiguous bit field contributing to a parameter's raw value.

    ``word`` and ``src_lsb`` are 1-based, matching the FAP. ``tgt_lsb`` is
    1-based too, but zero carries meaning: it says "continue immediately
    above the previous part" rather than "bit 0". Reading zero literally
    stacks every part on top of bit 0 and silently ORs them together, which
    decodes to plausible-looking nonsense.

    ``subframe`` is None for the common case, meaning "whichever subframe
    this sample sits in". It is set only when a parameter's bits are split
    across subframes -- A350 gross weight keeps twelve bits in subframe 3
    and the top three in subframe 4 -- in which case the part must be read
    from the subframe it names, not from the sample's.
    """

    word: int
    src_lsb: int
    length: int
    tgt_lsb: int = 0
    subframe: int | None = None


@dataclass(frozen=True)
class Sample:
    """One sample of a parameter within one subframe."""

    subframe: int
    index: int
    parts: tuple[BitPart, ...]
    modulo: int = 0          # superframe frame number, 0 = every frame

    @property
    def bits(self) -> int:
        total = 0
        cursor = 0
        for p in self.parts:
            lsb = (p.tgt_lsb - 1) if p.tgt_lsb > 0 else cursor
            cursor = lsb + p.length
            total = max(total, cursor)
        return total


# ---------------------------------------------------------------------------
# Counts to engineering units
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Conversion:
    """A polynomial in the raw count, valid over [lo, hi].

    AGS writes each coefficient as a mantissa and a power of ten:
    ``value = sum(coeff[n] * 10**exp[n] * raw**n)``. Most parameters are
    linear, a handful are not, and a few carry several Conversions that
    apply over different raw ranges.
    """

    coefficients: tuple[float, ...]
    lo: float = -1e30
    hi: float = 1e30

    @classmethod
    def from_element(cls, el: ET.Element) -> Conversion:
        coeffs: list[float] = []
        for n in range(6):
            if el.get(f"EUC_COEFF_{n}") is None:
                continue
            mantissa = _float(el, f"EUC_COEFF_{n}", 0.0)
            exponent = _int(el, f"EUC_COEFF_EXP_{n}", 0)
            while len(coeffs) < n:
                coeffs.append(0.0)
            coeffs.append(mantissa * (10.0 ** exponent))
        if not coeffs:
            coeffs = [0.0, 1.0]
        # A lone EUC_COEFF_1 with no EUC_COEFF_0 is a pure scale factor.
        if el.get("EUC_COEFF_0") is None and len(coeffs) >= 2:
            coeffs[0] = 0.0
        return cls(
            coefficients=tuple(coeffs),
            lo=_float(el, "EUC_MIN", -1e30),
            hi=_float(el, "EUC_MAX", 1e30),
        )


# ---------------------------------------------------------------------------
# The parameter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AcquiredParameter:
    """One FAP entry: identity, location, conversion, and meaning."""

    mnemonic: str
    name: str
    unit: str | None
    stated_rate: int
    signed: bool
    superframe: bool
    slope: float
    offset: float
    samples: tuple[Sample, ...]
    conversions: tuple[Conversion, ...] = ()
    discretes: dict[int, str] = field(default_factory=dict)
    min_op: float | None = None
    max_op: float | None = None
    description: str | None = None
    source_file: str | None = None
    # PRA_CONV_CONF as sub-field bit widths, most significant first. Empty
    # for a plain binary number, which is almost every parameter.
    digit_widths: tuple[int, ...] = ()

    @property
    def bits(self) -> int:
        return max((s.bits for s in self.samples), default=0)

    @property
    def samples_per_frame(self) -> int:
        """How many times the parameter appears in one frame.

        This is what AGS's PRM_RATE counts -- it agrees with this number on
        every non-superframe parameter checked -- and it is *not* a rate in
        hertz. A frame is four seconds, so PRM_RATE=4 means one sample per
        second, and PRM_RATE=32 on VRTG means 8 Hz, which is the standard
        vertical-acceleration rate. Reading PRM_RATE as hertz overstates
        every rate in the FAP by four.
        """
        return len(self.samples)

    @property
    def rate_hz(self) -> float:
        """Samples per second."""
        return self.samples_per_frame / FRAME_SECONDS

    @property
    def is_discrete(self) -> bool:
        return bool(self.discretes)

    @property
    def field_widths(self) -> tuple[int, ...]:
        """The digit widths laid over this parameter's actual bit length.

        PRA_CONV_CONF usually sums to the field width exactly. When it does
        not, it is a pattern: "7" on a 56-bit field is eight 7-bit
        characters, and "7777" on a 7-bit field is one character of a
        four-letter code the FAP splits across four parameters. A spec that
        neither tiles the field nor fits it from one end is left undecoded
        rather than guessed at.
        """
        widths, bits = self.digit_widths, self.bits
        if not widths or bits == 0:
            return ()
        total = sum(widths)
        if total == bits:
            return widths
        if bits % total == 0:
            return widths * (bits // total)
        for ordered, reverse in ((widths, False), (widths[::-1], True)):
            taken, used = [], 0
            for width in ordered:
                if used + width > bits:
                    break
                taken.append(width)
                used += width
            if used == bits:
                return tuple(reversed(taken)) if reverse else tuple(taken)
        return ()

    @property
    def encoding(self) -> str:
        """How the assembled bits are read: "binary", "bcd" or "text".

        Four bits or fewer per sub-field is a decimal digit; seven or eight
        is a character. Anything else is left as a binary number.
        """
        widths = self.field_widths
        if widths and all(w <= 4 for w in widths):
            return "bcd"
        if widths and all(w in (7, 8) for w in widths):
            return "text"
        return "binary"

    @classmethod
    def from_file(cls, path: Path) -> AcquiredParameter | None:
        r = _xml(path)

        # Group by sample number first. Every LCP under one sample number is
        # a *part* of that sample, even when the parts name different
        # subframes -- which is the case that makes the naive reading wrong.
        # Treating each subframe as a separate sample splits a 15-bit gross
        # weight into a 12-bit and a 3-bit parameter, both plausible-looking
        # and both wrong.
        grouped: dict[int, list[ET.Element]] = {}
        for el in r:
            if el.tag == "LCP":
                grouped.setdefault(_int(el, "LCP_SAMPLE_NUMBER", 1), []).append(el)
        if not grouped:
            return None

        samples: list[Sample] = []
        for idx in sorted(grouped):
            els = sorted(grouped[idx], key=lambda e: _int(e, "LCP_PART_NUMBER", 1))
            specs = {(e.get("LCP_SUBFRAME") or "ALL").strip().upper() for e in els}

            if len(specs) == 1:
                # All parts share a subframe spec: the sample repeats once
                # per subframe named, each repeat reading its own subframe.
                where = _subframes(specs.pop())
                for sf in where:
                    samples.append(
                        Sample(
                            subframe=sf,
                            index=idx,
                            parts=tuple(_part(e) for e in els),
                            modulo=_int(els[0], "LCP_MODULO", 0),
                        )
                    )
            else:
                # Parts split across subframes: one sample, each part read
                # from the subframe it names.
                anchor = _subframes(els[0].get("LCP_SUBFRAME") or "ALL")
                parts = tuple(
                    _part(e, subframe=_subframes(e.get("LCP_SUBFRAME") or "ALL")[0])
                    for e in els
                )
                samples.append(
                    Sample(
                        subframe=anchor[0] if anchor else 1,
                        index=idx,
                        parts=parts,
                        modulo=_int(els[0], "LCP_MODULO", 0),
                    )
                )

        samples.sort(key=lambda s: (s.subframe, s.index))
        if not samples:
            return None

        conversions = tuple(
            Conversion.from_element(e) for e in r if e.tag == "EUC"
        )
        discretes = {
            _int(e, "TXD_RAW_DATA_VALUE", 0): (e.get("TXD_DESCRIPTION") or "").strip()
            for e in r
            if e.tag == "TXD"
        }

        # A digit-width string such as "44" or "777". Real FAPs also carry
        # "" and ".", both of which mean a plain binary number.
        conv = (r.get("PRA_CONV_CONF") or "").strip()
        digit_widths = tuple(int(c) for c in conv if c.isdigit())
        if 0 in digit_widths:
            digit_widths = ()

        mnemonic = r.get("PRM_MNEMONIC") or path.stem
        return cls(
            mnemonic=mnemonic,
            name=r.get("PRM_NAME") or mnemonic,
            unit=(r.get("PRM_UNIT") or None),
            stated_rate=_int(r, "PRM_RATE", len(samples)),
            signed=r.get("PRM_PARAMETER_SIGNED") == "1",
            superframe=r.get("PRA_SUPERFRAME") == "1",
            slope=_float(r, "PRA_SLOPE", 1.0),
            offset=_float(r, "PRA_OFFSET", 0.0),
            samples=tuple(samples),
            conversions=conversions,
            discretes=discretes,
            min_op=_float(r, "PRM_MIN_OP_RANGE") if r.get("PRM_MIN_OP_RANGE") else None,
            max_op=_float(r, "PRM_MAX_OP_RANGE") if r.get("PRM_MAX_OP_RANGE") else None,
            description=r.get("PRM_DESCRIPTION") or None,
            source_file=path.name,
            digit_widths=digit_widths,
        )


# ---------------------------------------------------------------------------
# The whole document set
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fap:
    """A parsed FAP directory."""

    name: str
    frame: FrameLayout
    parameters: dict[str, AcquiredParameter]
    root: Path

    def __getitem__(self, mnemonic: str) -> AcquiredParameter:
        return self.parameters[mnemonic]

    def __contains__(self, mnemonic: str) -> bool:
        return mnemonic in self.parameters

    def __len__(self) -> int:
        return len(self.parameters)

    def rate_disagreements(self) -> list[tuple[str, int, int]]:
        """Parameters whose PRM_RATE contradicts their own locations.

        Returned rather than raised: a handful of disagreements is normal in
        a hand-maintained FAP, but a large number means the wrong FAP.
        """
        out = []
        for p in self.parameters.values():
            if p.stated_rate and p.stated_rate != p.samples_per_frame:
                out.append((p.mnemonic, p.stated_rate, p.samples_per_frame))
        return out


def load_fap(root: str | Path, only: set[str] | None = None) -> Fap:
    """Read a FAP directory.

    ``only`` restricts the parameter set by mnemonic. Loading all 2,693
    parameters of an A350 FAP takes a few seconds; a decode that needs
    sixteen of them should say so.
    """
    root = Path(root)
    lframes = sorted(root.glob("*.lframe"))
    if not lframes:
        raise FileNotFoundError(f"no .lframe in {root}")
    frame = FrameLayout.from_file(lframes[0])

    params: dict[str, AcquiredParameter] = {}
    acquired = root / "AcquiredParameters"
    for path in sorted(acquired.glob("*.lacquired")):
        if only is not None:
            # Filenames are <MNEMONIC>_USR.lacquired, so the cheap prefix
            # test avoids parsing thousands of documents we do not want.
            stem = path.stem
            base = stem[:-4] if stem.endswith("_USR") else stem
            if base not in only:
                continue
        try:
            param = AcquiredParameter.from_file(path)
        except ET.ParseError:
            continue
        if param is not None:
            params[param.mnemonic] = param

    return Fap(name=root.name, frame=frame, parameters=params, root=root)
