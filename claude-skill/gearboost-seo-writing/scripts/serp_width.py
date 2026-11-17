"""Measure how wide a title or meta description renders in Google (pixels, Arial).

Usage:
  python serp_width.py title "Brake Repair in Kinston, NC | Joe's Auto Repair"
  python serp_width.py description "Brake pad and rotor replacement in Kinston..."
"""
import sys

W = {" ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667, "'": 191, "(": 333, ")": 333, "*": 389,
     "+": 584, ",": 278, "-": 333, ".": 278, "/": 278, ":": 278, ";": 278, "?": 556, "@": 1015, "|": 260, "–": 556,
     "—": 1000, "’": 222, "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556, "i": 222,
     "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278,
     "u": 556, "v": 500, "w": 722, "x": 500, "y": 500, "z": 500, "A": 667, "B": 667, "C": 722, "D": 722, "E": 667,
     "F": 611, "G": 778, "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722, "O": 778, "P": 667,
     "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611}
SPEC = {"title": (20, 580), "description": (14, 920)}

if __name__ == "__main__":
    kind, text = sys.argv[1], " ".join(sys.argv[2:])
    font, limit = SPEC[kind]
    px = round(sum(W.get(c, 556) for c in text) * font / 1000)
    print(f"{px}px of {limit}px ({len(text)} characters): {'fits' if px <= limit else 'TOO WIDE, will be cut off'}")
