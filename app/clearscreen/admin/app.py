# -*- coding: utf-8 -*-
"""ClearScreen Admin - the server's management app (Windows desktop, runs only on the server,
as an administrator).

  Dashboard  service status, start / stop / restart, health check
  Clients    add a client (name + Client ID), reset its API key, enable / disable, rename, remove
  Settings   Groq API key, SQL Server connection string, HTTPS domain, limits, the /match/test switch
  Data       load whitelist records from Excel, back up and restore the database
  Embedding  the embedding queue: records waiting per client, progress, time left; pause / resume
  Logs       read the service logs

Start it from the "ClearScreen Admin" shortcut, or: pythonw -m clearscreen.admin
"""
import ctypes
import os
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone
from tkinter import filedialog, messagebox, simpledialog, ttk

from clearscreen import PRODUCT, __version__, db, embedding_queue
from clearscreen.admin import ops, services
from clearscreen.config import SecureStore, Settings, logs_dir
from clearscreen.security import add_client, remove_client, rename_client, reset_key, set_enabled

ACCENT, OK, BAD, WARN, MUTED = "#22324a", "#1e7b4a", "#b3261e", "#9a6700", "#5f6b7a"


def ensure_admin() -> None:
    """Relaunch elevated when not started as an administrator."""
    if ctypes.windll.shell32.IsUserAnAdmin():
        return
    params = " ".join(f'"{a}"' for a in ["-m", "clearscreen.admin", *sys.argv[1:]])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    sys.exit(0)


class AdminApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{PRODUCT} Admin")
        self.geometry("1040x700")
        self.minsize(900, 600)
        self.store = SecureStore()
        self.settings = Settings.load()
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 16), foreground="white", background=ACCENT)
        style.configure("Sub.TLabel", font=("Segoe UI", 9), foreground="#c9d3e0", background=ACCENT)
        style.configure("H.TLabel", font=("Segoe UI Semibold", 11))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Treeview", rowheight=24)

        banner = tk.Frame(self, bg=ACCENT, height=58)
        banner.pack(fill="x")
        ttk.Label(banner, text=f"{PRODUCT} Admin", style="Title.TLabel").pack(side="left", padx=16, pady=10)
        ttk.Label(banner, text=f"version {__version__}  ·  this server only  ·  {self.settings.domain}",
                  style="Sub.TLabel").pack(side="left", pady=(16, 0))

        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status, style="Muted.TLabel", anchor="w").pack(side="bottom", fill="x",
                                                                                          padx=10, pady=4)
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=(8, 0))
        self.nb, self.tabs = nb, {}
        for name, builder in (("Dashboard", self._dashboard), ("Clients", self._clients), ("Settings", self._settings),
                              ("Data", self._data), ("Embedding", self._embedding), ("Logs", self._logs)):
            frame = ttk.Frame(nb, padding=12)
            nb.add(frame, text=f"  {name}  ")
            self.tabs[name] = frame
            builder(frame)
        nb.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab())
        self.after(200, self.refresh_services)
        self.after(400, self.refresh_clients)

    # ------------------------------------------------------------ helpers --
    def busy(self, message, work, done=None):
        """Run work() in the background; done(result) back on the window thread."""
        self.status.set(message)
        self.config(cursor="watch")

        def run():
            try:
                result, error = work(), None
            except Exception as e:                                # noqa: BLE001
                result, error = None, e
            self.after(0, finish, result, error)

        def finish(result, error):
            self.config(cursor="")
            if error is not None:
                self.status.set("Failed.")
                messagebox.showerror(PRODUCT, f"{message.rstrip('.')} failed:\n\n{error}")
            else:
                self.status.set("Done.")
                if done:
                    done(result)
        threading.Thread(target=run, daemon=True).start()

    def show_key(self, title, client_id, key):
        """Show a new API key once, with a copy button."""
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.grab_set()
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"API key for client {client_id}", style="H.TLabel").pack(anchor="w")
        ttk.Label(f, text="Copy it now and give it to the client securely. It is shown only once:\n"
                          "ClearScreen stores only a fingerprint of it and cannot show it again.",
                  foreground=WARN).pack(anchor="w", pady=(4, 10))
        entry = ttk.Entry(f, width=64, font=("Consolas", 11))
        entry.insert(0, key)
        entry.configure(state="readonly")
        entry.pack(fill="x")

        def copy():
            self.clipboard_clear()
            self.clipboard_append(key)
            copied.set("Copied to the clipboard.")
        copied = tk.StringVar()
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(10, 0))
        ttk.Button(row, text="Copy key", command=copy).pack(side="left")
        ttk.Label(row, textvariable=copied, foreground=OK).pack(side="left", padx=8)
        ttk.Button(row, text="Close", command=win.destroy).pack(side="right")
        win.wait_window()

    # ---------------------------------------------------------- dashboard --
    def _dashboard(self, f):
        ttk.Label(f, text="Services", style="H.TLabel").grid(row=0, column=0, sticky="w")
        self.svc_vars = {}
        for i, (name, what) in enumerate(services.SERVICES, start=1):
            var = tk.StringVar(value="...")
            self.svc_vars[name] = var
            ttk.Label(f, text=name, width=20).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Label(f, textvariable=var, width=14).grid(row=i, column=1, sticky="w")
            ttk.Label(f, text=what, style="Muted.TLabel").grid(row=i, column=2, sticky="w", padx=(0, 10))
            for j, (label, fn) in enumerate((("Start", services.start), ("Stop", services.stop),
                                             ("Restart", services.restart))):
                ttk.Button(f, text=label, width=9, command=lambda n=name, fn=fn, label=label: self.busy(
                    f"{label} {n}...", lambda: fn(n), lambda _r: self.refresh_services())).grid(row=i, column=3 + j, padx=2)
        row = len(services.SERVICES) + 1
        bar = ttk.Frame(f)
        bar.grid(row=row, column=0, columnspan=6, sticky="w", pady=(10, 4))
        ttk.Button(bar, text="Start all", command=lambda: self.busy("Starting all services...", services.start_all,
                                                                    lambda _r: self.refresh_services())).pack(side="left")
        ttk.Button(bar, text="Stop all", command=self.stop_all).pack(side="left", padx=6)
        ttk.Button(bar, text="Restart all", command=lambda: self.busy("Restarting all services...", services.restart_all,
                                                                      lambda _r: self.refresh_services())).pack(side="left")
        ttk.Button(bar, text="Refresh", command=self.refresh_services).pack(side="left", padx=6)

        ttk.Separator(f).grid(row=row + 1, column=0, columnspan=6, sticky="ew", pady=12)
        ttk.Label(f, text="Health check", style="H.TLabel").grid(row=row + 2, column=0, sticky="w")
        ttk.Button(f, text="Run health check", command=self.run_health).grid(row=row + 2, column=1, sticky="w")
        self.health_text = tk.Text(f, height=14, width=110, font=("Consolas", 10), relief="flat", background="#f4f6f9")
        self.health_text.grid(row=row + 3, column=0, columnspan=6, sticky="nsew", pady=(6, 0))
        f.rowconfigure(row + 3, weight=1)
        f.columnconfigure(2, weight=1)

    def refresh_services(self):
        def work():
            return {n: services.status(n) for n in services.NAMES}

        def done(st):
            for n, s in st.items():
                self.svc_vars[n].set({"RUNNING": "● Running", "STOPPED": "○ Stopped",
                                      "NOT_INSTALLED": "Not installed"}.get(s, s.title()))
        self.busy("Reading service status...", work, done)

    def stop_all(self):
        if messagebox.askyesno(PRODUCT, "Stop all ClearScreen services?\n\nCallers will get no answers until "
                                        "the services are started again."):
            self.busy("Stopping all services...", services.stop_all, lambda _r: self.refresh_services())

    def run_health(self):
        def work():
            lines = []
            for n in services.NAMES:
                lines.append(f"{n:20} {services.status(n)}")
            try:
                h = ops.health(Settings.load())
                lines.append(f"\nAPI /health: {h['status'].upper()}  (version {h['version']})")
                for k, v in h["components"].items():
                    lines.append(f"  {k:20} {v}")
            except Exception as e:                                # noqa: BLE001
                lines.append(f"\nAPI /health: NOT REACHABLE ({type(e).__name__})")
            lines.append("\nHTTPS certificate: " + ops.certificate_status(Settings.load()))
            cs = self.store.get("sql_connection_string")
            lines.append("\nSQL Server: " + (ops.test_connection(cs)[1] if cs else "no connection string set"))
            if cs:
                try:
                    con = db.connect(cs)
                    counts = db.count_by_client(con)
                    con.close()
                    lines.append("Whitelist records per client: " +
                                 (", ".join(f"{c}: {n:,}" for c, n in sorted(counts.items())) or "none yet"))
                except Exception as e:                            # noqa: BLE001
                    lines.append(f"Whitelist records: could not read ({type(e).__name__})")
            key = self.store.get("groq_api_key")
            lines.append("Groq: " + (ops.test_groq_key(key, Settings.load().groq_model)[1] if key else "no API key set"))
            return "\n".join(lines)

        def done(text):
            self.health_text.delete("1.0", "end")
            self.health_text.insert("1.0", text)
        self.busy("Running health check...", work, done)

    # ------------------------------------------------------------ clients --
    def _clients(self, f):
        ttk.Label(f, text="Clients and their API keys", style="H.TLabel").pack(anchor="w")
        ttk.Label(f, text="Each client gets one API key. A key works only for its own client's whitelist records.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))
        cols = ("id", "name", "status", "key", "rotated", "records")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=14, selectmode="browse")
        for c, label, w in (("id", "Client ID", 140), ("name", "Name", 230), ("status", "Status", 80),
                            ("key", "API key (hint)", 170), ("rotated", "Key last set (UTC)", 160),
                            ("records", "Whitelist records", 120)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(8, 0))
        for label, cmd in (("Add client...", self.add_client), ("Reset API key...", self.reset_key),
                           ("Enable / disable", self.toggle_client), ("Rename...", self.rename_client),
                           ("Remove client...", self.remove_client), ("Refresh", self.refresh_clients)):
            ttk.Button(bar, text=label, command=cmd).pack(side="left", padx=(0, 6))

    def _selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(PRODUCT, "Select a client first.")
            return None
        return sel[0]

    def refresh_clients(self):
        def work():
            counts = {}
            cs = self.store.get("sql_connection_string")
            if cs:
                try:
                    con = db.connect(cs)
                    counts = {k.lower(): v for k, v in db.count_by_client(con).items()}
                    con.close()
                except Exception:                                 # noqa: BLE001
                    counts = None
            return self.store.clients(), counts

        def done(result):
            clients, counts = result
            self.tree.delete(*self.tree.get_children())
            for cid, c in sorted(clients.items()):
                n = "?" if counts is None else f"{counts.get(cid.lower(), 0):,}"
                self.tree.insert("", "end", iid=cid, values=(cid, c.get("name"), "Enabled" if c.get("enabled", True)
                                                             else "Disabled", c.get("key_hint"), c.get("rotated_utc"), n))
        self.busy("Loading clients...", work, done)

    def add_client(self):
        win = tk.Toplevel(self)
        win.title("Add client")
        win.transient(self)
        win.grab_set()
        f = ttk.Frame(win, padding=16)
        f.pack()
        ttk.Label(f, text="Client name").grid(row=0, column=0, sticky="w")
        name = ttk.Entry(f, width=40)
        name.grid(row=0, column=1, pady=4)
        ttk.Label(f, text="Client ID").grid(row=1, column=0, sticky="w")
        cid = ttk.Entry(f, width=40)
        cid.grid(row=1, column=1, pady=4)
        ttk.Label(f, text="Client ID: letters, digits, '.', '_' or '-' (up to 100). It must match the ClientID\n"
                          "the client sends in its requests and in its whitelist records.",
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 10))

        def save():
            try:
                key = add_client(self.store, cid.get(), name.get())
            except ValueError as e:
                messagebox.showerror(PRODUCT, str(e), parent=win)
                return
            win.destroy()
            self.refresh_clients()
            self.show_key("New client", cid.get().strip(), key)
        ttk.Button(f, text="Add client and create API key", command=save).grid(row=3, column=1, sticky="e")
        name.focus_set()

    def reset_key(self):
        cid = self._selected()
        if cid and messagebox.askyesno(PRODUCT, f"Create a new API key for {cid}?\n\nThe current key stops working "
                                                "immediately. The client must switch to the new key."):
            key = reset_key(self.store, cid)
            self.refresh_clients()
            self.show_key("New API key", cid, key)

    def toggle_client(self):
        cid = self._selected()
        if not cid:
            return
        enabled = self.store.clients()[cid].get("enabled", True)
        verb = "Disable" if enabled else "Enable"
        if messagebox.askyesno(PRODUCT, f"{verb} client {cid}?" + ("\n\nIts API key will be refused until enabled "
                                                                   "again. Its whitelist records are kept." if enabled else "")):
            set_enabled(self.store, cid, not enabled)
            self.refresh_clients()

    def rename_client(self):
        cid = self._selected()
        if cid:
            new = simpledialog.askstring(PRODUCT, f"New name for {cid}:", initialvalue=self.store.clients()[cid]["name"],
                                         parent=self)
            if new:
                try:
                    rename_client(self.store, cid, new)
                except ValueError as e:
                    messagebox.showerror(PRODUCT, str(e))
                self.refresh_clients()

    def remove_client(self):
        cid = self._selected()
        if not cid:
            return
        typed = simpledialog.askstring(PRODUCT, f"Remove client {cid} and its API key?\n\nTo also delete all of its "
                                                "whitelist records, type DELETE RECORDS. To keep the records, type "
                                                "REMOVE. Anything else cancels.", parent=self)
        if typed not in ("REMOVE", "DELETE RECORDS"):
            return

        def work():
            deleted = 0
            if typed == "DELETE RECORDS":
                con = db.connect(self.store.get("sql_connection_string"))
                deleted = db.delete_client_records(con, cid)
                con.commit()
                con.close()
            remove_client(self.store, cid)
            return deleted
        self.busy(f"Removing {cid}...", work, lambda n: (self.refresh_clients(), messagebox.showinfo(
            PRODUCT, f"Client {cid} removed." + (f" {n:,} whitelist records deleted." if typed == "DELETE RECORDS" else ""))))

    # ----------------------------------------------------------- settings --
    def _settings(self, f):
        s = self.settings
        ttk.Label(f, text="Groq API key", style="H.TLabel").grid(row=0, column=0, sticky="w")
        self.groq_state = tk.StringVar()
        ttk.Label(f, textvariable=self.groq_state).grid(row=0, column=1, sticky="w")
        ttk.Button(f, text="Set / replace key...", command=self.set_groq_key).grid(row=0, column=2, padx=4)
        ttk.Button(f, text="Test key", command=self.test_groq).grid(row=0, column=3)

        ttk.Separator(f).grid(row=1, column=0, columnspan=4, sticky="ew", pady=10)
        ttk.Label(f, text="SQL Server connection", style="H.TLabel").grid(row=2, column=0, sticky="w")
        self.sql_state = tk.StringVar()
        ttk.Label(f, textvariable=self.sql_state, wraplength=560).grid(row=2, column=1, sticky="w")
        ttk.Button(f, text="Edit...", command=self.edit_sql).grid(row=2, column=2, padx=4)
        ttk.Button(f, text="Test connection", command=self.test_sql).grid(row=2, column=3)

        ttk.Separator(f).grid(row=3, column=0, columnspan=4, sticky="ew", pady=10)
        ttk.Label(f, text="HTTPS certificate", style="H.TLabel").grid(row=4, column=0, sticky="w")
        self.cert_state = tk.StringVar()
        ttk.Label(f, textvariable=self.cert_state, wraplength=560).grid(row=4, column=1, sticky="w")
        ttk.Button(f, text="Replace certificate...", command=self.replace_certificate).grid(row=4, column=2, padx=4)

        ttk.Separator(f).grid(row=5, column=0, columnspan=4, sticky="ew", pady=10)
        ttk.Label(f, text="Address and limits", style="H.TLabel").grid(row=6, column=0, sticky="w")
        self.vars = {}
        for i, (key, label) in enumerate((("domain", "Domain name"),
                                          ("rate_limit_per_minute", "Requests per client per minute"),
                                          ("max_request_mb", "Largest request (MB)"),
                                          ("ai_max_cases", "AI reviews per alert (records)")), start=7):
            ttk.Label(f, text=label).grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=str(getattr(s, key)))
            self.vars[key] = var
            ttk.Entry(f, textvariable=var, width=42).grid(row=i, column=1, sticky="w")
        self.docs_var = tk.BooleanVar(value=s.enable_docs)
        ttk.Checkbutton(f, text="Publish the API documentation page (/docs)", variable=self.docs_var).grid(
            row=11, column=1, sticky="w", pady=4)
        ttk.Button(f, text="Save and restart services", command=self.save_settings).grid(row=12, column=1, sticky="w",
                                                                                         pady=8)

        ttk.Separator(f).grid(row=13, column=0, columnspan=4, sticky="ew", pady=10)
        ttk.Label(f, text="Test endpoint", style="H.TLabel").grid(row=14, column=0, sticky="nw")
        self.test_state = tk.StringVar()
        ttk.Label(f, textvariable=self.test_state, wraplength=560).grid(row=14, column=1, sticky="w")
        self.test_button = ttk.Button(f, command=self.toggle_test_endpoint)
        self.test_button.grid(row=14, column=2, padx=4, sticky="n")
        f.columnconfigure(1, weight=1)
        self.refresh_secret_state()
        self.refresh_test_endpoint()

    def refresh_test_endpoint(self):
        on = Settings.load().test_endpoint_enabled
        self.test_state.set(
            "ON - POST /match/test is listed on the /docs page and answers with every internal detail "
            "(AI provider, timings, candidates, reviews). Any client's key can call it." if on else
            "OFF - POST /match/test is hidden from the /docs page and answers 404. Clients use /match/main, "
            "which returns the decision only.")
        self.test_button.configure(text="Turn off" if on else "Turn on")

    def toggle_test_endpoint(self):
        s = Settings.load()
        if not s.test_endpoint_enabled and not messagebox.askyesno(
                PRODUCT, "Turn on POST /match/test?\n\nIt will be listed on the public /docs page, and any client "
                         "with a valid API key can call it and see internal details, including the AI provider. "
                         "Turn it off again when you are done testing."):
            return
        s.test_endpoint_enabled = not s.test_endpoint_enabled
        s.save()
        self.settings = s
        self.refresh_test_endpoint()
        self.status.set(f"/match/test turned {'on' if s.test_endpoint_enabled else 'off'} - takes effect within "
                        "10 seconds, no restart needed.")

    def refresh_secret_state(self):
        self.groq_state.set("Configured" if self.store.get("groq_api_key") else "NOT SET - matching will not work")
        self.sql_state.set(ops.mask_connection_string(self.store.get("sql_connection_string")))
        self.cert_state.set(ops.certificate_status(Settings.load()))

    def replace_certificate(self):
        from tkinter import filedialog
        s = Settings.load()
        messagebox.showinfo(PRODUCT, f"Choose the HTTPS certificate for {s.domain}:\n\n"
                                     "- a .pfx file (certificate and private key in one, with a password), or\n"
                                     "- a PEM certificate file; you are then asked for its private key file.")
        cert = filedialog.askopenfilename(parent=self, title="The certificate (.pfx or PEM)",
                                          filetypes=[("Certificate", "*.pfx *.p12 *.pem *.crt *.cer"), ("All files", "*.*")])
        if not cert:
            return
        if cert.lower().endswith(ops.PFX_SUFFIXES):
            password = simpledialog.askstring(PRODUCT, "Password of the .pfx file:", show="•", parent=self)
            if password is None:
                return
            key = cert                                            # the .pfx holds the key; delete it afterwards
        else:
            password = None
            key = filedialog.askopenfilename(parent=self, title="Its private key (PEM)",
                                             filetypes=[("PEM private key", "*.pem *.key"), ("All files", "*.*")])
            if not key:
                return

        def work():
            info = (ops.install_pfx(cert, password, s.domain) if password is not None
                    else ops.install_certificate(cert, key, s.domain))
            s.tls_mode = "certificate"
            s.save()
            ops.write_caddyfile(s)
            services.restart("ClearScreenHttps")
            return info

        def done(info):
            self.settings = s
            self.refresh_secret_state()
            self.refresh_services()
            messagebox.showinfo(PRODUCT, f"Certificate installed for {', '.join(info['names'])}, expires {info['expires']}. "
                                         "The HTTPS service was restarted.\n\nClearScreen keeps its own protected copy: "
                                         f"delete the file holding the private key ({key}) and any other copies.")
        self.busy("Checking and installing the certificate...", work, done)

    def set_groq_key(self):
        key = simpledialog.askstring(PRODUCT, "Paste the Groq API key (starts with gsk_):", show="•", parent=self)
        if not key:
            return
        key = key.strip()

        def work():
            return ops.test_groq_key(key, Settings.load().groq_model)

        def done(result):
            ok, msg = result
            if not ok and not messagebox.askyesno(PRODUCT, f"{msg}\n\nSave this key anyway?"):
                return
            self.store.update(lambda d: d.update(groq_api_key=key))
            self.refresh_secret_state()
            messagebox.showinfo(PRODUCT, f"Groq key saved. {msg}\nThe API uses it from the next request - no restart needed.")
        self.busy("Checking the Groq key...", work, done)

    def test_groq(self):
        key = self.store.get("groq_api_key")
        if not key:
            messagebox.showwarning(PRODUCT, "No Groq API key is set.")
            return
        self.busy("Testing the Groq key...", lambda: ops.test_groq_key(key, Settings.load().groq_model),
                  lambda r: (messagebox.showinfo if r[0] else messagebox.showerror)(PRODUCT, r[1]))

    def edit_sql(self):
        win = tk.Toplevel(self)
        win.title("SQL Server connection string")
        win.transient(self)
        win.grab_set()
        f = ttk.Frame(win, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="ODBC connection string used by the API service:").pack(anchor="w")
        text = tk.Text(f, width=90, height=5, font=("Consolas", 10))
        text.insert("1.0", self.store.get("sql_connection_string") or
                    "Driver={ODBC Driver 18 for SQL Server};Server=localhost;Database=ClearScreen;"
                    "Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes")
        text.pack(fill="both", expand=True, pady=6)
        ttk.Label(f, text="With Trusted_Connection=yes the API service signs in as NT SERVICE\\ClearScreenApi "
                          "(setup gives it access). For a SQL login use UID=...;PWD=... instead.",
                  style="Muted.TLabel", wraplength=640).pack(anchor="w")
        result = tk.StringVar()
        ttk.Label(f, textvariable=result, wraplength=640).pack(anchor="w", pady=6)
        row = ttk.Frame(f)
        row.pack(fill="x")

        def value():
            return " ".join(text.get("1.0", "end").split())

        def test():
            self.busy("Testing the connection...", lambda: ops.test_connection(value()),
                      lambda r: result.set(("OK: " if r[0] else "FAILED: ") + r[1]))

        def save():
            cs = value()
            ok, msg = ops.test_connection(cs)
            if not ok and not messagebox.askyesno(PRODUCT, f"{msg}\n\nSave anyway?", parent=win):
                return
            if ok and "not created" in msg and messagebox.askyesno(PRODUCT, "Create the ClearScreen tables in this "
                                                                            "database now?", parent=win):
                ops.ensure_schema(cs)
            self.store.update(lambda d: d.update(sql_connection_string=cs))
            win.destroy()
            self.refresh_secret_state()
            if messagebox.askyesno(PRODUCT, "Saved. Restart the API service now to use it?"):
                self.busy("Restarting the API...", lambda: services.restart("ClearScreenApi"),
                          lambda _r: self.refresh_services())
        ttk.Button(row, text="Test connection", command=test).pack(side="left")
        ttk.Button(row, text="Save", command=save).pack(side="right")

    def test_sql(self):
        cs = self.store.get("sql_connection_string")
        if not cs:
            messagebox.showwarning(PRODUCT, "No connection string is set.")
            return
        self.busy("Testing the SQL Server connection...", lambda: ops.test_connection(cs),
                  lambda r: (messagebox.showinfo if r[0] else messagebox.showerror)(PRODUCT, r[1]))

    def save_settings(self):
        s = Settings.load()
        try:
            s.domain = self.vars["domain"].get().strip()
            s.rate_limit_per_minute = max(1, int(self.vars["rate_limit_per_minute"].get()))
            s.max_request_mb = max(1, int(self.vars["max_request_mb"].get()))
            s.ai_max_cases = min(10, max(1, int(self.vars["ai_max_cases"].get())))
        except ValueError:
            messagebox.showerror(PRODUCT, "The limits must be whole numbers.")
            return
        s.enable_docs = self.docs_var.get()
        s.save()
        ops.write_caddyfile(s)
        self.settings = s
        self.busy("Restarting services...", services.restart_all, lambda _r: (self.refresh_services(), messagebox.showinfo(
            PRODUCT, "Settings saved and services restarted.")))

    # --------------------------------------------------------------- data --
    def _data(self, f):
        ttk.Label(f, text="Load whitelist records from Excel", style="H.TLabel").pack(anchor="w")
        ttk.Label(f, text="Columns: ClientID, Unique_ID, Screened_Fields, Screened_Values, Database_Screened, "
                          "Screened_Date, Matched_Details, Analyst_Allow_Reasoning. Every sheet with a ClientID column "
                          "is read; only rows of registered clients are loaded. A ClientID + Unique_ID that exists "
                          "is replaced.", style="Muted.TLabel", wraplength=900).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="Choose Excel file and load...", command=self.load_excel).pack(side="left")
        self.progress = ttk.Progressbar(row, length=360, mode="determinate")
        self.progress.pack(side="left", padx=10)
        self.load_result = tk.Text(f, height=9, font=("Consolas", 10), relief="flat", background="#f4f6f9")
        self.load_result.pack(fill="x")

        ttk.Separator(f).pack(fill="x", pady=12)
        ttk.Label(f, text="Database backup and restore", style="H.TLabel").pack(anchor="w")
        ttk.Label(f, text="Backups go to SQL Server's default backup folder. Client keys and settings are not in the "
                          "database: they stay on this server.", style="Muted.TLabel").pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=8)
        ttk.Button(row, text="Back up now", command=self.backup).pack(side="left")
        ttk.Button(row, text="Restore from a backup...", command=self.restore).pack(side="left", padx=6)
        ttk.Button(row, text="Open backup folder", command=self.open_backups).pack(side="left")

        ttk.Separator(f).pack(fill="x", pady=12)
        ttk.Label(f, text="View the raw data", style="H.TLabel").pack(anchor="w")
        ttk.Label(f, text="Opens SQL Server Management Studio connected to the ClearScreen database with your Windows "
                          "login. You have full rights there: changes bypass ClearScreen's checks, so take a backup "
                          "first. Whitelist records are in the table dbo.WL_Records.", style="Muted.TLabel",
                  wraplength=900).pack(anchor="w")
        ttk.Button(f, text="Open SQL Server Management Studio", command=self.open_ssms).pack(anchor="w", pady=8)

    def load_excel(self):
        cs = self.store.get("sql_connection_string")
        clients = self.store.clients()
        if not cs or not clients:
            messagebox.showwarning(PRODUCT, "Set the SQL Server connection and add at least one client first.")
            return
        path = filedialog.askopenfilename(title="Whitelist workbook", filetypes=[("Excel workbook", "*.xlsx")])
        if not path:
            return

        def progress(done, total):
            self.after(0, lambda: self.progress.configure(maximum=max(total, 1), value=done))

        def done(r):
            lines = [f"Rows read: {r['read']:,}", f"Added: {r['added']:,}", f"Replaced: {r['replaced']:,}",
                     f"Skipped (missing required fields): {r['skipped_incomplete']:,}"]
            if r["skipped_unknown_client"]:
                lines.append("Skipped (ClientID not registered): " +
                             ", ".join(f"{c} x{n:,}" for c, n in r["skipped_unknown_client"].items()))
            lines.append("\nNew records are searchable for similar cases once the API service has embedded them "
                         "(it works through them in the background; see Dashboard > Health check).")
            self.load_result.delete("1.0", "end")
            self.load_result.insert("1.0", "\n".join(lines))
            self.refresh_clients()
        self.busy("Loading the workbook...", lambda: ops.load_excel(cs, path, clients, progress), done)

    def backup(self):
        cs = self.store.get("sql_connection_string")
        self.busy("Backing up the database...", lambda: ops.backup(cs),
                  lambda p: messagebox.showinfo(PRODUCT, f"Backup written:\n{p}"))

    def restore(self):
        cs = self.store.get("sql_connection_string")
        backups = ops.list_backups(cs)
        path = filedialog.askopenfilename(title="Backup to restore", initialdir=os.path.dirname(backups[0]) if backups
                                          else ops.default_backup_dir(cs), filetypes=[("SQL Server backup", "*.bak")])
        if not path:
            return
        typed = simpledialog.askstring(PRODUCT, f"Restore {os.path.basename(path)}?\n\nThis REPLACES all current "
                                                "whitelist records with the backup's. The API is stopped during the "
                                                "restore. Type RESTORE to continue.", parent=self)
        if typed != "RESTORE":
            return

        def work():
            services.stop("ClearScreenApi")
            try:
                ops.restore(cs, path)
            finally:
                services.start("ClearScreenApi")
        self.busy("Restoring the database...", work, lambda _r: (self.refresh_clients(), self.refresh_services(),
                                                                 messagebox.showinfo(PRODUCT, "Database restored.")))

    def open_ssms(self):
        try:
            target = ops.open_ssms(self.store.get("sql_connection_string"))
            self.status.set(f"Opening SQL Server Management Studio ({target})... If it shows a connect window: "
                            "Windows Authentication, tick 'Trust server certificate', Connect.")
        except Exception as e:                                    # noqa: BLE001
            messagebox.showerror(PRODUCT, str(e))

    def open_backups(self):
        try:
            os.startfile(ops.default_backup_dir(self.store.get("sql_connection_string")))
        except Exception as e:                                    # noqa: BLE001
            messagebox.showerror(PRODUCT, f"Could not open the backup folder: {e}")

    # ---------------------------------------------------------- embedding --
    def _embedding(self, f):
        ttk.Label(f, text="Embedding queue", style="H.TLabel").pack(anchor="w")
        ttk.Label(f, text="New and replaced whitelist records wait here until the API service has embedded them; "
                          "until then only the exact-repeat check can use them. Clients are worked through oldest "
                          "first, and while an alert is being checked the queue steps aside. Pausing takes effect "
                          "after the current small batch (seconds) and lasts, also across restarts, until you "
                          "resume.", style="Muted.TLabel", wraplength=960).pack(anchor="w", pady=(0, 6))
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.emb_state = tk.StringVar(value="...")
        ttk.Label(bar, textvariable=self.emb_state, style="H.TLabel").pack(side="left")
        for label, cmd in (("Refresh", self.refresh_embedding), ("Resume", lambda: self.set_embedding_paused(False)),
                           ("Pause", lambda: self.set_embedding_paused(True))):
            ttk.Button(bar, text=label, command=cmd).pack(side="right", padx=(6, 0))
        self.emb_message = tk.StringVar()
        ttk.Label(f, textvariable=self.emb_message, style="Muted.TLabel", wraplength=960).pack(anchor="w", pady=(2, 8))

        cols = ("id", "name", "waiting", "done", "taken", "left", "since")
        self.emb_tree = ttk.Treeview(f, columns=cols, show="headings", height=7, selectmode="browse")
        for c, label, w in (("id", "Client ID", 110), ("name", "Name", 190), ("waiting", "Records waiting", 120),
                            ("done", "Embedded (this run)", 150), ("taken", "Time taken", 110),
                            ("left", "Est. time left", 130), ("since", "Waiting since (UTC)", 170)):
            self.emb_tree.heading(c, text=label)
            self.emb_tree.column(c, width=w, anchor="w")
        self.emb_tree.pack(fill="x")
        self.emb_tree.bind("<<TreeviewSelect>>", lambda _e: self.refresh_waiting_records())
        self.emb_total = tk.StringVar()
        ttk.Label(f, textvariable=self.emb_total).pack(anchor="w", pady=(4, 8))

        self.emb_records_title = tk.StringVar(value="Select a client above to see its waiting records.")
        ttk.Label(f, textvariable=self.emb_records_title, style="H.TLabel").pack(anchor="w")
        rcols = ("uid", "added", "size")
        self.emb_records = ttk.Treeview(f, columns=rcols, show="headings", height=10)
        for c, label, w in (("uid", "Unique_ID", 260), ("added", "Added / replaced (UTC)", 190),
                            ("size", "Listing size (characters)", 190)):
            self.emb_records.heading(c, text=label)
            self.emb_records.column(c, width=w, anchor="w")
        self.emb_records.pack(fill="both", expand=True)
        self._emb_after = None
        self._emb_loading = False

    def _on_tab(self):
        if self.nb.select() == str(self.tabs["Embedding"]):
            self.refresh_embedding()

    def _embedding_tick(self):
        """Refresh every 15 s while the Embedding tab is open."""
        self._emb_after = None
        if self.nb.select() == str(self.tabs["Embedding"]):
            self.refresh_embedding()

    @staticmethod
    def _duration(seconds) -> str:
        if seconds is None:
            return "-"
        s = int(round(seconds))
        if s < 60:
            return f"{s} s"
        h, m = divmod(s // 60, 60)
        return f"{h} h {m:02d} min" if h else f"{m} min"

    def refresh_embedding(self):
        if self._emb_loading:
            return
        self._emb_loading = True
        cs = self.store.get("sql_connection_string")
        model = Settings.load().embed_model

        def work():
            status = embedding_queue.read_status()
            queue, error = [], None
            if cs:
                try:
                    con = db.connect(cs)
                    try:
                        queue = embedding_queue.queue_by_client(con, model)
                    finally:
                        con.close()
                except Exception as e:                            # noqa: BLE001
                    error = f"Could not read the queue from SQL Server ({type(e).__name__})."
            else:
                error = "No SQL Server connection is set."
            return status, queue, error

        def done(result, err):
            self._emb_loading = False
            if err is not None:
                self.emb_state.set("Queue: could not be read")
                self.emb_message.set(str(err))
            else:
                self._show_embedding(*result)
            if self._emb_after is None:
                self._emb_after = self.after(15000, self._embedding_tick)

        def run():
            try:
                r, e = work(), None
            except Exception as ex:                               # noqa: BLE001
                r, e = None, ex
            self.after(0, done, r, e)
        threading.Thread(target=run, daemon=True).start()

    def _show_embedding(self, status, queue, error):
        paused_setting = Settings.load().embedding_paused
        state = status.get("state", "unknown")
        updated = status.get("updated_utc")
        age = None
        if updated:
            try:
                age = (datetime.now(timezone.utc) - datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ")
                       .replace(tzinfo=timezone.utc)).total_seconds()
            except ValueError:
                age = None
        label = {"running": "● Running", "paused": "Paused", "idle": "Idle - nothing waiting",
                 "error": "Problem - retrying", "starting": "Starting"}.get(state, "Unknown")
        if paused_setting and state != "paused":
            label = "Pausing... (after the current batch)"
        elif not paused_setting and state == "paused":
            label = "Resuming..."
        self.emb_state.set(f"Queue: {label}")
        msg = status.get("message") or ""
        if not status:
            msg = "The API service has not reported yet (is ClearScreenApi running? See Dashboard)."
        elif age is not None and age > 300 and state == "running":
            msg = (f"No report from the API service for {self._duration(age)} - it may be stopped or working on a "
                   "very long batch. Check Dashboard and Logs.")
        if error:
            msg = f"{msg}  {error}".strip()
        self.emb_message.set(msg + (f"   (last report {updated.replace('T', ' ').rstrip('Z')} UTC)" if updated else ""))

        clients = {k.lower(): v for k, v in self.store.clients().items()}
        stats = {k.lower(): (k, v or {}) for k, v in status.get("clients", {}).items()}
        overall = status.get("overall", {})
        waiting = {q["ClientID"].lower(): q for q in queue}
        selected = self.emb_tree.selection()
        self.emb_tree.delete(*self.emb_tree.get_children())
        total_left, total_waiting, unknown = 0.0, 0, False
        for cid_l in [*waiting, *sorted(set(stats) - set(waiting))]:
            q = waiting.get(cid_l)
            cid, st = stats.get(cid_l, (q["ClientID"] if q else cid_l, {}))
            cid = q["ClientID"] if q else cid
            name = clients.get(cid_l, {}).get("name", "")
            if q:
                left = embedding_queue.estimate(q, st, overall)
                total_waiting += q["waiting"]
                if left is None:
                    unknown = True
                else:
                    total_left += left
                running = bool(st.get("run_started_utc")) and not st.get("finished_utc")
                since = str(q["oldest_utc"])[:19] if q.get("oldest_utc") else ""
                self.emb_tree.insert("", "end", iid=cid, values=(
                    cid, name, f"{q['waiting']:,}", f"{st.get('done', 0):,}" if running else "0",
                    self._duration(st.get("seconds")) if running else "-",
                    f"about {self._duration(left)}" if left is not None else "estimating...", since))
            else:
                last = st.get("last_run") or {}
                if not last.get("done"):
                    continue
                self.emb_tree.insert("", "end", iid=cid, values=(
                    cid, name, "0", f"{last['done']:,} (finished)", self._duration(last.get("seconds")), "done",
                    "finished " + str(last.get("finished_utc") or "")[:16].replace("T", " ")))
        if selected and self.emb_tree.exists(selected[0]):
            self.emb_tree.selection_set(selected[0])
        if total_waiting:
            self.emb_total.set(f"In total: {total_waiting:,} record(s) waiting - about {self._duration(total_left)}"
                               f"{' or more' if unknown else ''} left at the current pace (an estimate: long "
                               "listings take longer).")
        else:
            self.emb_total.set("Nothing waiting: every whitelist record is searchable.")

    def refresh_waiting_records(self):
        sel = self.emb_tree.selection()
        cs = self.store.get("sql_connection_string")
        if not sel or not cs:
            return
        cid, model = sel[0], Settings.load().embed_model

        def work():
            con = db.connect(cs)
            try:
                return embedding_queue.waiting_records(con, model, cid, limit=1000)
            finally:
                con.close()

        def done(rows):
            self.emb_records.delete(*self.emb_records.get_children())
            for i, r in enumerate(rows):
                self.emb_records.insert("", "end", iid=str(i), values=(
                    r["Unique_ID"], str(r["added_utc"])[:19], f"{r['chars']:,}"))
            order = "the first 1,000, in the order they will be embedded" if len(rows) == 1000 else \
                "in the order they will be embedded"
            self.emb_records_title.set(f"Waiting records of client {cid} ({order})" if rows
                                       else f"Client {cid} has no records waiting.")
        self.busy(f"Reading the waiting records of {cid}...", work, done)

    def set_embedding_paused(self, paused: bool):
        s = Settings.load()
        if s.embedding_paused == paused:
            self.status.set("The queue is already " + ("paused." if paused else "running."))
            return
        s.embedding_paused = paused
        s.save()
        self.settings = s
        self.status.set("Queue paused - it stops after the current small batch." if paused else
                        "Queue resumed - it continues within a few seconds.")
        self.after(1500, self.refresh_embedding)

    # --------------------------------------------------------------- logs --
    def _logs(self, f):
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Log").pack(side="left")
        self.log_choice = tk.StringVar(value="API")
        ttk.Combobox(row, textvariable=self.log_choice, values=list(ops.log_files()), state="readonly",
                     width=34).pack(side="left", padx=6)
        ttk.Button(row, text="Show", command=self.show_log).pack(side="left")
        ttk.Button(row, text="Open logs folder", command=lambda: os.startfile(logs_dir())).pack(side="left", padx=6)
        self.log_text = tk.Text(f, font=("Consolas", 9), wrap="none", relief="flat", background="#f4f6f9")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))

    def show_log(self):
        text = ops.tail(ops.log_files()[self.log_choice.get()])
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", text)
        self.log_text.see("end")


def main():
    ensure_admin()
    AdminApp().mainloop()


if __name__ == "__main__":
    main()
