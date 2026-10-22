# Building the companion and book

The offline reference requires Python 3.11 or newer. It was verified with Python 3.12.14 in the authoring environment. No broker credentials, network calls or third-party Python packages are required for the accounting demo.

```bash
python3 -m reference.engine
python3 -m unittest discover -s tests -v
mkdir -p build
c++ -std=c++20 -O2 -Wall -Wextra -Werror -pedantic reference/risk_kernel.cpp -o build/risk_kernel
./build/risk_kernel
```

The PDF build requires Pandoc, XeLaTeX, makeindex and Matplotlib. Install the optional Python dependency into an isolated environment using `requirements-book.txt`. The TeX installation must include book, fontspec, unicode-math, listings, longtable, booktabs, fancyhdr, titlesec and makeidx. The selected fonts are Bitstream Charter, Latin Modern Sans and DejaVu Sans Mono. If a font is unavailable, install it or select an installed equivalent in `scripts/build_book.py` before building.

```bash
python3 scripts/build_book.py
```

The result is `output/Project_Unknown_Technical_Book_v1.pdf`. The build generates diagrams, expands source-code includes, creates the table of contents and subject index, and reruns TeX to resolve page references. Generated files are ignored by Git. PDF hashes can differ between builds because PDF metadata may include build time; compare source and content as well as hashes.

`docs/DELIVERY_MANIFEST.json` records the delivered edition's actual page count and hash. It is an edition record, not automatically rewritten by every local build. Update it only when publishing a new reviewed edition.
