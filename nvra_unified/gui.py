from __future__ import annotations
import sys, json, os
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QTabWidget, QComboBox, QPlainTextEdit,
    QMessageBox, QSpinBox, QDoubleSpinBox, QSystemTrayIcon, QMenu, QFileDialog,
    QGroupBox, QGridLayout, QFrame
)
from PySide6.QtGui import QAction

from god.security import GoogleOAuth, GoogleOAuthError, generate_totp_secret, otpauth_uri
from .config import BrokerAccount
from .runtime import UnifiedRuntime
from .setup_state import (
    evaluate_setup_state,
    mark_setup_complete,
    migrate_legacy_auth_state,
)


class NVRAUnifiedWindow(QMainWindow):
    def __init__(self, runtime: UnifiedRuntime):
        super().__init__()
        self.runtime = runtime
        self.config = runtime.config
        self.setWindowTitle("NVRA — Local Trading Control Center")
        self.resize(1380, 900)
        self._legacy_mig = migrate_legacy_auth_state()
        self._build()
        self._setup_tray()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)
        self.refresh()
        self.refresh_setup_status()
        self._route_startup()

    def _build(self):
        tabs = QTabWidget()
        self.tabs = tabs
        self._idx_dashboard = tabs.addTab(self.dashboard_tab(), "Dashboard")
        self._idx_setup = tabs.addTab(self.setup_center_tab(), "Setup Center")
        tabs.addTab(self.crypto_tab(), "Crypto / Brokers")
        tabs.addTab(self.forex_tab(), "Forex / MT5")
        tabs.addTab(self.idx_tab(), "IDX Signal")
        tabs.addTab(self.telegram_tab(), "Telegram")
        tabs.addTab(self.ml_tab(), "ML / Risk / Engine")
        tabs.addTab(self.settings_tab(), "Settings")
        self.setCentralWidget(tabs)

    def dashboard_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        self.dash_status = QLabel("Single-user local mode — no login required")
        self.dash_status.setWordWrap(True)
        l.addWidget(self.dash_status)
        g = QFormLayout()
        self.fields = {}
        for k, n in [("running", "Runtime"), ("crypto", "Crypto"), ("forex", "Forex/MT5"), ("idx", "IDX"), ("telegram", "Telegram"), ("hardware", "Hardware"), ("ml_engine", "ML engine"), ("risk", "Risk"), ("cycles", "Cycles")]:
            v = QLabel("-"); self.fields[k] = v; g.addRow(n, v)
        l.addLayout(g)
        row = QHBoxLayout()
        for text, slot in [("Start runtime", self.start_runtime), ("Grace stop", self.grace_stop), ("Force stop", self.force_stop), ("Refresh", self.refresh)]:
            b = QPushButton(text); b.clicked.connect(slot); row.addWidget(b)
        l.addLayout(row)
        note = QLabel("LIVE remains blocked by ProductionGate. Paper is default. Configure services in Setup Center.")
        note.setWordWrap(True); l.addWidget(note)
        return w

    def setup_center_tab(self):
        w = QWidget(); root = QVBoxLayout(w)
        self.setup_overall = QLabel("Setup status: evaluating…"); self.setup_overall.setWordWrap(True)
        root.addWidget(self.setup_overall)
        # Gemini
        ai = QGroupBox("Gemini AI (optional narrator)"); af = QFormLayout(ai)
        self.gemini_key = QLineEdit(); self.gemini_key.setEchoMode(QLineEdit.Password); self.gemini_key.setPlaceholderText("Paste Gemini API key here")
        af.addRow("API key", self.gemini_key)
        row = QHBoxLayout()
        for text, slot in [("Save", self.save_gemini), ("Test", self.test_gemini), ("Clear", self.clear_gemini)]:
            b = QPushButton(text); b.clicked.connect(slot); row.addWidget(b)
        af.addRow(row); root.addWidget(ai)
        # Google Drive
        goog = QGroupBox("Google Drive (optional backup)"); gof = QFormLayout(goog)
        self.google_client_path = QLineEdit(); self.google_client_path.setReadOnly(True); self.google_client_path.setPlaceholderText("Select OAuth client JSON…")
        gof.addRow("OAuth client JSON", self.google_client_path)
        grow = QHBoxLayout(); pick = QPushButton("Browse…"); pick.clicked.connect(self.pick_google_client); grow.addWidget(pick)
        gof.addRow(grow)
        grow2 = QHBoxLayout()
        save_go = QPushButton("Save path"); save_go.clicked.connect(self.save_google_client)
        glogin = QPushButton("Connect Google (Drive)"); glogin.clicked.connect(self.connect_google)
        gdrive = QPushButton("Backup now"); gdrive.clicked.connect(self.backup_drive)
        grow2.addWidget(save_go); grow2.addWidget(glogin); grow2.addWidget(gdrive); gof.addRow(grow2); root.addWidget(goog)
        # Crypto
        crypto = QGroupBox("Crypto / Brokers"); cf = QFormLayout(crypto)
        self.broker = QComboBox(); self.broker.addItems(["binance", "tokocrypto", "indodax"])
        self.account = QLineEdit("default"); self.api_key = QLineEdit(); self.api_key.setEchoMode(QLineEdit.Password)
        self.api_secret = QLineEdit(); self.api_secret.setEchoMode(QLineEdit.Password)
        cf.addRow("Broker", self.broker); cf.addRow("Account id", self.account); cf.addRow("API key", self.api_key); cf.addRow("API secret", self.api_secret)
        crow = QHBoxLayout()
        for text, slot in [("Save", self.save_exchange), ("Test", self.test_exchange), ("Clear", self.clear_exchange)]:
            b = QPushButton(text); b.clicked.connect(slot); crow.addWidget(b)
        cf.addRow(crow); root.addWidget(crypto)
        # Telegram
        tg = QGroupBox("Telegram"); tf = QFormLayout(tg)
        self.tg_token = QLineEdit(); self.tg_token.setEchoMode(QLineEdit.Password); self.tg_chat = QLineEdit()
        tf.addRow("Bot token", self.tg_token); tf.addRow("Chat id", self.tg_chat)
        trow = QHBoxLayout()
        for text, slot in [("Save", self.save_telegram), ("Test", self.test_telegram), ("Clear", self.clear_telegram)]:
            b = QPushButton(text); b.clicked.connect(slot); trow.addWidget(b)
        tf.addRow(trow); root.addWidget(tg)
        # MT5
        mt = QGroupBox("Forex / MT5"); mf = QFormLayout(mt)
        self.mt5_python_status = QLabel("—"); self.mt5_terminal_status = QLabel("—")
        mf.addRow("Python module", self.mt5_python_status); mf.addRow("Terminal", self.mt5_terminal_status)
        mrow = QHBoxLayout(); det = QPushButton("Detect MT5"); det.clicked.connect(self.detect_mt5); mrow.addWidget(det)
        tst = QPushButton("Test MT5"); tst.clicked.connect(self.test_mt5); mrow.addWidget(tst); mf.addRow(mrow); root.addWidget(mt)
        # Status panel
        st = QGroupBox("Honest status"); sf = QFormLayout(st)
        self.status_labels = {}
        for key in ("telegram", "ai", "crypto", "mt5", "google", "compute", "license", "device"):
            lab = QLabel("—"); self.status_labels[key] = lab; sf.addRow(key.upper(), lab)
        root.addWidget(st)
        brow = QHBoxLayout()
        fin = QPushButton("Mark setup complete → Dashboard"); fin.clicked.connect(self.finish_setup); brow.addWidget(fin)
        ref = QPushButton("Refresh status"); ref.clicked.connect(self.refresh_setup_status); brow.addWidget(ref)
        tall = QPushButton("Test all (safe)"); tall.clicked.connect(self.test_all_safe); brow.addWidget(tall)
        root.addLayout(brow)
        totp_btn = QPushButton("Setup TOTP secret (optional)"); totp_btn.clicked.connect(self.setup_totp); root.addWidget(totp_btn)
        return w

    def crypto_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        l.addWidget(QLabel("Crypto accounts are managed in Setup Center. Portfolio snapshot below."))
        self.crypto_info = QPlainTextEdit(); self.crypto_info.setReadOnly(True); l.addWidget(self.crypto_info)
        return w

    def forex_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        l.addWidget(QLabel("Forex uses the installed MetaTrader 5 terminal. No broker password or API key is stored in NVRA."))
        self.mt5_python_status2 = QLabel("Python module: —"); self.mt5_terminal_status2 = QLabel("Terminal: —")
        l.addWidget(self.mt5_python_status2); l.addWidget(self.mt5_terminal_status2)
        b = QPushButton("Detect / refresh MT5"); b.clicked.connect(self.detect_mt5); l.addWidget(b)
        return w

    def idx_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        l.addWidget(QLabel("IDX Signal — signal-only + simulated portfolio (IDR)."))
        form = QFormLayout(); self.idx_balance = QDoubleSpinBox(); self.idx_balance.setMaximum(1e12); self.idx_balance.setValue(getattr(self.config, "idx_initial_balance", 10_000_000))
        form.addRow("Simulated balance (IDR)", self.idx_balance)
        row = QHBoxLayout(); s = QPushButton("Save"); s.clicked.connect(self.save_idx); r = QPushButton("Reset balance"); r.clicked.connect(self.reset_idx)
        row.addWidget(s); row.addWidget(r); form.addRow(row); l.addLayout(form)
        self.idx_info = QPlainTextEdit(); self.idx_info.setReadOnly(True); l.addWidget(self.idx_info)
        return w

    def telegram_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        l.addWidget(QLabel("Telegram credentials are stored in SecretStore (keyring). Configure in Setup Center."))
        b = QPushButton("Open Setup Center"); b.clicked.connect(lambda: self.tabs.setCurrentIndex(self._idx_setup)); l.addWidget(b)
        return w

    def ml_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        l.addWidget(QLabel("ML evidence → risk gate → execution. LIVE remains fail-closed."))
        self.ml_info = QPlainTextEdit(); self.ml_info.setReadOnly(True); l.addWidget(self.ml_info)
        return w

    def settings_tab(self):
        w = QWidget(); l = QVBoxLayout(w)
        form = QFormLayout()
        self.grace = QSpinBox(); self.grace.setRange(2, 600); self.grace.setValue(getattr(self.config, "grace_stop_seconds", 30))
        form.addRow("Grace stop seconds", self.grace)
        l.addLayout(form)
        s = QPushButton("Save settings"); s.clicked.connect(self.save_settings); l.addWidget(s)
        h = QPushButton("Run health check"); h.clicked.connect(self.run_health_check); l.addWidget(h)
        self.cash_broker = QComboBox(); self.cash_broker.addItems(["binance", "tokocrypto", "indodax"])
        self.cash_amount = QDoubleSpinBox(); self.cash_amount.setMaximum(1e12)
        cf = QFormLayout(); cf.addRow("Cashout broker", self.cash_broker); cf.addRow("Amount IDR", self.cash_amount)
        l.addLayout(cf)
        c = QPushButton("Request cashout (audited, no silent withdraw)"); c.clicked.connect(self.cashout); l.addWidget(c)
        return w

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        menu = QMenu()
        show = QAction("Show", self); show.triggered.connect(self.show); menu.addAction(show)
        quit_a = QAction("Quit (grace)", self); quit_a.triggered.connect(self._quit); menu.addAction(quit_a)
        self.tray.setContextMenu(menu); self.tray.show()

    def _route_startup(self):
        state = evaluate_setup_state(self.runtime)
        if state.needs_setup():
            self.tabs.setCurrentIndex(self._idx_setup)
            self.dash_status.setText(f"Setup required — overall={state.overall.value}. Configure services below, then Mark setup complete.")
        else:
            self.tabs.setCurrentIndex(self._idx_dashboard)
            self.dash_status.setText(f"Ready — overall={state.overall.value}. Single-user local mode.")

    def finish_setup(self):
        mark_setup_complete(self.runtime)
        self.refresh_setup_status()
        self.tabs.setCurrentIndex(self._idx_dashboard)
        QMessageBox.information(self, "Setup", "Setup marked complete. Dashboard opened. You can re-open Setup Center anytime.")

    def start_runtime(self):
        self.runtime.start(); self.refresh()

    def grace_stop(self):
        self.runtime.request_grace_stop(self.config.grace_stop_seconds); self.refresh()

    def force_stop(self):
        self.runtime.force_stop(); self.refresh()

    def save_gemini(self):
        key = self.gemini_key.text().strip()
        if not key:
            QMessageBox.warning(self, "Gemini", "API key is empty"); return
        self.runtime.secrets.set_gemini_api_key(key); self.gemini_key.clear()
        QMessageBox.information(self, "Gemini", "API key stored in SecretStore"); self.refresh_setup_status()

    def test_gemini(self):
        ok = self.runtime.secrets.gemini_configured()
        QMessageBox.information(self, "Gemini", "CONFIGURED" if ok else "NOT CONFIGURED")

    def clear_gemini(self):
        self.runtime.secrets.delete_gemini_api_key(); QMessageBox.information(self, "Gemini", "Cleared"); self.refresh_setup_status()

    def pick_google_client(self):
        path, _ = QFileDialog.getOpenFileName(self, "Google OAuth client JSON", "", "JSON (*.json)")
        if path: self.google_client_path.setText(path)

    def save_google_client(self):
        path = self.google_client_path.text().strip()
        self.config.google_oauth_client_file = path; self.config.google_drive_enabled = bool(path); self.config.save()
        QMessageBox.information(self, "Google", "Client path saved"); self.refresh_setup_status()

    def connect_google(self):
        client_file = (self.config.google_oauth_client_file or self.google_client_path.text() or "").strip()
        if not client_file or not Path(client_file).is_file():
            QMessageBox.warning(self, "Google", "Select and save a valid OAuth client JSON first"); return
        try:
            result = GoogleOAuth(client_file, self.runtime.secrets.google_oauth_token, self.runtime.secrets.set_google_oauth_token).login()
            if not result.get("verified_email") or not result.get("email"):
                raise GoogleOAuthError("Google account is not verified")
            self.config.google_account_email = result["email"]; self.config.save()
            QMessageBox.information(self, "Google", f"Connected: {result['email']}")
        except Exception as exc:
            QMessageBox.warning(self, "Google", str(exc))
        self.refresh_setup_status()

    def save_exchange(self):
        broker = self.broker.currentText(); account = self.account.text().strip() or "default"
        key = self.api_key.text().strip(); secret = self.api_secret.text().strip()
        if not key or not secret:
            QMessageBox.warning(self, "Exchange", "API key and secret required"); return
        self.runtime.secrets.set_exchange(broker, account, key, secret)
        # track in config accounts list if present
        try:
            accounts = list(getattr(self.config, "crypto_accounts", []) or [])
            exists = any(getattr(a, "broker", None) == broker and getattr(a, "account_id", None) == account for a in accounts)
            if not exists:
                accounts.append(BrokerAccount(broker=broker, account_id=account, mode="PAPER"))
                self.config.crypto_accounts = accounts
                self.config.save()
        except Exception:
            pass
        self.api_key.clear(); self.api_secret.clear()
        QMessageBox.information(self, "Exchange", f"Saved {broker}:{account}"); self.refresh_setup_status()

    def test_exchange(self):
        broker = self.broker.currentText(); account = self.account.text().strip() or "default"
        ok = self.runtime.secrets.exchange_configured(broker, account)
        QMessageBox.information(self, "Exchange", "CONFIGURED" if ok else "NOT CONFIGURED")

    def clear_exchange(self):
        broker = self.broker.currentText(); account = self.account.text().strip() or "default"
        self.runtime.secrets.delete_exchange(broker, account)
        QMessageBox.information(self, "Exchange", "Cleared"); self.refresh_setup_status()

    def save_telegram(self):
        tok = self.tg_token.text().strip(); chat = self.tg_chat.text().strip()
        if not tok or not chat:
            QMessageBox.warning(self, "Telegram", "Token and chat id required"); return
        self.runtime.secrets.set_telegram(tok, chat); self.tg_token.clear()
        QMessageBox.information(self, "Telegram", "Stored in SecretStore"); self.refresh_setup_status()

    def test_telegram(self):
        ok = self.runtime.secrets.telegram_configured()
        QMessageBox.information(self, "Telegram", "CONFIGURED" if ok else "NOT CONFIGURED")

    def clear_telegram(self):
        self.runtime.secrets.delete_telegram(); QMessageBox.information(self, "Telegram", "Cleared"); self.refresh_setup_status()

    def detect_mt5(self):
        self._update_mt5_status(); self.refresh_setup_status()

    def test_mt5(self):
        self._update_mt5_status()
        QMessageBox.information(self, "MT5", f"Module: {self.mt5_python_status.text()}\nTerminal: {self.mt5_terminal_status.text()}")

    def _update_mt5_status(self):
        py = "missing"
        try:
            import MetaTrader5  # noqa: F401
            py = "available"
        except Exception:
            pass
        term = "not found"
        try:
            from god.mt5_runtime.detect import detect_mt5
            r = detect_mt5(); term = "found" if getattr(r, "found", False) else "not found"
        except Exception as exc:
            term = f"error:{type(exc).__name__}"
        self.mt5_python_status.setText(py); self.mt5_terminal_status.setText(term)
        if hasattr(self, "mt5_python_status2"):
            self.mt5_python_status2.setText(f"Python module: {py}"); self.mt5_terminal_status2.setText(f"Terminal: {term}")

    def refresh_setup_status(self):
        state = evaluate_setup_state(self.runtime)
        self.setup_overall.setText(f"Overall: {state.overall.value} | needs_setup={state.needs_setup()}")
        mapping = {
            "telegram": state.telegram.value,
            "ai": state.ai.value,
            "crypto": state.crypto.value,
            "mt5": state.mt5.value,
            "google": state.google.value,
            "compute": state.compute.value,
        }
        for k, v in mapping.items():
            if k in self.status_labels:
                self.status_labels[k].setText(v)
        try:
            dev = self.runtime.device_status()
            self.status_labels["license"].setText("ACTIVE" if dev.get("allowed", True) else str(dev.get("status", "BLOCKED")))
            self.status_labels["device"].setText(str(dev.get("status", "—")))
        except Exception:
            self.status_labels["device"].setText("—")
        self._update_mt5_status()

    def test_all_safe(self):
        self.refresh_setup_status()
        QMessageBox.information(self, "Test All", "Safe diagnostic complete.\n- Secrets presence checked\n- MT5 module/terminal distinguished\n- No live orders, no withdrawals, no risk-ceiling changes\nSee status panel for results.")

    def setup_totp(self):
        secret = generate_totp_secret()
        self.runtime.secrets.set_totp_secret(secret)
        uri = otpauth_uri(secret, "NVRA", "local")
        QMessageBox.information(self, "TOTP", f"Secret stored in keyring.\nURI:\n{uri}")

    def save_idx(self):
        self.config.idx_initial_balance = self.idx_balance.value(); self.config.save()

    def reset_idx(self):
        self.idx_balance.setValue(self.runtime.reset_idx_balance())

    def cashout(self):
        result = self.runtime.cashout_request(self.cash_broker.currentText(), self.cash_amount.value())
        QMessageBox.information(self, "Cashout", json.dumps(result, indent=2))

    def backup_drive(self):
        try:
            result = self.runtime.backup_to_google_drive()
            QMessageBox.information(self, "Google Drive backup", json.dumps(result, indent=2))
        except Exception as exc:
            QMessageBox.warning(self, "Google Drive backup failed", str(exc))
        self.refresh_setup_status()

    def save_settings(self):
        self.config.grace_stop_seconds = self.grace.value(); self.config.save()

    def run_health_check(self):
        snap = self.runtime.snapshot()
        snap["setup"] = evaluate_setup_state(self.runtime).to_dict()
        snap["live_authorized"] = False
        QMessageBox.information(self, "Health", json.dumps(snap, indent=2, default=str))

    def refresh(self):
        s = self.runtime.snapshot()
        for k, v in s.items():
            if k in self.fields: self.fields[k].setText(str(v))
        if hasattr(self, "idx_info"): self.idx_info.setPlainText(json.dumps(self.runtime.portfolio_snapshot(), indent=2))
        if hasattr(self, "crypto_info"):
            self.crypto_info.setPlainText(json.dumps({"accounts": [a.__dict__ for a in self.config.crypto_accounts], "portfolio": self.runtime.portfolio_snapshot()}, indent=2, default=str))
        if hasattr(self, "ml_info"):
            self.ml_info.setPlainText(json.dumps({"engine": s["ml_engine"], "risk": s["risk"], "hardware": s["hardware"], "principle": "ML evidence -> risk gate -> execution"}, indent=2))

    def closeEvent(self, event):
        event.ignore(); self.hide()
        self.tray.showMessage("NVRA Unified", "GUI closed to tray; runtime remains active.", QSystemTrayIcon.Information, 2000)

    def _quit(self):
        self.runtime.request_grace_stop(self.config.grace_stop_seconds)
        QTimer.singleShot((self.config.grace_stop_seconds + 2) * 1000, QApplication.quit)


def run_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    runtime = UnifiedRuntime()
    win = NVRAUnifiedWindow(runtime)
    win.show()
    return app.exec()
