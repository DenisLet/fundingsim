import random
from pathlib import Path
from typing import Tuple, Literal

import pandas as pd
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import tkinter as tk
from tkinter import ttk, filedialog, messagebox


# ──────────── helpers ────────────
def detect_reader(p: Path) -> Literal["excel", "csv"]:
    """Choose reader by file extension."""
    return "excel" if p.suffix.lower().endswith((".xlsx", ".xls")) else "csv"


def load_funding(p: Path) -> pd.DataFrame:
    """
    Load Bybit funding-rate export (CSV RU-locale or Excel EN-locale).

    Returns DataFrame with:
        timestamp – naive UTC datetime
        funding_rate – decimal (0.0001 for 0.01 %)
    """
    if detect_reader(p) == "excel":
        df = pd.read_excel(p)
        df.columns = [c.strip() for c in df.columns]
        df.rename(columns={"Time(UTC)": "timestamp",
                           "Funding Rate": "funding_rate"}, inplace=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df["funding_rate"] = df["funding_rate"].astype(str).str.rstrip("% ").astype(float)
    else:
        df = pd.read_csv(p)
        df.rename(columns={"Время": "timestamp",
                           '"Ставка финансирования"': "raw_rate"}, inplace=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df["funding_rate"] = df["raw_rate"].str.rstrip("% ").astype(float) / 100

    # если вдруг пришла уже десятичная ставка > 1 % — делим ещё раз
    df.loc[df["funding_rate"].abs() > 1, "funding_rate"] /= 100
    df["timestamp"] = df["timestamp"].dt.tz_localize(None)          # drop tzinfo
    return df.sort_values("timestamp")[["timestamp", "funding_rate"]]


def sim(rates: pd.DataFrame, *,
        initial: float, thresh: float, target: float,
        l_rng: Tuple[float, float], s_rng: Tuple[float, float],
        skim_enabled: bool = True) -> pd.DataFrame:
    """
    One Monte-Carlo pass of funding-fund behaviour.

    · initial – starting balance
    · thresh  – if FF < thresh → top-up to *target*
    · target  – desired balance; any excess is skimmed off
    · l_rng / s_rng – uniform ranges for long/short notionals
    · skim_enabled – turn skim logic ON/OFF
    """
    bal = initial
    rows = []

    for ts, rate in rates.itertuples(index=False):
        nL = random.uniform(*l_rng)
        nS = random.uniform(*s_rng)

        feeL, feeS = -nL * rate, nS * rate
        inflow, outflow = max(-feeL, 0), max(feeS, 0)
        bal += inflow - outflow

        base = {"timestamp": ts, "funding_rate": rate,
                "notional_long": nL, "notional_short": nS,
                "inflow": inflow, "outflow": outflow}

        # 1️⃣ провал ниже порога – фиксируем до пополнения, затем top-up
        if bal < thresh:
            rows.append({**base, "top_up": 0.0, "skim": 0.0,
                         "balance_after": bal})
            top = target - bal
            bal = target
            rows.append({**base, "top_up": top, "skim": 0.0,
                         "balance_after": bal})
            continue

        # 2️⃣ выше цели – только если skim включён
        if bal > target and skim_enabled:
            rows.append({**base, "top_up": 0.0, "skim": 0.0,
                         "balance_after": bal})
            skim = bal - target
            bal = target
            rows.append({**base, "top_up": 0.0, "skim": skim,
                         "balance_after": bal})
            continue

        # 3️⃣ обычный случай
        rows.append({**base, "top_up": 0.0, "skim": 0.0,
                     "balance_after": bal})

    return pd.DataFrame(rows)


# ──────────── GUI ────────────
class FundingApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Funding Fund simulator")
        self.geometry("1180x730")

        # top control panel -------------------------------------------------
        ctl = ttk.Frame(self, padding=4)
        ctl.pack(side=tk.TOP, fill=tk.X)

        self.file = tk.StringVar()
        ttk.Button(ctl, text="📂 Файл ставок",
                   command=self.pick_file).pack(side=tk.LEFT)
        ttk.Entry(ctl, width=45, textvariable=self.file,
                  state="readonly").pack(side=tk.LEFT, padx=4)

        # parameters --------------------------------------------------------
        self.init_v = tk.DoubleVar(value=2000)
        self.th_v   = tk.DoubleVar(value=1000)
        self.targ_v = tk.DoubleVar(value=2000)
        self.l_lo_v = tk.DoubleVar(value=60000)
        self.l_hi_v = tk.DoubleVar(value=90000)
        self.s_lo_v = tk.DoubleVar(value=40000)
        self.s_hi_v = tk.DoubleVar(value=70000)
        self.seed_v = tk.IntVar(value=42)
        self.skim_v = tk.BooleanVar(value=True)   # ← new toggle

        def add_param(label, var, width=7):
            f = ttk.Frame(ctl); f.pack(side=tk.LEFT, padx=2)
            ttk.Label(f, text=label).pack()
            ttk.Entry(f, width=width, textvariable=var).pack()

        for lbl, var, w in [
            ("Init", self.init_v, 7), ("Thr",  self.th_v,   7),
            ("Tgt",  self.targ_v, 7), ("L-L",  self.l_lo_v, 7),
            ("L-H",  self.l_hi_v, 7), ("S-L",  self.s_lo_v, 7),
            ("S-H",  self.s_hi_v, 7), ("Seed", self.seed_v, 4)
        ]:
            add_param(lbl, var, w)

        ttk.Checkbutton(ctl, text="Skim excess",
                        variable=self.skim_v).pack(side=tk.LEFT, padx=8)

        ttk.Button(ctl, text="▶ Run",
                   command=self.run_once).pack(side=tk.LEFT, padx=6)
        ttk.Button(ctl, text="📑 Show ledger",
                   command=self.show_ledger).pack(side=tk.LEFT)

        # figure ------------------------------------------------------------
        self.fig, (self.ax_ff, self.ax_not) = plt.subplots(
            2, 1, figsize=(9, 5), sharex=True,
            gridspec_kw={"height_ratios": [2, 1]}
        )
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # status line -------------------------------------------------------
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status,
                  padding=4).pack(anchor=tk.W)

        self.df_last = None
        self.win_ledger = None

    # ---------- callbacks ----------
    def pick_file(self):
        p = filedialog.askopenfilename(
            filetypes=[("Excel/CSV", "*.xlsx *.xls *.csv")])
        if p:
            self.file.set(p)

    def run_once(self):
        if not self.file.get():
            messagebox.showwarning("Нет файла", "Выберите файл ставок")
            return
        try:
            rates = load_funding(Path(self.file.get()))
        except Exception as e:
            messagebox.showerror("Ошибка чтения", str(e))
            return

        random.seed(self.seed_v.get())
        self.df_last = sim(
            rates,
            initial=self.init_v.get(),
            thresh=self.th_v.get(),
            target=self.targ_v.get(),
            l_rng=(self.l_lo_v.get(), self.l_hi_v.get()),
            s_rng=(self.s_lo_v.get(), self.s_hi_v.get()),
            skim_enabled=self.skim_v.get()          # ← pass flag
        )
        self.draw_plots(self.df_last)

        top_sum  = self.df_last["top_up"].sum()
        skim_sum = self.df_last["skim"].sum()
        final    = self.df_last["balance_after"].iloc[-1]
        net      = final - self.init_v.get() - top_sum + skim_sum
        mode     = "ON" if self.skim_v.get() else "OFF"
        self.status.set(
            f"Skim {mode} | Top-up {top_sum:.2f} | Skim$ {skim_sum:.2f} | "
            f"Final FF {final:.2f} | Net P/L {net:+.2f}"
        )

        if self.win_ledger:
            self.populate_ledger()

    # ---------- plotting ----------
    def draw_plots(self, df: pd.DataFrame):
        self.ax_ff.clear(); self.ax_not.clear()

        self.ax_ff.plot(df["timestamp"], df["balance_after"],
                        marker="o", markersize=2, linewidth=1,
                        label="Funding Fund")
        self.ax_ff.axhline(self.th_v.get(), linestyle=":", linewidth=0.8,
                           label="Threshold")
        self.ax_ff.axhline(self.targ_v.get(), linestyle="--",
                           linewidth=0.8, color="green", label="Target")
        self.ax_ff.set_ylabel("FF, USDT")
        self.ax_ff.grid(True, linestyle=":", linewidth=0.4)
        self.ax_ff.legend(fontsize=8, loc="upper left")

        self.ax_not.plot(df["timestamp"], df["notional_long"],
                         "--", linewidth=1, label="Notional long")
        self.ax_not.plot(df["timestamp"], df["notional_short"],
                         "--", linewidth=1, label="Notional short")
        self.ax_not.set_ylabel("Notional")
        self.ax_not.grid(True, linestyle=":", linewidth=0.4)
        self.ax_not.legend(fontsize=8, loc="upper left")

        self.ax_not.xaxis.set_major_formatter(
            mdates.DateFormatter('%Y-%m-%d %H:%M'))
        self.ax_not.tick_params(axis="x", rotation=45, labelsize=8)

        self.fig.tight_layout()
        self.canvas.draw_idle()

    # ---------- ledger ----------
    def show_ledger(self):
        if self.win_ledger and tk.Toplevel.winfo_exists(self.win_ledger):
            self.win_ledger.lift()
            return

        self.win_ledger = tk.Toplevel(self)
        self.win_ledger.title("Ledger")
        self.win_ledger.geometry("1050x420")

        frame = ttk.Frame(self.win_ledger)
        frame.pack(fill=tk.BOTH, expand=True)

        cols = ("ts", "rate", "nL", "nS", "in", "out",
                "top", "skim", "bal")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")

        for c, txt, w in [
            ("ts",  "timestamp", 150), ("rate", "rate", 70),
            ("nL",  "notL", 100),     ("nS",  "notS", 100),
            ("in",  "in", 80),        ("out", "out", 80),
            ("top", "top-up", 80),    ("skim", "skim", 80),
            ("bal", "balance", 100)
        ]:
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w,
                             anchor=tk.E if c != "ts" else tk.W)

        vsb = ttk.Scrollbar(frame, orient="vertical",
                            command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal",
                            command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set,
                            xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        def _wheel(e):
            delta = -1 if e.delta < 0 else 1
            self.tree.yview_scroll(delta, "units")
        self.tree.bind("<MouseWheel>", _wheel)                      # Win/Mac
        self.tree.bind("<Button-4>",
                       lambda e: self.tree.yview_scroll(-1, "units"))  # Linux up
        self.tree.bind("<Button-5>",
                       lambda e: self.tree.yview_scroll(+1, "units"))  # Linux down

        if self.df_last is not None:
            self.populate_ledger()

    def populate_ledger(self):
        self.tree.delete(*self.tree.get_children())
        for _, r in self.df_last.iterrows():
            self.tree.insert("", tk.END, values=(
                r["timestamp"].strftime("%Y-%m-%d %H:%M"),
                f"{r['funding_rate']:.6f}",
                f"{r['notional_long']:.0f}",
                f"{r['notional_short']:.0f}",
                f"{r['inflow']:.2f}",
                f"{r['outflow']:.2f}",
                f"{r['top_up']:.2f}",
                f"{r['skim']:.2f}",
                f"{r['balance_after']:.2f}"
            ))


# ──────────── run ────────────
if __name__ == "__main__":
    FundingApp().mainloop()
