"""Build the indexed PDF from Markdown and the actual reference source files.

Requires Python 3.11+, Matplotlib, Pandoc, XeLaTeX and makeindex.
Run from any directory: python3 scripts/build_book.py
"""
from pathlib import Path
import json
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)


def figures():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyBboxPatch

    def draw(name, nodes, edges, size, limits):
        fig, ax = plt.subplots(figsize=size)
        ax.set(xlim=(0, limits[0]), ylim=(0, limits[1]))
        ax.axis("off")
        for key, (x, y, label, dark) in nodes.items():
            ax.add_patch(FancyBboxPatch((x-1.2, y-.43), 2.4, .86,
                         boxstyle="round,pad=0.05,rounding_size=0.10",
                         facecolor="#15324b" if dark else "#eef3f6",
                         edgecolor="#557487", linewidth=1))
            ax.text(x, y, label, ha="center", va="center", fontsize=9,
                    color="white" if dark else "#15324b")
        for a, b in edges:
            x1,y1,*_=nodes[a]; x2,y2,*_=nodes[b]
            if abs(x2-x1)<.1:
                dy=.49 if y2>y1 else -.49
                start=(x1,y1+dy); end=(x2,y2-dy)
            elif abs(y2-y1)<.1:
                dx=1.27 if x2>x1 else -1.27
                start=(x1+dx,y1); end=(x2-dx,y2)
            else:
                start=(x1,y1-.49); end=(x2,y2+.49)
            ax.annotate("",xy=end,xytext=start,arrowprops={"arrowstyle":"->","color":"#557487","lw":1.2})
        fig.tight_layout(pad=.3)
        fig.savefig(OUT / (name+".pdf"), bbox_inches="tight")
        plt.close(fig)

    draw("architecture", {
        "data": (1.5,7,"Licensed sources",False),
        "ai": (4.7,7,"Cloud research\nworkers",False),
        "owner": (7.9,7,"Owner",False),
        "features": (1.5,5,"Versioned data\nand features",False),
        "proposal": (4.7,5,"Validated\nproposals",False),
        "policy": (7.9,5,"Approved policy\nand stop latch",True),
        "risk": (4.7,3,"Risk, reservations\nand ledger",True),
        "gateway": (4.7,1,"Execution gateway\nand reconciliation",True),
        "broker": (7.9,1,"Broker account",True)},
        [("data","features"),("data","ai"),("ai","proposal"),("owner","policy"),
         ("features","risk"),("proposal","risk"),("policy","risk"),("risk","gateway"),("gateway","broker")],
        (6.5,5.3),(9.5,8))
    draw("sessions", {
        "goal": (4.5,10,"Goal observed",False),
        "wind": (4.5,8.4,"Wind down\nand reconcile",True),
        "done": (4.5,6.8,"Completed\nnet proceeds",True),
        "protect": (1.6,5,"Protected cash\nand receivables",True),
        "request": (7.4,5,"One successor\nrequest",False),
        "settled": (1.6,3.2,"Settlement confirmed\nprotection retained",True),
        "fund": (7.4,3.2,"Check independent\navailable cash",True),
        "wait": (5.6,1.3,"Insufficient:\nwait for funds",False),
        "run": (8.5,1.3,"Sufficient:\nstart successor",True)},
        [("goal","wind"),("wind","done"),("done","protect"),("done","request"),
         ("protect","settled"),("request","fund"),("fund","wait"),("fund","run")],
        (6.5,6.5),(10,11))


TERMS = ["account eligibility", "adverse selection", "agents", "ALFRED", "Almgren-Chriss",
         "API", "authentication", "Avellaneda-Stoikov", "AWS", "backpressure", "backtest",
         "Bayesian", "Black-Scholes", "broker", "calibration", "cancel", "capital",
         "cash", "Claude Code", "cloud", "cointegration", "conservation", "corporate actions",
         "cost", "covariance", "data rights", "DCF", "drawdown", "entitlements", "event",
         "execution", "expected shortfall", "fees", "fencing", "FINRA", "foreign exchange",
         "futures", "HFT", "IBKR", "idempotence", "India", "inventory", "Kalman",
         "Kelly", "latency", "ledger", "license", "liquidity", "look-ahead", "margin",
         "market making", "mean reversion", "MiFID", "momentum", "NIST", "NSE", "options",
         "outbox", "paper", "point-in-time", "portfolio", "promotion", "prompt injection",
         "protected proceeds", "RBI", "reconciliation", "reinforcement learning", "reservation",
         "risk", "SEBI", "security", "session", "settlement", "Sharpe", "shorting", "slippage",
         "SQLite", "stop", "stress", "tax", "terminal", "trend", "uncertainty", "volatility",
         "walk-forward"]


def prepare(path):
    s = path.read_text()
    if path.name.startswith("00-"):
        s = s.replace("{#reading-guide}", "{#reading-guide .unnumbered}")
    # Index the first prose occurrence per chapter, never the source listings.
    split = s.find("\n\n")
    title, body = s[:split], s[split:]
    if not path.name.startswith(("31-", "32-", "33-")):
        for term in sorted(TERMS, key=len, reverse=True):
            pattern = re.compile(r"(?<![\w/#])"+re.escape(term)+r"(?![\w])", re.I)
            body = pattern.sub(lambda m: m[0]+r"\index{"+term+"}", body, count=1)
    s=title+body
    def include(m):
        p=ROOT/m[1]
        lang={".py":"python", ".cpp":"cpp", ".sql":"sql"}[p.suffix]
        return "\n```"+lang+"\n"+p.read_text()+"\n```\n"
    s=re.sub(r"<!-- INCLUDE: (.*?) -->",include,s)
    s=re.sub(r"<!-- FIGURE: (.*?) -->",lambda m:"\n!["+("Authority boundaries" if m[1]=="architecture" else "Completion, settlement and successor funding")+"]("+str(OUT/(m[1]+".pdf"))+"){width=95%}\n",s)
    # Linked bibliography identifiers, preserving compact source notation.
    s=re.sub(r"\[(S\d\d(?:, S\d\d)*)\]",lambda m:"["+", ".join("["+v+"](#source-"+v.lower()+")" for v in m[1].split(", "))+"]",s)
    s=re.sub(r"\*\*(S\d\d)\.",lambda m:r"\hypertarget{source-"+m[1].lower()+"}{}\n\n**"+m[1]+".",s)
    return s


def run(args):
    result=subprocess.run(args,cwd=OUT,capture_output=True,text=True)
    if result.returncode:
        print(result.stdout[-6000:]+result.stderr[-6000:],file=sys.stderr)
        raise SystemExit(result.returncode)


def main():
    figures()
    chapters=sorted((ROOT/"docs/book").glob("[0-9][0-9]-*.md"))
    content="\n\n".join(prepare(p) for p in chapters)
    (OUT/"book.md").write_text(content)
    metadata="""title: 'Project Unknown'
subtitle: 'Engineering a Personal AI Trading Desk'
author: 'Technical specification and implementation companion'
date: 'Engineering edition 1.0 | October 22, 2026'
documentclass: book
classoption: [oneside, openany]
fontsize: 11pt
mainfont: Bitstream Charter
sansfont: Latin Modern Sans
monofont: DejaVu Sans Mono
geometry: [paperwidth=7.25in, paperheight=10.25in, left=0.72in, right=0.68in, top=0.75in, bottom=0.75in]
colorlinks: true
linkcolor: NavyBlue
urlcolor: NavyBlue
toc-title: Contents
"""
    (OUT/"metadata.yaml").write_text(metadata)
    header=r"""
\usepackage{makeidx}
\makeindex
\usepackage{fancyhdr}
\usepackage{titlesec}
\makeatletter
\renewcommand{\@pnumwidth}{2.2em}
\renewcommand{\@tocrmarg}{3.2em}
\makeatother
\definecolor{NavyBlue}{HTML}{15324B}
\definecolor{CodeGray}{HTML}{F3F5F7}
\titleformat{\chapter}[display]{\sffamily\bfseries\raggedright\color{NavyBlue}}{\large\chaptertitlename\ \thechapter}{8pt}{\LARGE}
\titlespacing*{\chapter}{0pt}{12pt}{22pt}
\titleformat{\section}{\sffamily\large\bfseries\raggedright\color{NavyBlue}}{\thesection}{0.7em}{}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\sffamily\small PROJECT UNKNOWN}
\fancyhead[R]{\sffamily\small Engineering edition 1.0}
\fancyfoot[C]{\sffamily\small\thepage}
\setlength{\headheight}{15pt}
\setlength{\parindent}{0pt}
\setlength{\parskip}{5pt plus 1pt}
\setlength{\emergencystretch}{3em}
\widowpenalty=10000
\clubpenalty=10000
\lstset{basicstyle=\ttfamily\scriptsize,breaklines=true,breakatwhitespace=false,columns=fullflexible,keepspaces=true,showstringspaces=false,backgroundcolor=\color{CodeGray},frame=single,rulecolor=\color{CodeGray},xleftmargin=5pt,xrightmargin=5pt,framesep=4pt}
"""
    (OUT/"header.tex").write_text(header)
    run(["pandoc","book.md","--from=markdown+raw_tex+tex_math_dollars","--to=latex","--standalone",
         "--top-level-division=chapter","--number-sections","--toc","--toc-depth=2","--listings",
         "--metadata-file=metadata.yaml","--include-in-header=header.tex","-o","book.tex"])
    tex=(OUT/"book.tex").read_text()
    cover=r"""\begin{titlepage}
\sffamily\color{NavyBlue}
\vspace*{0.55in}
{\small ENGINEERING EDITION 1.0\par}
\vspace{0.45in}
{\fontsize{36}{43}\selectfont\bfseries PROJECT\par UNKNOWN\par}
\vspace{0.35in}
\rule{\linewidth}{1.2pt}
\vspace{0.25in}
{\LARGE Engineering a Personal\par AI Trading Desk\par}
\vfill
{\large Theory, mathematics, architecture\par and implementation\par}
\vspace{0.3in}
{\normalsize Technical book and Claude Code companion\par}
\vspace{0.5in}
{\small October 22, 2026\par}
\vspace{0.12in}
{\small Personal use\quad |\quad Stocks first\quad |\quad Offline reference\par}
\end{titlepage}"""
    tex=tex.replace("\n"+r"\maketitle"+"\n", "\n"+cover+"\n", 1)
    tex=tex.replace(r"\mainmatter", "")
    if r"\frontmatter" not in tex:
        tex=tex.replace(r"\begin{document}",r"\begin{document}"+"\n"+r"\frontmatter",1)
    tex=tex.replace(r"\hypertarget{product-contract}",r"\mainmatter"+"\n"+r"\hypertarget{product-contract}",1)
    tex=tex.replace(r"\hypertarget{contracts-appendix}",r"\appendix"+"\n"+r"\hypertarget{contracts-appendix}",1)
    tex=tex.replace(r"\end{document}",r"\clearpage\phantomsection\addcontentsline{toc}{chapter}{Subject index}\printindex"+"\n"+r"\end{document}")
    (OUT/"book.tex").write_text(tex)
    run(["xelatex","-interaction=nonstopmode","-halt-on-error","book.tex"])
    run(["makeindex","book.idx"])
    for _ in range(2):
        run(["xelatex","-interaction=nonstopmode","-halt-on-error","book.tex"])
    run(["makeindex","book.idx"])
    run(["xelatex","-interaction=nonstopmode","-halt-on-error","book.tex"])
    target=OUT/"Project_Unknown_Technical_Book_v1.pdf"
    target.write_bytes((OUT/"book.pdf").read_bytes())
    manifest={"edition":"1.0","research_date":"2026-10-22","chapter_files":len(chapters),
              "source_words":sum(len(p.read_text().split()) for p in chapters),"pdf":target.name}
    (OUT/"book_manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
