"""Estimate how wide a title or meta description renders in Google results, in pixels.

Google cuts snippets by pixel width, not character count, so "60 characters" is a rough
guess at best. These are Arial advance widths (units per 1000 em), which is close to what
Google uses on desktop: titles at about 20px, descriptions at about 14px.
"""

_WIDTHS = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191,
    "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333, ".": 278, "/": 278,
    ":": 278, ";": 278, "<": 584, "=": 584, ">": 584, "?": 556, "@": 1015, "[": 278,
    "]": 278, "_": 556, "|": 260, "~": 584, "–": 556, "—": 1000, "’": 222, "“": 333, "”": 333,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556,
    "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556, "p": 556,
    "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500, "w": 722, "x": 500,
    "y": 500, "z": 500,
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722,
    "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778, "P": 667,
    "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667,
    "Y": 667, "Z": 611,
}
_DEFAULT = 556

TITLE_FONT_PX = 20
TITLE_MAX_PX = 580
DESCRIPTION_FONT_PX = 14
DESCRIPTION_MAX_PX = 920


def text_width(text: str, font_px: int) -> int:
    units = sum(_WIDTHS.get(ch, _DEFAULT) for ch in text or "")
    return round(units * font_px / 1000)


def title_width(text: str) -> int:
    return text_width(text, TITLE_FONT_PX)


def description_width(text: str) -> int:
    return text_width(text, DESCRIPTION_FONT_PX)
