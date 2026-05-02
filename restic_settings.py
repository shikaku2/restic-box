"""Settings dialog UI for restic-box."""

from __future__ import annotations

import dataclasses
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore[attr-defined]

import restic_config as cfg_mod
import restic_runner as runner


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------
class SettingsDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window | None, cfg: cfg_mod.Config) -> None:
        super().__init__(title="restic-box Settings", transient_for=parent)
        self.set_default_size(520, 460)
        self.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("_Save", Gtk.ResponseType.OK)

        self._cfg = cfg
        notebook = Gtk.Notebook()
        self.get_content_area().pack_start(notebook, True, True, 0)

        notebook.append_page(self._connection_tab(), Gtk.Label(label="Connection"))
        notebook.append_page(self._security_tab(), Gtk.Label(label="Security"))
        notebook.append_page(self._directories_tab(), Gtk.Label(label="Directories"))
        notebook.append_page(self._options_tab(), Gtk.Label(label="Options"))
        notebook.append_page(self._retention_tab(), Gtk.Label(label="Retention"))

        self.show_all()

    # --- helpers ---

    @staticmethod
    def _labeled_entry(label: str, value: str, grid: Gtk.Grid, row: int) -> Gtk.Entry:
        grid.attach(Gtk.Label(label=label, xalign=1.0), 0, row, 1, 1)
        entry = Gtk.Entry()
        entry.set_text(value)
        entry.set_hexpand(True)
        grid.attach(entry, 1, row, 1, 1)
        return entry

    @staticmethod
    def _grid() -> Gtk.Grid:
        g = Gtk.Grid()
        g.set_row_spacing(8)
        g.set_column_spacing(12)
        g.set_border_width(16)
        return g

    # --- connection tab ---

    def _connection_tab(self) -> Gtk.Widget:
        g = self._grid()
        cfg = self._cfg

        self._e_host = self._labeled_entry("SSH Host:", cfg.ssh_host, g, 0)
        self._e_port = self._labeled_entry("SSH Port:", str(cfg.ssh_port), g, 1)
        self._e_user = self._labeled_entry("SSH User:", cfg.ssh_user, g, 2)
        self._e_key = self._labeled_entry("SSH Key:", cfg.ssh_key, g, 3)

        btn_key = Gtk.Button(label="Browse…")
        btn_key.connect("clicked", self._browse_key)
        g.attach(btn_key, 2, 3, 1, 1)

        self._e_repo = self._labeled_entry("Repo Path:", cfg.repo_path, g, 4)

        btn_test = Gtk.Button(label="Test Connection")
        btn_test.connect("clicked", self._test_connection)
        g.attach(btn_test, 1, 5, 1, 1)

        self._lbl_test = Gtk.Label(label="")
        g.attach(self._lbl_test, 1, 6, 2, 1)

        return g

    def _browse_key(self, _btn: Gtk.Button) -> None:
        dlg = Gtk.FileChooserDialog(
            title="Select SSH private key",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        dlg.set_current_folder(str(Path.home() / ".ssh"))
        if dlg.run() == Gtk.ResponseType.OK:
            self._e_key.set_text(dlg.get_filename())
        dlg.destroy()

    def _test_connection(self, _btn: Gtk.Button) -> None:
        self._lbl_test.set_text("Testing…")
        threading.Thread(target=self._do_test_connection, daemon=True).start()

    def _do_test_connection(self) -> None:
        cfg = self.collect()
        cmd = [
            "ssh",
            "-p", str(cfg.ssh_port),
            "-i", str(Path(cfg.ssh_key).expanduser()),
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=10",
            f"{cfg.ssh_user}@{cfg.ssh_host}",
            "echo OK",
        ]
        print(f"[test-connection] cmd: {' '.join(cmd)}", flush=True)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            print(f"[test-connection] rc={result.returncode}", flush=True)
            if result.stdout:
                print(f"[test-connection] stdout: {result.stdout.strip()}", flush=True)
            if result.stderr:
                print(f"[test-connection] stderr: {result.stderr.strip()}", flush=True)
            # Hetzner storage boxes run a restricted shell: "echo OK" returns rc=8
            # with "Command not found" — that still means auth succeeded.
            auth_failed = any(s in result.stderr for s in ("Permission denied", "Authentication failed"))
            ok = not auth_failed and (
                (result.returncode == 0 and "OK" in result.stdout)
                or "Command not found" in result.stderr
            )
        except Exception as e:
            print(f"[test-connection] exception: {e}", flush=True)
            ok = False
        GLib.idle_add(self._lbl_test.set_text, "Connection OK" if ok else "Connection FAILED")

    # --- security tab ---

    def _security_tab(self) -> Gtk.Widget:
        g = self._grid()
        cfg = self._cfg

        self._e_pwfile = self._labeled_entry("Password File:", cfg.password_file, g, 0)

        btn_browse_pw = Gtk.Button(label="Browse…")
        btn_browse_pw.connect("clicked", self._browse_pwfile)
        g.attach(btn_browse_pw, 2, 0, 1, 1)

        btn_gen = Gtk.Button(label="Generate random password")
        btn_gen.connect("clicked", self._generate_password)
        g.attach(btn_gen, 1, 1, 1, 1)

        self._lbl_pw_status = Gtk.Label(label=self._pw_status_text())
        g.attach(self._lbl_pw_status, 1, 2, 2, 1)

        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        g.attach(sep, 0, 3, 3, 1)

        note = Gtk.Label()
        note.set_markup(
            "<i>Never use your SSH public key as the restic password.\n"
            "The public key is not secret. Use a randomly generated\n"
            "password stored only in the password file (mode 0600).</i>"
        )
        note.set_xalign(0)
        g.attach(note, 0, 4, 3, 1)

        btn_init = Gtk.Button(label="Initialize Repository (first time only)")
        btn_init.connect("clicked", self._init_repo)
        g.attach(btn_init, 1, 5, 1, 1)

        self._lbl_init = Gtk.Label(label="")
        g.attach(self._lbl_init, 1, 6, 2, 1)

        return g

    def _pw_status_text(self) -> str:
        exists = cfg_mod.password_file_exists(self._cfg)
        return "Password file: EXISTS" if exists else "Password file: NOT FOUND"

    def _browse_pwfile(self, _btn: Gtk.Button) -> None:
        dlg = Gtk.FileChooserDialog(
            title="Select password file",
            parent=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE, Gtk.ResponseType.OK,
        )
        if dlg.run() == Gtk.ResponseType.OK:
            self._e_pwfile.set_text(dlg.get_filename())
        dlg.destroy()

    def _generate_password(self, _btn: Gtk.Button) -> None:
        self._cfg.password_file = self._e_pwfile.get_text().strip()
        pw = cfg_mod.generate_password(self._cfg)
        self._lbl_pw_status.set_text(f"Generated & saved ({len(pw)} chars)")

    def _init_repo(self, _btn: Gtk.Button) -> None:
        self._lbl_init.set_text("Initializing…")
        threading.Thread(target=self._do_init_repo, daemon=True).start()

    def _do_init_repo(self) -> None:
        cfg = self.collect()
        print(f"[init-repo] repo: {runner.repo_url(cfg)}", flush=True)
        print(f"[init-repo] password-file: {cfg_mod.password_file_path(cfg)} exists={cfg_mod.password_file_exists(cfg)}", flush=True)
        def on_line(line: str) -> None:
            print(f"[init-repo] {line}", flush=True)
        rc, _out = runner.init_repo(cfg, on_output=on_line)
        print(f"[init-repo] rc={rc}", flush=True)
        msg = "Init OK" if rc == 0 else f"Init failed (rc={rc})"
        GLib.idle_add(self._lbl_init.set_text, msg)

    # --- directories tab ---

    def _directories_tab(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_border_width(12)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(200)

        self._dir_store = Gtk.ListStore(bool, str, str)  # enabled, path, excludes
        for d in self._cfg.directories:
            self._dir_store.append([d.enabled, d.path, ",".join(d.excludes)])

        tv = Gtk.TreeView(model=self._dir_store)

        toggle_r = Gtk.CellRendererToggle()
        toggle_r.connect("toggled", self._on_dir_toggled)
        col_en = Gtk.TreeViewColumn("On", toggle_r, active=0)
        tv.append_column(col_en)

        text_r = Gtk.CellRendererText()
        text_r.set_property("editable", True)
        text_r.connect("edited", lambda r, path, text: self._dir_store.set_value(
            self._dir_store.get_iter_from_string(path), 1, text))
        tv.append_column(Gtk.TreeViewColumn("Directory", text_r, text=1))

        excl_r = Gtk.CellRendererText()
        excl_r.set_property("editable", True)
        excl_r.set_property("placeholder-text", "comma-separated globs")
        excl_r.connect("edited", lambda r, path, text: self._dir_store.set_value(
            self._dir_store.get_iter_from_string(path), 2, text))
        tv.append_column(Gtk.TreeViewColumn("Excludes", excl_r, text=2))

        scroll.add(tv)
        self._dir_tv = tv
        box.pack_start(scroll, True, True, 0)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_add = Gtk.Button(label="Add Directory…")
        btn_add.connect("clicked", self._add_directory)
        btn_remove = Gtk.Button(label="Remove Selected")
        btn_remove.connect("clicked", self._remove_directory)
        self._btn_excl_file = Gtk.Button(label="Add Exclude File(s)…")
        self._btn_excl_file.set_sensitive(False)
        self._btn_excl_file.connect("clicked", self._add_exclude_files)
        self._btn_excl_folder = Gtk.Button(label="Add Exclude Folder…")
        self._btn_excl_folder.set_sensitive(False)
        self._btn_excl_folder.connect("clicked", self._add_exclude_folder)
        btn_row.pack_start(btn_add, False, False, 0)
        btn_row.pack_start(btn_remove, False, False, 0)
        btn_row.pack_start(self._btn_excl_file, False, False, 0)
        btn_row.pack_start(self._btn_excl_folder, False, False, 0)
        box.pack_start(btn_row, False, False, 0)

        tv.get_selection().connect("changed", self._on_dir_selection_changed)

        return box

    def _on_dir_toggled(self, _renderer: Gtk.CellRendererToggle, path: str) -> None:
        it = self._dir_store.get_iter_from_string(path)
        val = self._dir_store.get_value(it, 0)
        self._dir_store.set_value(it, 0, not val)

    def _add_directory(self, _btn: Gtk.Button) -> None:
        dlg = Gtk.FileChooserDialog(
            title="Select directory to back up",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dlg.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN, Gtk.ResponseType.OK,
        )
        if dlg.run() == Gtk.ResponseType.OK:
            self._dir_store.append([True, dlg.get_filename(), ""])
        dlg.destroy()

    def _remove_directory(self, _btn: Gtk.Button) -> None:
        sel = self._dir_tv.get_selection()
        model, it = sel.get_selected()
        if it:
            model.remove(it)

    def _on_dir_selection_changed(self, sel: Gtk.TreeSelection) -> None:
        _model, it = sel.get_selected()
        sensitive = it is not None
        self._btn_excl_file.set_sensitive(sensitive)
        self._btn_excl_folder.set_sensitive(sensitive)

    def _append_excludes(self, it: Gtk.TreeIter, paths: list[str]) -> None:
        existing = self._dir_store.get_value(it, 2)
        parts = [e.strip() for e in existing.split(",") if e.strip()]
        for p in paths:
            if p not in parts:
                parts.append(p)
        self._dir_store.set_value(it, 2, ",".join(parts))

    def _add_exclude_files(self, _btn: Gtk.Button) -> None:
        _model, it = self._dir_tv.get_selection().get_selected()
        if not it:
            return
        dlg = Gtk.FileChooserDialog(
            title="Select file(s) to exclude",
            parent=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dlg.set_select_multiple(True)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK, Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            self._append_excludes(it, dlg.get_filenames())
        dlg.destroy()

    def _add_exclude_folder(self, _btn: Gtk.Button) -> None:
        _model, it = self._dir_tv.get_selection().get_selected()
        if not it:
            return
        dlg = Gtk.FileChooserDialog(
            title="Select folder to exclude",
            parent=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OK, Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            self._append_excludes(it, [dlg.get_filename()])
        dlg.destroy()

    # --- options tab ---

    def _options_tab(self) -> Gtk.Widget:
        g = self._grid()
        cfg = self._cfg

        g.attach(Gtk.Label(label="Compression:", xalign=1.0), 0, 0, 1, 1)
        self._combo_comp = Gtk.ComboBoxText()
        for opt in ("off", "auto", "max"):
            self._combo_comp.append_text(opt)
        self._combo_comp.set_active(["off", "auto", "max"].index(cfg.compression))
        self._combo_comp.set_hexpand(True)
        g.attach(self._combo_comp, 1, 0, 1, 1)

        self._chk_startup = Gtk.CheckButton(label="Backup on startup")
        self._chk_startup.set_active(cfg.backup_on_startup)
        g.attach(self._chk_startup, 1, 1, 1, 1)

        g.attach(Gtk.Label(label="Auto-backup:", xalign=1.0), 0, 2, 1, 1)
        self._combo_bsched = Gtk.ComboBoxText()
        for opt in ("disabled", "interval", "daily"):
            self._combo_bsched.append_text(opt)
        self._combo_bsched.set_active(["disabled", "interval", "daily"].index(cfg.backup_schedule))
        g.attach(self._combo_bsched, 1, 2, 1, 1)

        self._lbl_binterval = Gtk.Label(label="Interval (h):", xalign=1.0)
        self._lbl_binterval.set_no_show_all(True)
        g.attach(self._lbl_binterval, 0, 3, 1, 1)
        self._spin_binterval = Gtk.SpinButton.new_with_range(1, 168, 1)
        self._spin_binterval.set_value(cfg.backup_interval_hours)
        self._spin_binterval.set_no_show_all(True)
        g.attach(self._spin_binterval, 1, 3, 1, 1)

        self._lbl_bdailyhour = Gtk.Label(label="Run at hour:", xalign=1.0)
        self._lbl_bdailyhour.set_no_show_all(True)
        g.attach(self._lbl_bdailyhour, 0, 4, 1, 1)
        self._spin_bdailyhour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self._spin_bdailyhour.set_value(cfg.backup_daily_hour)
        self._spin_bdailyhour.set_no_show_all(True)
        g.attach(self._spin_bdailyhour, 1, 4, 1, 1)

        def _on_bsched_changed(_combo: Gtk.ComboBoxText) -> None:
            sel = self._combo_bsched.get_active_text()
            self._lbl_binterval.set_visible(sel == "interval")
            self._spin_binterval.set_visible(sel == "interval")
            self._lbl_bdailyhour.set_visible(sel == "daily")
            self._spin_bdailyhour.set_visible(sel == "daily")
        self._combo_bsched.connect("changed", _on_bsched_changed)
        _on_bsched_changed(self._combo_bsched)

        g.attach(Gtk.Label(label="Check interval:", xalign=1.0), 0, 5, 1, 1)
        self._combo_interval = Gtk.ComboBoxText()
        for opt in ("daily", "weekly"):
            self._combo_interval.append_text(opt)
        self._combo_interval.set_active(0 if cfg.check_interval == "daily" else 1)
        g.attach(self._combo_interval, 1, 5, 1, 1)

        _days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        self._lbl_day = Gtk.Label(label="Check day:", xalign=1.0)
        self._lbl_day.set_no_show_all(True)
        g.attach(self._lbl_day, 0, 6, 1, 1)
        self._combo_day = Gtk.ComboBoxText()
        for d in _days:
            self._combo_day.append_text(d)
        self._combo_day.set_active(cfg.check_day)
        self._combo_day.set_no_show_all(True)
        g.attach(self._combo_day, 1, 6, 1, 1)

        g.attach(Gtk.Label(label="Check hour:", xalign=1.0), 0, 7, 1, 1)
        self._spin_hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self._spin_hour.set_value(cfg.check_hour)
        g.attach(self._spin_hour, 1, 7, 1, 1)

        def _on_interval_changed(_combo: Gtk.ComboBoxText) -> None:
            weekly = self._combo_interval.get_active_text() == "weekly"
            self._lbl_day.set_visible(weekly)
            self._combo_day.set_visible(weekly)
        self._combo_interval.connect("changed", _on_interval_changed)
        _on_interval_changed(self._combo_interval)

        self._chk_subset = Gtk.CheckButton(label="Read data subset during check")
        self._chk_subset.set_active(cfg.check_read_data_subset)
        g.attach(self._chk_subset, 1, 8, 2, 1)

        self._lbl_subset = Gtk.Label(label="Subsets total:", xalign=1.0)
        g.attach(self._lbl_subset, 0, 9, 1, 1)
        self._spin_subset = Gtk.SpinButton.new_with_range(10, 1000, 10)
        self._spin_subset.set_value(cfg.check_subset_total)
        g.attach(self._spin_subset, 1, 9, 1, 1)

        self._lbl_subset_note = Gtk.Label(xalign=0.0)
        self._lbl_subset_note.set_markup(
            f"<i>Verifies 1/N of stored data per check — cycles through everything over N checks.\n"
            f"Next check: subset {cfg.check_subset_current}/{cfg.check_subset_total}.</i>"
        )
        g.attach(self._lbl_subset_note, 0, 10, 3, 1)

        def _on_subset_toggled(_chk: Gtk.CheckButton) -> None:
            on = self._chk_subset.get_active()
            self._lbl_subset.set_sensitive(on)
            self._spin_subset.set_sensitive(on)
            self._lbl_subset_note.set_sensitive(on)
        self._chk_subset.connect("toggled", _on_subset_toggled)
        _on_subset_toggled(self._chk_subset)

        return g

    # --- retention tab ---

    def _retention_tab(self) -> Gtk.Widget:
        g = self._grid()
        cfg = self._cfg

        self._chk_retention = Gtk.CheckButton(label="Enable automatic retention (forget + prune after each backup)")
        self._chk_retention.set_active(cfg.retention_enabled)
        g.attach(self._chk_retention, 0, 0, 3, 1)

        note = Gtk.Label()
        note.set_markup(
            "<i>Keeps snapshots within the specified age of the newest snapshot.\n"
            "All older snapshots are forgotten and their data pruned.\n"
            "Set only the components you need — zero values are omitted.</i>"
        )
        note.set_xalign(0)
        g.attach(note, 0, 1, 3, 1)

        fields = [("Years:", "retention_years"), ("Months:", "retention_months"),
                  ("Days:", "retention_days"),  ("Hours:", "retention_hours")]
        self._retention_spins: dict[str, Gtk.SpinButton] = {}
        for row, (label, attr) in enumerate(fields, start=2):
            lbl = Gtk.Label(label=label, xalign=1.0)
            g.attach(lbl, 0, row, 1, 1)
            spin = Gtk.SpinButton.new_with_range(0, 999, 1)
            spin.set_value(getattr(cfg, attr))
            g.attach(spin, 1, row, 1, 1)
            self._retention_spins[attr] = spin

        self._lbl_retention_preview = Gtk.Label(xalign=0.0)
        g.attach(self._lbl_retention_preview, 0, 6, 3, 1)

        def _update_preview(*_) -> None:
            parts = [(int(self._retention_spins[a].get_value()), u)
                     for a, u in [("retention_years","y"),("retention_months","m"),
                                   ("retention_days","d"),("retention_hours","h")]]
            dur = "".join(f"{v}{u}" for v, u in parts if v > 0)
            if dur:
                self._lbl_retention_preview.set_markup(f"<i>--keep-within {dur}</i>")
            else:
                self._lbl_retention_preview.set_markup("<i>No duration set — retention will do nothing.</i>")

        for spin in self._retention_spins.values():
            spin.connect("value-changed", _update_preview)
        _update_preview()

        def _on_retention_toggled(_chk: Gtk.CheckButton) -> None:
            on = self._chk_retention.get_active()
            for spin in self._retention_spins.values():
                spin.set_sensitive(on)
            self._lbl_retention_preview.set_sensitive(on)
        self._chk_retention.connect("toggled", _on_retention_toggled)
        _on_retention_toggled(self._chk_retention)

        return g

    # --- collect ---

    def collect(self) -> cfg_mod.Config:
        try:
            port = int(self._e_port.get_text().strip())
        except ValueError:
            port = self._cfg.ssh_port

        dirs = [
            cfg_mod.Directory(
                path=row[1],
                excludes=[e.strip() for e in row[2].split(",") if e.strip()],
                enabled=row[0],
            )
            for row in self._dir_store
        ]

        return dataclasses.replace(
            self._cfg,
            ssh_host=self._e_host.get_text().strip(),
            ssh_port=port,
            ssh_user=self._e_user.get_text().strip(),
            ssh_key=self._e_key.get_text().strip(),
            repo_path=self._e_repo.get_text().strip(),
            password_file=self._e_pwfile.get_text().strip(),
            compression=self._combo_comp.get_active_text() or "max",
            backup_on_startup=self._chk_startup.get_active(),
            backup_schedule=self._combo_bsched.get_active_text() or "disabled",
            backup_interval_hours=int(self._spin_binterval.get_value()),
            backup_daily_hour=int(self._spin_bdailyhour.get_value()),
            check_interval=self._combo_interval.get_active_text() or "daily",
            check_day=self._combo_day.get_active(),
            check_hour=int(self._spin_hour.get_value()),
            check_read_data_subset=self._chk_subset.get_active(),
            check_subset_total=int(self._spin_subset.get_value()),
            retention_enabled=self._chk_retention.get_active(),
            retention_years=int(self._retention_spins["retention_years"].get_value()),
            retention_months=int(self._retention_spins["retention_months"].get_value()),
            retention_days=int(self._retention_spins["retention_days"].get_value()),
            retention_hours=int(self._retention_spins["retention_hours"].get_value()),
            directories=dirs,
        )


