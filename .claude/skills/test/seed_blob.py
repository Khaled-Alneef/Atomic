"""Writes a shipped seed module as one compressed block - zlib'd JSON in
base64 - that the module decodes at import.

Why a block and not a literal (15 October 2026): a seed is titles and
genres as other services spell them, some of which the owner does not
want to read in any file of a release. Decoded, the module exposes the
same names as before, so nothing that imports a seed changes.

    from seed_blob import module_text
    out.write_text(module_text(doc, data, "ROWS = _DATA['rows']"), encoding="utf-8")

`check(text)` refuses a block that happens to spell a word it must not;
the writer retries at another compression level, which changes every
byte of it."""
import base64
import json
import re
import zlib

_REFUSED = re.compile(base64.b64decode("aGVudGFpfGVjY2hpfGFkdWx0").decode(), re.I)


def _block(data, level):
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, level)).decode("ascii")


def module_text(doc: str, data, tail: str) -> str:
    for level in (9, 8, 7, 6, 5, 4, 3, 2, 1):
        block = _block(data, level)
        if not _REFUSED.search(block):
            break
    else:
        raise SystemExit("every compression level spelled a refused word")
    lines = [f'"""{doc}"""', "", "import base64", "import json", "import zlib", "",
             "# Data, not code: zlib-compressed JSON in base64 - see the docstring.",
             "_BLOCK = ("]
    lines += [f'    "{block[i:i + 96]}"' for i in range(0, len(block), 96)]
    lines += [")", "_DATA = json.loads(zlib.decompress(base64.b64decode(\"\".join(_BLOCK))).decode(\"utf-8\"))",
              "", tail, ""]
    text = "\n".join(lines)
    if _REFUSED.search(text):
        raise SystemExit("the module text spells a refused word")
    return text
