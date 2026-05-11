
import os
import sys
import json
import shutil
import zipfile
import hashlib
import logging
import threading
import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path


# ─────────────────────────────────────────────
#  Логування
# ─────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.expanduser("~"), "backup_pro.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("BackupPro")

CONFIG_FILE = os.path.join(os.path.expanduser("~"), "backup_pro_config.json")


# ─────────────────────────────────────────────
#  Конфігурація
# ─────────────────────────────────────────────
def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"sources": [], "destination": "", "compress": True, "keep_versions": 5}


def save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def human_size(size: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def count_files(sources: list) -> tuple:
    total_files, total_bytes = 0, 0
    for src in sources:
        p = Path(src)
        if p.is_file():
            total_files += 1
            total_bytes += p.stat().st_size
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    total_files += 1
                    total_bytes += f.stat().st_size
    return total_files, total_bytes


# ─────────────────────────────────────────────
#  Ядро бекапу
# ─────────────────────────────────────────────
class BackupEngine:
    def __init__(self, config, progress_cb=None, log_cb=None):
        self.config = config
        self.progress_cb = progress_cb or (lambda v, t: None)
        self.log_cb = log_cb or (lambda m, t: None)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self) -> bool:
        sources = self.config.get("sources", [])
        dest = self.config.get("destination", "")
        compress = self.config.get("compress", True)
        keep = self.config.get("keep_versions", 5)

        if not sources:
            self.log_cb("Не вибрано жодного джерела!", "error")
            return False
        if not dest:
            self.log_cb("Не вказано папку призначення!", "error")
            return False

        os.makedirs(dest, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        name = f"backup_{stamp}"
        self.log_cb(f"Початок: {stamp}", "info")

        ok = self._zip(sources, dest, name) if compress else self._copy(sources, dest, name)
        if ok:
            self._rotate(dest, keep)
            self.log_cb("Готово! Резервне копіювання завершено.", "success")
        else:
            self.log_cb("Перервано.", "error")
        return ok

    def _zip(self, sources, dest, name) -> bool:
        path = os.path.join(dest, name + ".zip")
        _, total = count_files(sources)
        done = 0
        self.log_cb(f"Архів: {path}", "info")
        try:
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
                for src in sources:
                    if self._stop:
                        return False
                    p = Path(src)
                    if p.is_file():
                        zf.write(p, p.name)
                        done += p.stat().st_size
                        self.progress_cb(done, total)
                        self.log_cb(f"  + {p.name}", "file")
                    elif p.is_dir():
                        for f in p.rglob("*"):
                            if self._stop:
                                return False
                            if f.is_file():
                                arc = os.path.join(p.name, f.relative_to(p))
                                zf.write(f, arc)
                                done += f.stat().st_size
                                self.progress_cb(done, total)
                                self.log_cb(f"  + {arc}", "file")
            sz = os.path.getsize(path)
            ratio = (1 - sz / total) * 100 if total else 0
            self.log_cb(f"Розмір архіву: {human_size(sz)} (стиснення {ratio:.1f}%)", "info")
            return True
        except Exception as e:
            self.log_cb(f"Помилка: {e}", "error")
            return False

    def _copy(self, sources, dest, name) -> bool:
        out = os.path.join(dest, name)
        os.makedirs(out, exist_ok=True)
        _, total = count_files(sources)
        done = 0
        self.log_cb(f"Папка: {out}", "info")
        try:
            for src in sources:
                if self._stop:
                    return False
                p = Path(src)
                if p.is_file():
                    shutil.copy2(p, out)
                    done += p.stat().st_size
                    self.progress_cb(done, total)
                    self.log_cb(f"  + {p.name}", "file")
                elif p.is_dir():
                    sub = os.path.join(out, p.name)
                    for f in p.rglob("*"):
                        if self._stop:
                            return False
                        if f.is_file():
                            rel = f.relative_to(p)
                            tgt = Path(sub) / rel
                            tgt.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(f, tgt)
                            done += f.stat().st_size
                            self.progress_cb(done, total)
                            self.log_cb(f"  + {p.name}/{rel}", "file")
            return True
        except Exception as e:
            self.log_cb(f"Помилка: {e}", "error")
            return False

    def _rotate(self, dest, keep):
        entries = sorted(
            [e for e in Path(dest).iterdir() if e.name.startswith("backup_")],
            key=lambda e: e.stat().st_mtime,
        )
        while len(entries) > keep:
            old = entries.pop(0)
            try:
                shutil.rmtree(old) if old.is_dir() else old.unlink()
                self.log_cb(f"Видалено старий бекап: {old.name}", "info")
            except Exception as e:
                self.log_cb(f"Не вдалося видалити {old.name}: {e}", "warn")


# ─────────────────────────────────────────────
#  Кольори
# ─────────────────────────────────────────────
BG      = "#0f1117"
PANEL   = "#1a1d27"
ACCENT  = "#4f8ef7"
FG      = "#e8eaf0"
MUTED   = "#6b7280"
RED     = "#f87171"
GREEN   = "#4ade80"
YELLOW  = "#fbbf24"
MONO    = ("Consolas", 9)
UI      = ("Segoe UI", 9)
UI_B    = ("Segoe UI", 9, "bold")


# ─────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────
class BackupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BackupPro — Утиліта резервного копіювання")
        self.configure(bg=BG)
        self.geometry("960x700")
        self.minsize(800, 580)
        self.config_data = load_config()
        self._engine = None
        self._thread = None
        self._build()
        self._refresh_list()

    # ── Розмітка ─────────────────────────────
    def _build(self):
        # 1. Заголовок (фіксований зверху)
        hdr = tk.Frame(self, bg=PANEL, pady=10)
        hdr.pack(side="top", fill="x")
        tk.Label(hdr, text="  BackupPro", font=("Segoe UI", 15, "bold"),
                 fg=ACCENT, bg=PANEL).pack(side="left", padx=16)
        tk.Label(hdr, text="Утиліта резервного копіювання файлів",
                 font=UI, fg=MUTED, bg=PANEL).pack(side="left")

        # 2. Кнопки дій (фіксовані знизу)
        bar = tk.Frame(self, bg=BG, pady=8)
        bar.pack(side="bottom", fill="x", padx=10)
        self._btn_start = self._btn(bar, "▶  Запустити бекап", self._start, ACCENT)
        self._btn_start.pack(side="left", padx=(0, 6))
        self._btn_stop = self._btn(bar, "⏹  Зупинити", self._stop_eng, "#7f1d1d")
        self._btn_stop.pack(side="left")
        self._btn_stop.config(state="disabled")
        self._btn(bar, "💾  Зберегти налаштування", self._save, "#374151").pack(side="right")

        # 3. Центральна область (розтягується)
        mid = tk.Frame(self, bg=BG)
        mid.pack(side="top", fill="both", expand=True, padx=10, pady=6)

        # 3а. Верхні дві панелі поряд (фіксована висота ~260px)
        top = tk.Frame(mid, bg=BG, height=260)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)   # ← фіксує висоту

        # Ліва панель — джерела
        lf = tk.Frame(top, bg=PANEL)
        lf.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self._section_header(lf, "Джерела резервного копіювання")
        self._build_sources(lf)

        # Права панель — налаштування
        rf = tk.Frame(top, bg=PANEL)
        rf.pack(side="left", fill="both", expand=True, padx=(5, 0))
        self._section_header(rf, "Налаштування")
        self._build_settings(rf)

        # 3б. Нижня панель — журнал (займає решту місця)
        bf = tk.Frame(mid, bg=PANEL)
        bf.pack(side="top", fill="both", expand=True, pady=(6, 0))
        self._section_header(bf, "Журнал операцій")
        self._build_log(bf)

    def _section_header(self, parent, title):
        tk.Label(parent, text=title, font=UI_B, fg=ACCENT, bg=PANEL
                 ).pack(anchor="w", padx=10, pady=(8, 2))
        tk.Frame(parent, bg=ACCENT, height=1).pack(fill="x", padx=10)

    def _btn(self, parent, text, cmd, color):
        return tk.Button(parent, text=text, command=cmd,
                         bg=color, fg="#fff", activebackground=color,
                         activeforeground="#fff", font=UI_B,
                         borderwidth=0, cursor="hand2",
                         padx=12, pady=5, relief="flat")

    # ── Джерела ──────────────────────────────
    def _build_sources(self, parent):
        wrap = tk.Frame(parent, bg=PANEL, padx=10, pady=6)
        wrap.pack(fill="both", expand=True)

        brow = tk.Frame(wrap, bg=PANEL)
        brow.pack(fill="x", pady=(0, 6))
        self._btn(brow, "+ Файл",     self._add_file,   "#1e40af").pack(side="left", padx=(0, 4))
        self._btn(brow, "+ Папка",    self._add_folder, "#1e40af").pack(side="left", padx=(0, 4))
        self._btn(brow, "✕ Видалити", self._remove,     "#7f1d1d").pack(side="right")

        lf = tk.Frame(wrap, bg=BG)
        lf.pack(fill="both", expand=True)
        sb = tk.Scrollbar(lf, bg=PANEL, troughcolor=BG)
        sb.pack(side="right", fill="y")
        self._listbox = tk.Listbox(
            lf, bg=BG, fg=FG, font=MONO,
            selectbackground=ACCENT, selectforeground="#fff",
            borderwidth=0, highlightthickness=0,
            yscrollcommand=sb.set, activestyle="none"
        )
        self._listbox.pack(side="left", fill="both", expand=True)
        sb.config(command=self._listbox.yview)

        self._count_lbl = tk.Label(wrap, text="", font=("Segoe UI", 8),
                                   fg=MUTED, bg=PANEL)
        self._count_lbl.pack(anchor="w", pady=(4, 0))

    # ── Налаштування ─────────────────────────
    def _build_settings(self, parent):
        wrap = tk.Frame(parent, bg=PANEL, padx=10, pady=6)
        wrap.pack(fill="both", expand=True)

        tk.Label(wrap, text="Папка призначення:", font=UI, fg=MUTED, bg=PANEL
                 ).pack(anchor="w")
        dr = tk.Frame(wrap, bg=PANEL)
        dr.pack(fill="x", pady=(2, 10))
        self._dest_var = tk.StringVar(value=self.config_data.get("destination", ""))
        tk.Entry(dr, textvariable=self._dest_var,
                 bg=BG, fg=FG, insertbackground=FG, font=MONO,
                 borderwidth=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground=MUTED
                 ).pack(side="left", fill="x", expand=True, ipady=4)
        self._btn(dr, "…", self._browse, "#374151").pack(side="left", padx=(4, 0))

        self._compress_var = tk.BooleanVar(value=self.config_data.get("compress", True))
        tk.Checkbutton(wrap, text="Стискати у ZIP-архів",
                       variable=self._compress_var,
                       bg=PANEL, fg=FG, selectcolor=BG,
                       activebackground=PANEL, activeforeground=ACCENT,
                       font=UI, borderwidth=0).pack(anchor="w", pady=(0, 10))

        tk.Label(wrap, text="Зберігати останніх версій:", font=UI, fg=MUTED, bg=PANEL
                 ).pack(anchor="w")
        self._keep_var = tk.IntVar(value=self.config_data.get("keep_versions", 5))
        tk.Spinbox(wrap, from_=1, to=50, textvariable=self._keep_var,
                   bg=BG, fg=FG, insertbackground=FG, buttonbackground=PANEL,
                   font=UI, borderwidth=0, highlightthickness=1,
                   highlightcolor=ACCENT, highlightbackground=MUTED, width=6
                   ).pack(anchor="w", pady=(2, 10), ipady=3)

        tk.Frame(wrap, bg=MUTED, height=1).pack(fill="x", pady=4)
        tk.Label(wrap, text="Автозапуск через taskschd.msc →\n  python backup_app.py --run",
                 font=("Segoe UI", 8), fg=MUTED, bg=PANEL, justify="left"
                 ).pack(anchor="w", pady=(4, 0))

        tk.Frame(wrap, bg=MUTED, height=1).pack(fill="x", pady=6)
        self._last_lbl = tk.Label(wrap, text="", font=("Segoe UI", 8), fg=MUTED, bg=PANEL)
        self._last_lbl.pack(anchor="w")
        self._update_last()

    # ── Журнал ───────────────────────────────
    def _build_log(self, parent):
        wrap = tk.Frame(parent, bg=PANEL, padx=10, pady=6)
        wrap.pack(fill="both", expand=True)

        pr = tk.Frame(wrap, bg=PANEL)
        pr.pack(fill="x", pady=(0, 4))
        self._prog_lbl = tk.Label(pr, text="Готово до запуску", font=("Segoe UI", 8),
                                  fg=MUTED, bg=PANEL)
        self._prog_lbl.pack(side="left")
        self._prog_pct = tk.Label(pr, text="", font=("Segoe UI", 8, "bold"),
                                  fg=ACCENT, bg=PANEL)
        self._prog_pct.pack(side="right")

        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("BP.Horizontal.TProgressbar",
                         troughcolor=BG, background=ACCENT,
                         borderwidth=0, thickness=6)
        self._progress = ttk.Progressbar(wrap, style="BP.Horizontal.TProgressbar",
                                          orient="horizontal", mode="determinate")
        self._progress.pack(fill="x", pady=(0, 6))

        lf = tk.Frame(wrap, bg=BG)
        lf.pack(fill="both", expand=True)
        sb = tk.Scrollbar(lf, bg=PANEL, troughcolor=BG)
        sb.pack(side="right", fill="y")
        self._log = tk.Text(lf, bg=BG, fg=FG, font=MONO,
                             insertbackground=FG, borderwidth=0,
                             highlightthickness=0, state="disabled",
                             wrap="none", yscrollcommand=sb.set)
        self._log.pack(side="left", fill="both", expand=True)
        sb.config(command=self._log.yview)

        self._log.tag_config("info",    foreground=FG)
        self._log.tag_config("file",    foreground=MUTED)
        self._log.tag_config("success", foreground=GREEN)
        self._log.tag_config("error",   foreground=RED)
        self._log.tag_config("warn",    foreground=YELLOW)

        tk.Button(wrap, text="Очистити журнал", font=("Segoe UI", 8),
                  fg=MUTED, bg=PANEL, activebackground=BG,
                  activeforeground=FG, borderwidth=0, cursor="hand2",
                  command=self._clear_log).pack(anchor="e", pady=(4, 0))

    # ── Дії з джерелами ──────────────────────
    def _add_file(self):
        paths = filedialog.askopenfilenames(title="Виберіть файли")
        for p in paths:
            if p not in self.config_data["sources"]:
                self.config_data["sources"].append(p)
        self._refresh_list()

    def _add_folder(self):
        p = filedialog.askdirectory(title="Виберіть папку")
        if p and p not in self.config_data["sources"]:
            self.config_data["sources"].append(p)
            self._refresh_list()

    def _remove(self):
        for i in reversed(self._listbox.curselection()):
            del self.config_data["sources"][i]
        self._refresh_list()

    def _refresh_list(self):
        self._listbox.delete(0, "end")
        for s in self.config_data["sources"]:
            icon = "[F] " if os.path.isfile(s) else "[D] "
            self._listbox.insert("end", icon + s)
        tf, tb = count_files(self.config_data["sources"])
        self._count_lbl.config(text=f"Файлів: {tf}  •  {human_size(tb)}")

    def _browse(self):
        p = filedialog.askdirectory(title="Папка для бекапів")
        if p:
            self._dest_var.set(p)

    # ── Збереження ───────────────────────────
    def _save(self):
        self.config_data["destination"]   = self._dest_var.get()
        self.config_data["compress"]      = self._compress_var.get()
        self.config_data["keep_versions"] = self._keep_var.get()
        save_config(self.config_data)
        self._log_append("Налаштування збережено.", "success")

    # ── Запуск / зупинка ─────────────────────
    def _start(self):
        self._save()
        if not self.config_data["sources"]:
            messagebox.showwarning("BackupPro", "Додайте хоча б одне джерело!")
            return
        if not self.config_data["destination"]:
            messagebox.showwarning("BackupPro", "Вкажіть папку призначення!")
            return
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._progress["value"] = 0
        self._engine = BackupEngine(self.config_data,
                                     progress_cb=self._on_prog,
                                     log_cb=self._log_append)
        self._thread = threading.Thread(target=self._run_engine, daemon=True)
        self._thread.start()

    def _run_engine(self):
        self._engine.run()
        self.after(0, self._on_done)

    def _stop_eng(self):
        if self._engine:
            self._engine.stop()
            self._log_append("Зупинка...", "warn")

    def _on_done(self):
        self._btn_start.config(state="normal")
        self._btn_stop.config(state="disabled")
        self._progress["value"] = 100
        self._prog_lbl.config(text="Готово")
        self._prog_pct.config(text="100%")
        self._update_last()

    def _on_prog(self, done, total):
        pct = int(done / total * 100) if total else 0
        self.after(0, lambda: self._set_prog(pct, done, total))

    def _set_prog(self, pct, done, total):
        self._progress["value"] = pct
        self._prog_lbl.config(text=f"{human_size(done)} / {human_size(total)}")
        self._prog_pct.config(text=f"{pct}%")

    # ── Журнал ───────────────────────────────
    def _log_append(self, msg, tag="info"):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self.after(0, lambda: self._write_log(f"[{ts}] {msg}\n", tag))

    def _write_log(self, line, tag):
        self._log.config(state="normal")
        self._log.insert("end", line, tag)
        self._log.see("end")
        self._log.config(state="disabled")

    def _clear_log(self):
        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")

    def _update_last(self):
        dest = self.config_data.get("destination", "")
        if dest and os.path.isdir(dest):
            entries = sorted(
                [e for e in Path(dest).iterdir() if e.name.startswith("backup_")],
                key=lambda e: e.stat().st_mtime, reverse=True,
            )
            if entries:
                mt = datetime.datetime.fromtimestamp(entries[0].stat().st_mtime)
                self._last_lbl.config(text=f"Остання копія: {mt.strftime('%d.%m.%Y %H:%M')}")
                return
        self._last_lbl.config(text="Резервних копій ще немає")


# ─────────────────────────────────────────────
#  CLI-режим
# ─────────────────────────────────────────────
def run_headless():
    config = load_config()
    print("=" * 50)
    print("  BackupPro — Автоматичний запуск")
    print("=" * 50)
    BackupEngine(config,
                 progress_cb=lambda d, t: None,
                 log_cb=lambda m, _: print(m)).run()


if __name__ == "__main__":
    if "--run" in sys.argv:
        run_headless()
    else:
        BackupApp().mainloop()