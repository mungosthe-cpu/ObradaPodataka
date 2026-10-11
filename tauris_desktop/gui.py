from __future__ import annotations

import os
import re
import time

from tauris_desktop.lib import *
import tauris_desktop.lib as _tdl

# ``import *`` ne uvozi privatna imena iz ``lib``; GUI ih koristi u nekim mestima.
_cleanup_obsolete_taurislock_local_secrets = _tdl._cleanup_obsolete_taurislock_local_secrets
_is_network_unreachable_error = _tdl._is_network_unreachable_error
_offline_send_instructions_for_data_folder = _tdl._offline_send_instructions_for_data_folder
_clean_takmicar_display = _tdl._clean_takmicar_display
_show_block_message = _tdl._show_block_message


class TaurisApp:
    def __init__(self, delivery_config: DeliveryConfig) -> None:
        self.delivery_config = delivery_config
        self._pending_retry_running = False
        self.connected = False
        self.busy = False
        self.worker_queue: Queue[tuple[str, Any]] = Queue()
        self.root = tk.Tk()
        self.root.title(f"Tauris Povezivanje ver {APP_VERSION}")
        self.window_width = 520
        self.window_height = 640
        self._set_window_icon()
        self._center_window()
        self.root.resizable(False, False)

        self.device_var = tk.StringVar(value=CLOCK_DEVICE_LABELS[0])
        self.port_var = tk.StringVar(value="")
        self.baud_var = tk.StringVar(value="9600")
        self.grupacija_var = tk.StringVar(value=GRUPACIJA_LABELS[0])
        self.broj_trke_var = tk.StringVar(value=BROJ_TRKE_LABELS[0])
        self.mesto_var = tk.StringVar(value="")
        self.pilot_var = tk.StringVar(value=UNKNOWN_PILOT_LABEL)
        self.slow_tauris_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(
            value="Status: izaberite tip sata i COM port — zatim «Preuzmi podatke»."
        )
        self.last_locked_bundle_path: Path | None = None
        self._last_seen_ports: set[str] = set()
        self._upgrade_in_progress = False
        self._port_devices: list[str] = []
        self._port_label_by_device: dict[str, str] = {}
        self._bricon_workflow_hint_shown = False
        self._cancel_download = threading.Event()

        self._build_ui()
        self._load_gui_settings_into_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<F5>", self._on_f5_refresh_ports)
        self.root.bind("<FocusIn>", self._on_app_focus_refresh_ports)
        self.root.after(200, self._poll_worker_queue)
        self.root.after(150, self._startup_port_scan_and_connect)
        self.root.after(2000, self._port_poll_tick)
        self.root.after(5000, lambda: self.check_for_updates(silent_if_current=True, auto_install=True))
        self.root.after(9000, lambda: self._retry_pending_sends_background(silent=True))
        if serial is None:
            self.status_var.set(
                "Status: nedostaje pyserial — COM neće raditi. Instalacija: pip install pyserial"
            )

    def _on_f5_refresh_ports(self, _event: object | None = None) -> str:
        self._sync_ports_from_system(trigger_auto_prepare=True)
        return "break"

    def _on_app_focus_refresh_ports(self, _event: object | None = None) -> None:
        """Kad se vrati fokus na prozor, osveži listu — ukloni port koji više nije priključen."""
        if self.busy:
            return
        self._sync_ports_from_system(trigger_auto_prepare=True)

    def _startup_port_scan_and_connect(self) -> None:
        """Pri pokretanju: skeniraj COM i izaberi port (bez dugog čitanja sa uređaja)."""
        if serial is None:
            return
        self._last_seen_ports = set(self._list_available_ports())
        self.refresh_ports()
        self._try_auto_prepare(reason="startup")

    def _port_poll_tick(self) -> None:
        """Povremeno proveri da li je sat priključen / isključen (novi ili nestali COM port)."""
        try:
            if serial is not None and not self.busy:
                ports_now = set(self._list_available_ports())
                added = ports_now - self._last_seen_ports
                removed = self._last_seen_ports - ports_now
                if ports_now != self._last_seen_ports:
                    cur = (self.port_var.get() or "").strip()
                    if removed and self.connected and (not ports_now or cur in removed):
                        self.connected = False
                        self.download_btn.config(state="disabled")
                        self._update_download_btn_appearance()
                    self._last_seen_ports = ports_now
                    self.refresh_ports()
                    if added and not self.connected:
                        self._try_auto_prepare(reason="plug")
                    elif removed and not ports_now:
                        self.status_var.set(
                            "Status: TAURIS isključen — nema COM portova. Priključite sat (F5)."
                        )
        finally:
            self.root.after(2000, self._port_poll_tick)

    def _sync_ports_from_system(self, *, trigger_auto_prepare: bool = False) -> None:
        ports_now = set(self._list_available_ports())
        self._last_seen_ports = ports_now
        self.refresh_ports()
        if trigger_auto_prepare and not self.connected:
            self._try_auto_prepare(reason="focus")

    def _apply_port_ready_state(
        self, port: str, *, reason: str = "", update_status: bool = True
    ) -> None:
        """Port izabran i vidljiv sistemu — omogući preuzimanje (bez posebnog koraka „Poveži se“)."""
        if self._get_device_kind() == CLOCK_DEVICE_BRICON:
            port = self._resolve_com_device(port)
            ports = list(self._port_devices)
            reliable = self._reliable_com_ports(ports)
            self.connected = bool(port and port in reliable) or bool(reliable)
            if not self.busy:
                self.download_btn.config(state="normal" if self.connected else "disabled")
            self._update_download_btn_appearance()
            if update_status:
                if port and port in reliable:
                    self.status_var.set(
                        f"Status: BRICON port {port} — sat priključen? Kliknite «Preuzmi BRICON» (program sam čita sat)."
                    )
                elif reliable:
                    self.status_var.set(
                        "Status: BRICON — izaberite USB port (F5), pa «Preuzmi BRICON»."
                    )
                else:
                    self.status_var.set(self._bricon_port_status_hint())
            return
        port = self._resolve_com_device(port)
        ports = list(self._port_devices)
        if not port or port not in ports:
            self.connected = False
            if not self.busy:
                self.download_btn.config(state="disabled")
            self._update_download_btn_appearance()
            return
        self.connected = True
        if not self.busy:
            self.download_btn.config(state="normal")
        self._update_download_btn_appearance()
        if reason == "plug":
            play_tauris_connected_sound()
        if update_status:
            self.status_var.set(
                f"Status: spreman ({port}). Kliknite «Preuzmi podatke» i pokrenite slanje na {self._device_display_name()}-u."
            )

    def _on_port_selection_changed(self, *_args: object) -> None:
        if self.busy:
            return
        dev = self._resolve_com_device(self.port_var.get())
        if dev and dev not in self._port_devices:
            self.connected = False
            if not self.busy:
                self.download_btn.config(state="disabled")
            self._update_download_btn_appearance()
            if self._get_device_kind() == CLOCK_DEVICE_BRICON:
                self.status_var.set(
                    f"Status: {dev} nije u listi — F5 ili «Uvezi BRICON fajl»."
                )
            else:
                self.status_var.set(
                    f"Status: {dev} nije priključen — pritisnite F5 i izaberite port iz liste."
                )
            return
        self._apply_port_ready_state(dev)

    def _list_port_entries(self) -> list[tuple[str, str]]:
        """(COM ime, prikaz u padajućem meniju sa opisom uređaja)."""
        if list_ports is None:
            return []
        out: list[tuple[str, str]] = []
        for info in list_ports.comports():
            dev = (info.device or "").strip()
            if not dev:
                continue
            desc = (info.description or "").strip()
            manu = (info.manufacturer or "").strip()
            hint = desc or manu
            blob = " ".join(str(x or "") for x in (desc, manu, info.product, info.interface)).lower()
            suffix = ""
            if "phased out" in blob or ("pl2303" in blob and "please" in blob):
                suffix = " — ne koristiti"
            if hint and hint.lower() not in dev.lower():
                label = f"{dev} — {hint[:38]}{suffix}"
            else:
                label = f"{dev}{suffix}"
            out.append((dev, label))
        return out

    def _reliable_com_ports(self, ports: list[str] | None = None) -> list[str]:
        pool = list(ports if ports is not None else self._port_devices)
        good = [p for p in pool if com_port_usability_issue(p) is None]
        return good if good else pool

    def _resolve_com_device(self, port_text: str) -> str:
        """Iz teksta u polju Port izvuci COM3, COM4, …"""
        t = (port_text or "").strip()
        if not t:
            return ""
        for dev, label in self._port_label_by_device.items():
            if t == dev or t == label:
                return dev
        m = re.match(r"^(COM\d+)", t, re.IGNORECASE)
        if m:
            return m.group(1).upper()
        return t

    def _label_for_com_device(self, device: str) -> str:
        device = (device or "").strip()
        return self._port_label_by_device.get(device, device)

    def _bricon_port_status_hint(self) -> str:
        return (
            "Status: nema BRICON USB porta — USB kabl, F5. "
            "Drajver je u instalacionom paketu (ponovo instalirajte program ako treba). "
            "Ili «Uvezi BRICON fajl»."
        )

    def _bricon_no_port_dialog_text(self) -> str:
        from tauris_desktop.config import BRICON_USB_DRIVER_BUNDLED, BRICON_USB_DRIVER_URL

        driver_note = (
            "BRICON USB drajver se instalira zajedno sa programom (pri prvoj instalaciji).\n"
            if BRICON_USB_DRIVER_BUNDLED
            else f"Instalirajte BRICON USB drajver (CDM_Setup) sa:\n   {BRICON_USB_DRIVER_URL}\n"
        )
        return (
            "Windows ne vidi BRICON USB serijski port.\n\n"
            + driver_note
            + "1) Priključite sat USB kablom, pritisnite F5\n"
            "2) «Preuzmi BRICON» — program sam pita sat (Print Manager protokol), ništa ne pritiskate na satu\n\n"
            "Ako i dalje nema porta — ponovo pokrenite instalaciju (drajver) ili «Uvezi BRICON fajl»."
        )

    def _bricon_workflow_text(self) -> str:
        return (
            "BRICON preko USB (kao Print Manager «Uitlezen»):\n\n"
            "• Zatvorite Bricon Print Manager dok koristite ovaj program "
            "(isti COM port ne može oba).\n"
            "• USB kabl u računar pre pokretanja; napajanje sata/antena po uputstvu.\n"
            "• Na satu «traži štampač» = pogrešan režim (štampač/klub, ne USB readout).\n\n"
            "U ovom programu (isti binarni protokol kao Print Manager, 9600 baud 8N2):\n"
            "1) FTDI drajver (instalacioni paket)\n"
            "2) F5 — izaberite BRICON port\n"
            "3) «Preuzmi BRICON» — program pronalazi sat i sam čita takmičara, golubove i vremena\n"
            "4) Na satu ne treba ništa da se pritiska; sačekajte da status prikaže broj golubova\n"
            "5) «Pošalji podatke na obradu»\n\n"
            "Rezerva: u PM učitajte listu → «Uvezi BRICON fajl»."
        )

    def _show_bricon_workflow_hint_once(self) -> None:
        if self._bricon_workflow_hint_shown:
            return
        self._bricon_workflow_hint_shown = True
        messagebox.showinfo("BRICON — kako preuzeti podatke", self._bricon_workflow_text(), parent=self.root)

    def start_bricon_acquisition(self) -> None:
        """Glavni BRICON korak: USB preuzimanje (zamena za PM) ili uvoz fajla."""
        if self.busy:
            return
        if serial is None:
            messagebox.showerror(
                "Greška",
                "Nedostaje pyserial paket. Instalirajte: pip install pyserial",
                parent=self.root,
            )
            return

        self.refresh_ports()
        from tauris_desktop.bricon import com_port_looks_like_bricon_usb

        reliable = self._reliable_com_ports()
        bricon_ports = [p for p in reliable if com_port_looks_like_bricon_usb(p)]
        pick = self._resolve_com_device(self.port_var.get())
        if pick and pick not in reliable:
            pick = ""
        if not pick and bricon_ports:
            pick = bricon_ports[0]
        elif not pick and len(reliable) == 1:
            pick = reliable[0]
        elif not pick and reliable:
            pick = reliable[0]

        if pick:
            self.port_var.set(self._label_for_com_device(pick))
            self._sync_saved_port_in_settings(pick)
            self._apply_port_ready_state(pick, update_status=False)

        if not reliable:
            if messagebox.askyesno(
                "BRICON — nema USB porta",
                self._bricon_no_port_dialog_text() + "\n\nOtvoriti uvoz fajla?",
                parent=self.root,
            ):
                self.import_bricon_file_clicked()
            return

        if not pick:
            messagebox.showwarning(
                "BRICON — izaberite port",
                "Izaberite BRICON USB port u listi (F5 osveži), pa ponovo «Preuzmi BRICON».",
                parent=self.root,
            )
            return

        choice = messagebox.askyesnocancel(
            "BRICON preuzimanje",
            self._bricon_workflow_text()
            + "\n\n"
            + (f"Izabrani port: {pick}\n\n" if pick else "")
            + "Da = USB preuzimanje sada (program drži COM)\n"
            "Ne = uvezite sačuvani fajl\n"
            "Otkaži",
            parent=self.root,
        )
        if choice is None:
            return
        if choice:
            self.download_data()
        else:
            self.import_bricon_file_clicked()

    # Uređaj-specifični ključevi za opis COM porta u Windows-u. Generični adapter
    # ključevi (usb-serial, ch340, cp210, ftdi, prolific, silicon labs, cdc, uart...)
    # se dele jer i TAURIS i BRICON često izlaze preko istih jeftinih USB-serial
    # čipova — ali marketing/uredjaj-specifični nazivi NE smeju da se mešaju, inače
    # se auto-izbor porta za TAURIS može zalepiti za priključen BRICON sat i obrnuto.
    _GENERIC_USB_SERIAL_KEYS = (
        "usb-serial",
        "usb serial",
        "ch340",
        "cp210",
        "ftdi",
        "prolific",
        "silicon labs",
        "cdc",
        "composite",
        "uart",
        "serial port",
        "ch343",
        "ch910",
        "wch",
    )
    _TAURIS_ONLY_KEYS = ("tauris", "tau ris")
    _BRICON_ONLY_KEYS = ("bricon", "x-treme", "xtreme", "speedy")

    def _tauris_like_com_ports(self, ports: list[str]) -> list[str]:
        """Portovi čiji opis liči na TAURIS / generički USB-serial (BEZ bricon-specifičnih naziva)."""
        return self._filter_com_ports_by_keys(
            ports, self._TAURIS_ONLY_KEYS + self._GENERIC_USB_SERIAL_KEYS, exclude=self._BRICON_ONLY_KEYS
        )

    def _bricon_like_com_ports(self, ports: list[str]) -> list[str]:
        """Portovi čiji opis liči na BRICON / generički USB-serial (BEZ tauris naziva)."""
        return self._filter_com_ports_by_keys(
            ports, self._BRICON_ONLY_KEYS + self._GENERIC_USB_SERIAL_KEYS, exclude=self._TAURIS_ONLY_KEYS
        )

    def _bricon_auto_port_candidates(self, selected: str) -> list[str]:
        """
        „COM: Auto“ kao u Print Manageru: izabrani port prvi, zatim portovi koji liče na
        BRICON/USB-serial, pa ostali pouzdani. Portovi koji liče na TAURIS se izostavljaju
        (ne šaljemo BRICON komande drugom satu).
        """
        reliable = self._reliable_com_ports()
        liked = self._bricon_like_com_ports(reliable)
        tauris_only = set(self._filter_com_ports_by_keys(reliable, self._TAURIS_ONLY_KEYS))
        out: list[str] = []
        for p in ([selected] if selected else []) + liked + reliable:
            if p and p not in out and p not in tauris_only:
                out.append(p)
        return out

    def _device_like_com_ports(self, ports: list[str]) -> list[str]:
        """Portovi koji liče na TRENUTNO IZABRANI tip uređaja (TAURIS ili BRICON)."""
        if self._get_device_kind() == CLOCK_DEVICE_BRICON:
            return self._bricon_like_com_ports(ports)
        return self._tauris_like_com_ports(ports)

    def _filter_com_ports_by_keys(
        self, ports: list[str], keys: tuple[str, ...], *, exclude: tuple[str, ...] = ()
    ) -> list[str]:
        if list_ports is None:
            return []
        want = {p.strip() for p in ports if p.strip()}
        hits: list[str] = []
        for info in list_ports.comports():
            dev = (info.device or "").strip()
            if dev not in want:
                continue
            blob = " ".join(
                str(x or "")
                for x in (
                    info.description,
                    info.manufacturer,
                    info.product,
                    info.interface,
                )
            ).lower()
            if any(k in blob for k in exclude):
                continue
            if any(k in blob for k in keys):
                hits.append(dev)
        return hits

    def _pick_preferred_com_port(self, ports: list[str]) -> str:
        if not ports:
            return ""
        reliable = self._reliable_com_ports(ports)
        saved = self._resolve_com_device(load_gui_settings_dict().get("port") or "")
        if saved in reliable:
            return saved
        if saved in ports and com_port_usability_issue(saved):
            pass  # ne biraj automatski neispravan sačuvani port
        elif saved in ports:
            return saved
        if len(reliable) == 1:
            return reliable[0]
        if len(ports) == 1:
            return ports[0]
        liked = self._device_like_com_ports(reliable)
        if len(liked) == 1:
            return liked[0]
        if liked:
            return liked[0]
        if reliable:
            return reliable[0]
        return ports[0]

    def _should_auto_connect(self, ports: list[str]) -> bool:
        if not ports:
            return False
        if len(ports) == 1:
            return True
        saved = (load_gui_settings_dict().get("port") or "").strip()
        if saved in ports:
            return True
        return len(self._device_like_com_ports(ports)) == 1

    def _try_auto_prepare(self, *, reason: str = "startup") -> None:
        if serial is None or self.busy:
            return
        ports = self._list_available_ports()
        if not self._should_auto_connect(ports):
            if reason == "startup" and ports:
                self.status_var.set(
                    f"Status: više COM portova — izaberite {self._device_display_name()} port, pa «Preuzmi podatke»."
                )
            return
        pick = self._pick_preferred_com_port(ports)
        if not pick:
            return
        self.port_var.set(pick)
        self._sync_saved_port_in_settings(pick)
        self._apply_port_ready_state(pick, reason=reason)

    def _sync_saved_port_in_settings(self, port: str) -> None:
        port = self._resolve_com_device(port)
        d = dict(load_gui_settings_dict())
        if d.get("port") == port:
            return
        d["port"] = port
        save_gui_settings_dict(d)

    def _load_gui_settings_into_ui(self) -> None:
        d = load_gui_settings_dict()
        if not d:
            return
        baud = str(d.get("baud") or "").strip()
        if baud in BAUD_CHOICES:
            self.baud_var.set(baud)
        g = str(d.get("grupacija") or "").strip()
        if g in GRUPACIJA_LABELS:
            self.grupacija_var.set(g)
        b = str(d.get("broj_trke") or "").strip()
        if b in BROJ_TRKE_LABELS:
            self.broj_trke_var.set(b)
        self.mesto_var.set(str(d.get("mesto_pustanja") or ""))
        pilot = str(d.get("pilot") or "").strip()
        if pilot:
            self.pilot_var.set(pilot)
        # Port postavlja refresh_ports() — ovde samo ne učitavamo zastareli puni opis u polje.
        slow = d.get("slow_uart_legacy")
        if isinstance(slow, bool):
            self.slow_tauris_var.set(slow)
        elif str(slow).strip().lower() in ("1", "true", "da", "yes"):
            self.slow_tauris_var.set(True)
        dev = str(d.get("device_type") or d.get("clock_device") or "").strip().upper()
        if dev == "BRICON":
            self.device_var.set("BRICON")
        elif dev == "TAURIS":
            self.device_var.set("TAURIS")

    def _persist_gui_settings(self) -> None:
        # Spoji sa postojećim — ne briši ključeve koje GUI ne prikazuje (npr. bricon_import_dir).
        merged = dict(load_gui_settings_dict())
        merged.update(
            {
                "device_type": (self.device_var.get() or "TAURIS").strip(),
                "port": self._resolve_com_device(self.port_var.get()),
                "baud": (self.baud_var.get() or "").strip(),
                "grupacija": (self.grupacija_var.get() or "").strip(),
                "broj_trke": (self.broj_trke_var.get() or "").strip(),
                "mesto_pustanja": (self.mesto_var.get() or "").strip(),
                "pilot": (self.pilot_var.get() or "").strip(),
                "slow_uart_legacy": bool(self.slow_tauris_var.get()),
            }
        )
        save_gui_settings_dict(merged)

    def _get_device_kind(self) -> str:
        label = (self.device_var.get() or "TAURIS").strip().upper()
        return CLOCK_DEVICE_BRICON if label == "BRICON" else CLOCK_DEVICE_TAURIS

    def _device_display_name(self) -> str:
        return "BRICON" if self._get_device_kind() == CLOCK_DEVICE_BRICON else "TAURIS"

    def _on_device_type_changed(self, *_args: object) -> None:
        self._sync_device_dependent_ui()
        self._persist_gui_settings()
        self._layout_main_window_background()

    def _sync_device_dependent_ui(self) -> None:
        is_bricon = self._get_device_kind() == CLOCK_DEVICE_BRICON
        try:
            if is_bricon:
                self.port_combo.configure(state="readonly", width=28)
                self.download_btn.config(
                    text="1)  PREUZMI BRICON",
                    command=self.start_bricon_acquisition,
                )
                self._show_bricon_workflow_hint_once()
            else:
                self._bg_canvas.itemconfigure(self._cw_bricon_import, state="hidden")
                self.port_combo.configure(state="readonly", width=18)
                self.download_btn.config(
                    text="1)  PREUZMI PODATKE",
                    command=self.download_data,
                )
        except tk.TclError:
            pass
        self.refresh_ports()

    def _on_close(self) -> None:
        if self.busy:
            proceed = messagebox.askyesno(
                "Preuzimanje u toku",
                "BRICON/TAURIS preuzimanje ili slanje je još u toku.\n\n"
                "Zatvaranje sada može prekinuti prenos usred posla "
                "(parcijalni podaci se čuvaju za dijagnostiku, ali trka neće biti obrađena).\n\n"
                "Zatvoriti ipak?",
                parent=self.root,
                icon="warning",
                default="no",
            )
            if not proceed:
                return
            logging.warning("Zatvaranje prozora dok je preuzimanje/slanje u toku — otkazujem pozadinski posao.")
            self._cancel_download.set()
        try:
            self._persist_gui_settings()
        finally:
            try:
                self.root.destroy()
            except Exception:
                pass

    def _set_window_icon(self) -> None:
        icon_path = get_resource_path("ICON/PIGEONp.png")
        if not icon_path.exists():
            icon_path = get_resource_path("ICON/pigeon.png")
        if not icon_path.exists():
            logging.warning("Ikonica nije pronadjena: %s", icon_path)
            return
        try:
            self.window_icon_image = tk.PhotoImage(file=str(icon_path))
            self.root.iconphoto(True, self.window_icon_image)
        except Exception:
            logging.exception("Neuspesno postavljanje ikonice prozora.")

    def _center_window(self) -> None:
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - self.window_width) // 2
        y = (screen_height - self.window_height) // 2
        self.root.geometry(f"{self.window_width}x{self.window_height}+{x}+{y}")

    def _sync_status_canvas_text(self, *_args: object) -> None:
        sid = getattr(self, "_main_status_text_id", None)
        if sid is None:
            return
        try:
            self._bg_canvas.itemconfigure(sid, text=self.status_var.get())
        except tk.TclError:
            pass

    def _toggle_slow_check_via_canvas_label(self, _event: object | None = None) -> None:
        self.slow_tauris_var.set(not self.slow_tauris_var.get())
        self._persist_gui_settings()

    def _layout_main_window_background(self, _event: object | None = None) -> None:
        """Fotografija preko celog canvasa; kontrole u create_window, tekst u create_text (bez neprozirnog panela)."""
        c = self._bg_canvas
        c.update_idletasks()
        w = max(c.winfo_width(), 1)
        h = max(c.winfo_height(), 1)
        if w < 48 or h < 48:
            return

        need_new_bg = getattr(self, "_main_last_client_wh", None) != (w, h)
        self._main_last_client_wh = (w, h)

        if self._main_bg_pil is None:
            c.delete("bg_img")
        elif need_new_bg:
            try:
                from PIL import Image, ImageEnhance, ImageTk  # type: ignore

                resized = self._main_bg_pil.resize((w, h), Image.Resampling.LANCZOS)
                resized = ImageEnhance.Brightness(resized).enhance(1.14)
                self._main_bg_photo = ImageTk.PhotoImage(resized)
                c.delete("bg_img")
                c.create_image(0, 0, anchor="nw", image=self._main_bg_photo, tags="bg_img")
                c.tag_lower("bg_img")
            except Exception:
                logging.exception("Pozadina glavnog prozora.")
                c.delete("bg_img")

        self._place_main_ui_over_canvas(w, h)

    def _place_main_ui_over_canvas(self, w: int, h: int) -> None:
        c = self._bg_canvas
        c.delete("ui_txt")
        cx = w // 2
        x_lr = cx - 98
        x_c = cx - 90
        font_lbl = ("Segoe UI", 9)
        font_hi = ("Segoe UI", 9)
        fill_txt = "#0f172a"
        fill_muted = "#1e293b"
        fill_foot = "#334155"
        dev = self._device_display_name()
        title = f"{dev} — preuzimanje rezultata"
        if self._get_device_kind() == CLOCK_DEVICE_BRICON:
            slow_txt = (
                "BRICON USB: program sam čita sat (Print Manager protokol) — na satu ništa ne pritiskate"
            )
            help_txt = (
                "1) USB (drajver u paketu), F5 port. 2) «Preuzmi BRICON» — sačekajte da program pročita sat. "
                "3) «Snimi COM» ako ne radi (log za analizu). 4) «Uvezi fajl». 5) Pošalji na obradu."
            )
        else:
            slow_txt = (
                "Stariji / sporiji TAURIS — duže čitanje COM-a (prekid tek kad zaista stane slanje)"
            )
            help_txt = (
                "1) Preuzmi podatke — na TAURIS-u pokrenite slanje u tom trenutku. "
                "2) Pošalji podatke na obradu — prilozi bez ZIP arhive. "
                "F5 / Osveži osvežava COM portove."
            )
        if not tauris_is_lite_edition():
            help_txt += " Otvaranje paketa: meni Baza."

        row_gap = 8
        y = 12
        c.create_text(cx + 1, y + 1, text=title, anchor="n", font=("Segoe UI", 12, "bold"), fill="#f1f5f9", tags="ui_txt")
        c.create_text(cx, y, text=title, anchor="n", font=("Segoe UI", 12, "bold"), fill=fill_txt, tags="ui_txt")
        y += 34

        c.coords(self._cw_device, x_c, y)
        self.root.update_idletasks()
        rh_dev = max(self.device_combo.winfo_reqheight(), 22)
        c.create_text(
            x_lr,
            y + rh_dev // 2,
            text="Tip sata:",
            anchor="e",
            font=font_lbl,
            fill=fill_txt,
            tags="ui_txt",
        )
        y += rh_dev + row_gap

        # anchor="e" za tekst: (x,y) je sredina desne ivice — y mora biti sredina widgeta u redu
        c.coords(self._cw_port_combo, x_c, y)
        self.root.update_idletasks()
        cw = max(self.port_combo.winfo_reqwidth(), 1)
        c.coords(self._cw_refresh, x_c + cw + 8, y)
        self.root.update_idletasks()
        rh_port = max(
            self.port_combo.winfo_reqheight(),
            self.refresh_btn.winfo_reqheight(),
            22,
        )
        c.create_text(
            x_lr,
            y + rh_port // 2,
            text="Port:",
            anchor="e",
            font=font_lbl,
            fill=fill_txt,
            tags="ui_txt",
        )
        y += rh_port + row_gap

        if self._get_device_kind() == CLOCK_DEVICE_BRICON:
            self.root.update_idletasks()
            biw = max(self.bricon_import_btn.winfo_reqwidth(), 1)
            c.coords(self._cw_bricon_import, cx - biw // 2, y)
            try:
                self._bg_canvas.itemconfigure(self._cw_bricon_import, state="normal")
            except tk.TclError:
                pass
            self.root.update_idletasks()
            rh_import = max(self.bricon_import_btn.winfo_reqheight(), 24)
            y += rh_import + row_gap
            bcw = max(self.bricon_capture_btn.winfo_reqwidth(), 1)
            c.coords(self._cw_bricon_capture, cx - bcw // 2, y)
            try:
                self._bg_canvas.itemconfigure(self._cw_bricon_capture, state="normal")
            except tk.TclError:
                pass
            self.root.update_idletasks()
            rh_capture = max(self.bricon_capture_btn.winfo_reqheight(), 24)
            y += rh_capture + row_gap
        else:
            try:
                self._bg_canvas.itemconfigure(self._cw_bricon_import, state="hidden")
                self._bg_canvas.itemconfigure(self._cw_bricon_capture, state="hidden")
            except tk.TclError:
                pass

        c.coords(self._cw_baud, x_c, y)
        self.root.update_idletasks()
        rh_baud = max(self.baud_combo.winfo_reqheight(), 22)
        c.create_text(
            x_lr,
            y + rh_baud // 2,
            text="Baud rate:",
            anchor="e",
            font=font_lbl,
            fill=fill_txt,
            tags="ui_txt",
        )
        y += rh_baud + row_gap

        c.coords(self._cw_slow_check, x_c, y)
        self.root.update_idletasks()
        ch_slow = max(self.slow_tauris_check.winfo_reqheight(), 18)
        # Canvas „window“ uvek je iznad teksta — tekst mora početi desno od stvarne širine kvačice.
        w_chk = max(self.slow_tauris_check.winfo_reqwidth(), 26)
        gap_after_chk = 12
        tx_slow = x_c + w_chk + gap_after_chk
        ty_slow = y + max(0, (ch_slow - 14) // 2)
        c.create_text(
            tx_slow,
            ty_slow,
            text=slow_txt,
            anchor="nw",
            width=max(320, w - tx_slow - 24),
            font=font_hi,
            fill=fill_txt,
            tags=("ui_txt", "slow_lbl"),
        )
        for seq in ("<Button-1>", "<Enter>", "<Leave>"):
            try:
                c.tag_unbind("slow_lbl", seq)
            except tk.TclError:
                pass
        c.tag_bind("slow_lbl", "<Button-1>", self._toggle_slow_check_via_canvas_label)
        c.tag_bind("slow_lbl", "<Enter>", lambda _e: c.config(cursor="hand2"))
        c.tag_bind("slow_lbl", "<Leave>", lambda _e: c.config(cursor=""))
        bb_slow = c.bbox("slow_lbl")
        y = (bb_slow[3] + row_gap + 4) if bb_slow else y + 52

        c.coords(self._cw_grupacija, x_c, y)
        self.root.update_idletasks()
        rh_g = max(self.grupacija_combo.winfo_reqheight(), 22)
        c.create_text(
            x_lr,
            y + rh_g // 2,
            text="Grupacija:",
            anchor="e",
            font=font_lbl,
            fill=fill_txt,
            tags="ui_txt",
        )
        y += rh_g + row_gap

        c.coords(self._cw_broj_trke, x_c, y)
        self.root.update_idletasks()
        rh_bt = max(self.broj_trke_combo.winfo_reqheight(), 22)
        c.create_text(
            x_lr,
            y + rh_bt // 2,
            text="Broj trke:",
            anchor="e",
            font=font_lbl,
            fill=fill_txt,
            tags="ui_txt",
        )
        y += rh_bt + row_gap

        c.coords(self._cw_mesto, x_c, y)
        self.root.update_idletasks()
        rh_me = max(self.mesto_entry.winfo_reqheight(), 22)
        c.create_text(
            x_lr,
            y + rh_me // 2,
            text="Mesto puštanja:",
            anchor="e",
            font=font_lbl,
            fill=fill_txt,
            tags="ui_txt",
        )
        y += rh_me + row_gap

        c.coords(self._cw_pilot, x_c, y)
        self.root.update_idletasks()
        rh_pe = max(self.pilot_entry.winfo_reqheight(), 22)
        c.create_text(
            x_lr,
            y + rh_pe // 2,
            text="Takmičar:",
            anchor="e",
            font=font_lbl,
            fill=fill_txt,
            tags="ui_txt",
        )
        y += rh_pe + row_gap

        self.root.update_idletasks()
        dhw = max(self.download_btn.winfo_reqwidth(), 1)
        c.coords(self._cw_download, cx - dhw // 2, y)
        self.root.update_idletasks()
        bh_btn = max(self.download_btn.winfo_reqheight(), DOWNLOAD_BTN_MIN_HEIGHT_PX)
        y += bh_btn + row_gap + 2

        self.root.update_idletasks()
        uhw = max(self.upload_folder_btn.winfo_reqwidth(), 1)
        uhh = max(self.upload_folder_btn.winfo_reqheight(), 26)
        c.coords(self._cw_upload_folder, cx - uhw // 2, y)
        y += uhh + row_gap

        c.create_text(cx, y, text=help_txt, anchor="n", width=460, font=font_hi, fill=fill_muted, tags="ui_txt")
        y += 52

        y_stat = min(y, h - 52)
        if getattr(self, "_main_status_text_id", None) is None:
            self._main_status_text_id = c.create_text(
                cx,
                y_stat,
                text=self.status_var.get(),
                anchor="n",
                width=460,
                font=font_hi,
                fill=fill_muted,
                tags="ui_status",
            )
        else:
            c.coords(self._main_status_text_id, cx, y_stat)
            c.itemconfigure(self._main_status_text_id, text=self.status_var.get())

        c.create_text(w - 10, h - 10, text="by Dragan Todorović", anchor="se", font=("Segoe UI", 9, "italic"), fill=fill_foot, tags="ui_txt")

    def _build_ui(self) -> None:
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        self.root.configure(bg=MAIN_WINDOW_SKY_BG)
        menu_baza = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Baza", menu=menu_baza)
        if not tauris_is_lite_edition():
            menu_baza.add_command(label="Otvori zaključan folder", command=self.baza_extract_pdfs_from_locks)
            menu_baza.add_command(label="Otvori zaključan paket", command=self.open_locked_bundle)
        menu_baza.add_command(label="Pošalji neposlate pakete", command=self.baza_send_pending)
        menu_baza.add_command(
            label="Provera Nadogradje",
            command=lambda: self.check_for_updates(auto_install=True),
        )

        self._main_bg_pil = None
        self._main_bg_photo = None
        self._main_last_client_wh: tuple[int, int] | None = None
        bg_path = get_resource_path(MAIN_WINDOW_BG_IMAGE_REL)
        if bg_path.is_file():
            try:
                from PIL import Image  # type: ignore

                self._main_bg_pil = Image.open(bg_path).convert("RGB")
            except Exception:
                logging.exception("Učitavanje pozadinske slike: %s", bg_path)
        else:
            logging.warning("Nedostaje slika pozadine (očekivano: %s)", bg_path)

        self._main_status_text_id: int | None = None

        self._bg_canvas = tk.Canvas(
            self.root,
            highlightthickness=0,
            borderwidth=0,
            bg=MAIN_WINDOW_SKY_BG,
        )
        self._bg_canvas.pack(fill=tk.BOTH, expand=True)

        cv = self._bg_canvas
        self.device_combo = ttk.Combobox(
            cv,
            textvariable=self.device_var,
            values=CLOCK_DEVICE_LABELS,
            width=14,
            state="readonly",
        )
        self._cw_device = cv.create_window(0, 0, window=self.device_combo, anchor="nw", tags=("ui_win",))
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_type_changed)
        self.device_var.trace_add("write", self._on_device_type_changed)

        self.port_combo = ttk.Combobox(
            cv,
            textvariable=self.port_var,
            width=18,
            state="readonly",
        )
        self._cw_port_combo = cv.create_window(0, 0, window=self.port_combo, anchor="nw", tags=("ui_win",))
        self.port_combo.bind("<<ComboboxSelected>>", self._on_port_selection_changed)
        self.port_var.trace_add("write", self._on_port_selection_changed)
        self.refresh_btn = tk.Button(
            cv,
            text="Osvezi (F5)",
            width=12,
            command=self.refresh_ports,
        )
        self._cw_refresh = cv.create_window(0, 0, window=self.refresh_btn, anchor="nw", tags=("ui_win",))

        self.bricon_import_btn = tk.Button(
            cv,
            text="Uvezi BRICON fajl",
            width=22,
            command=self.import_bricon_file_clicked,
        )
        self._cw_bricon_import = cv.create_window(
            0,
            0,
            window=self.bricon_import_btn,
            anchor="nw",
            tags=("ui_win",),
            state="hidden",
        )
        self.bricon_capture_btn = tk.Button(
            cv,
            text="Snimi COM (dijagnostika)",
            width=22,
            command=self.bricon_capture_com_clicked,
        )
        self._cw_bricon_capture = cv.create_window(
            0,
            0,
            window=self.bricon_capture_btn,
            anchor="nw",
            tags=("ui_win",),
            state="hidden",
        )

        self.baud_combo = ttk.Combobox(
            cv,
            textvariable=self.baud_var,
            width=16,
            state="readonly",
            values=BAUD_CHOICES,
        )
        self._cw_baud = cv.create_window(0, 0, window=self.baud_combo, anchor="nw", tags=("ui_win",))

        self.slow_tauris_check = tk.Checkbutton(
            cv,
            variable=self.slow_tauris_var,
            command=self._persist_gui_settings,
            highlightthickness=0,
        )
        self._cw_slow_check = cv.create_window(0, 0, window=self.slow_tauris_check, anchor="nw", tags=("ui_win",))

        self.grupacija_combo = ttk.Combobox(
            cv,
            textvariable=self.grupacija_var,
            values=GRUPACIJA_LABELS,
            width=27,
            state="readonly",
        )
        self._cw_grupacija = cv.create_window(0, 0, window=self.grupacija_combo, anchor="nw", tags=("ui_win",))

        self.broj_trke_combo = ttk.Combobox(
            cv,
            textvariable=self.broj_trke_var,
            values=BROJ_TRKE_LABELS,
            width=7,
            state="readonly",
        )
        self._cw_broj_trke = cv.create_window(0, 0, window=self.broj_trke_combo, anchor="nw", tags=("ui_win",))

        self.mesto_entry = tk.Entry(
            cv,
            textvariable=self.mesto_var,
            width=30,
            bg="white",
            highlightthickness=1,
            highlightbackground="#9cc8ef",
            highlightcolor="#6ba3d6",
        )
        self._cw_mesto = cv.create_window(0, 0, window=self.mesto_entry, anchor="nw", tags=("ui_win",))

        self.pilot_entry = tk.Entry(
            cv,
            textvariable=self.pilot_var,
            width=30,
            bg="white",
            highlightthickness=1,
            highlightbackground="#9cc8ef",
            highlightcolor="#6ba3d6",
        )
        self._cw_pilot = cv.create_window(0, 0, window=self.pilot_entry, anchor="nw", tags=("ui_win",))

        self.download_btn = tk.Button(
            cv,
            text="1)  PREUZMI PODATKE",
            width=DOWNLOAD_BTN_WIDTH,
            font=DOWNLOAD_BTN_FONT,
            bg=DOWNLOAD_BTN_DISABLED_BG,
            fg=DOWNLOAD_BTN_DISABLED_FG,
            activebackground=DOWNLOAD_BTN_ACTIVE_BG,
            activeforeground=DOWNLOAD_BTN_FG,
            disabledforeground=DOWNLOAD_BTN_DISABLED_FG,
            relief=tk.RAISED,
            borderwidth=DOWNLOAD_BTN_BORDER,
            highlightthickness=2,
            highlightbackground=DOWNLOAD_BTN_HIGHLIGHT_BG,
            highlightcolor=DOWNLOAD_BTN_HIGHLIGHT_COLOR,
            cursor="hand2",
            padx=DOWNLOAD_BTN_PADX,
            pady=DOWNLOAD_BTN_PADY,
            state="disabled",
            command=self.download_data,
        )
        self._cw_download = cv.create_window(0, 0, window=self.download_btn, anchor="nw", tags=("ui_win",))

        self.upload_folder_btn = tk.Button(
            cv,
            text="2) Pošalji podatke na obradu",
            width=32,
            command=self.send_folder_email_clicked,
        )
        self._cw_upload_folder = cv.create_window(0, 0, window=self.upload_folder_btn, anchor="nw", tags=("ui_win",))

        self._bg_canvas.bind("<Configure>", self._layout_main_window_background)
        self.status_var.trace_add("write", self._sync_status_canvas_text)
        self.root.after_idle(self._layout_main_window_background)
        self.refresh_ports()
        self._sync_device_dependent_ui()
        self._update_download_btn_appearance()

    def _update_download_btn_appearance(self) -> None:
        """Plavo dugme u tonu pozadine kad je aktivno; svetlo plavo-sivo kad nije spreman port."""
        try:
            if str(self.download_btn.cget("state")) == str(tk.DISABLED):
                self.download_btn.config(
                    bg=DOWNLOAD_BTN_DISABLED_BG,
                    fg=DOWNLOAD_BTN_DISABLED_FG,
                    activebackground=DOWNLOAD_BTN_DISABLED_BG,
                    highlightbackground=DOWNLOAD_BTN_HIGHLIGHT_BG,
                    highlightcolor=DOWNLOAD_BTN_HIGHLIGHT_BG,
                )
            else:
                self.download_btn.config(
                    bg=DOWNLOAD_BTN_BG,
                    fg=DOWNLOAD_BTN_FG,
                    activebackground=DOWNLOAD_BTN_ACTIVE_BG,
                    activeforeground=DOWNLOAD_BTN_FG,
                    highlightbackground=DOWNLOAD_BTN_HIGHLIGHT_BG,
                    highlightcolor=DOWNLOAD_BTN_HIGHLIGHT_COLOR,
                )
        except tk.TclError:
            pass

    def _set_busy(self, is_busy: bool) -> None:
        self.busy = is_busy
        common_state = "disabled" if is_busy else "normal"
        self.refresh_btn.config(state=common_state)
        self.baud_combo.config(state=common_state)
        self.slow_tauris_check.config(state=common_state)
        self.grupacija_combo.config(state="disabled" if is_busy else "readonly")
        self.broj_trke_combo.config(state="disabled" if is_busy else "readonly")
        self.mesto_entry.config(state=common_state)
        self.pilot_entry.config(state=common_state)
        self.upload_folder_btn.config(state=common_state)
        if hasattr(self, "bricon_capture_btn"):
            self.bricon_capture_btn.config(state=common_state)
        if is_busy:
            self.download_btn.config(state="disabled")
            self._update_download_btn_appearance()
            return
        if self._get_device_kind() == CLOCK_DEVICE_BRICON:
            self.download_btn.config(state="normal")
        else:
            self.download_btn.config(state="normal" if self.connected else "disabled")
        self._update_download_btn_appearance()

    def refresh_ports(self) -> None:
        """Portovi koje Windows vidi, sa opisom uređaja u listi."""
        entries = self._list_port_entries()
        ports = [dev for dev, _ in entries]
        self._port_devices = ports
        self._port_label_by_device = {dev: lbl for dev, lbl in entries}
        labels = [lbl for _, lbl in entries]

        saved = self._resolve_com_device(load_gui_settings_dict().get("port") or "")
        current_dev = self._resolve_com_device(self.port_var.get())
        self.port_combo["values"] = labels

        if not ports:
            if current_dev:
                self._sync_saved_port_in_settings("")
            self.port_var.set("")
            if self._get_device_kind() == CLOCK_DEVICE_BRICON:
                self.connected = False
                if not self.busy:
                    self.download_btn.config(state="normal")
                self._update_download_btn_appearance()
                self.status_var.set(self._bricon_port_status_hint())
            else:
                if self.connected:
                    self.connected = False
                    self.download_btn.config(state="disabled")
                    self._update_download_btn_appearance()
                self.status_var.set(
                    "Status: nema COM portova. Priključite sat i pritisnite F5 (Osveži)."
                )
            return

        stale = current_dev if current_dev and current_dev not in ports else ""
        if stale:
            self.port_var.set("")
            self._sync_saved_port_in_settings("")
            if self._get_device_kind() == CLOCK_DEVICE_BRICON:
                if not self.busy:
                    self.download_btn.config(state="normal")
            else:
                self.connected = False
                self.download_btn.config(state="disabled")
            self._update_download_btn_appearance()

        if current_dev in ports:
            self.port_var.set(self._label_for_com_device(current_dev))
            if saved and saved not in ports:
                self._sync_saved_port_in_settings(current_dev)
            self._apply_port_ready_state(current_dev)
            return

        if saved in ports:
            self.port_var.set(self._label_for_com_device(saved))
            self._apply_port_ready_state(saved)
            return

        pick = self._pick_preferred_com_port(ports)
        self.port_var.set(self._label_for_com_device(pick))
        self._sync_saved_port_in_settings(pick)
        if stale:
            self.status_var.set(
                f"Status: {stale} više nije priključen — izabran je {pick}. "
                "Proverite kabl i pritisnite F5 ako se port promenio."
            )
            self._apply_port_ready_state(pick, update_status=False)
        else:
            self._apply_port_ready_state(pick)

    def _list_available_ports(self) -> list[str]:
        if list_ports is None:
            return []
        return [port.device for port in list_ports.comports()]

    def _capture_staza_from_ui(self) -> tuple[str, int, str]:
        """Čitanje staze sa glavne niti pre pozadinskog preuzimanja."""
        g = (self.grupacija_var.get() or GRUPACIJA_LABELS[0]).strip()
        if g not in GRUPACIJA_LABELS:
            g = GRUPACIJA_LABELS[0]
        b = (self.broj_trke_var.get() or BROJ_TRKE_LABELS[0]).strip()
        if b not in BROJ_TRKE_LABELS:
            b = BROJ_TRKE_LABELS[0]
        try:
            n = int(b, 10)
        except ValueError:
            n = 1
        m = (self.mesto_var.get() or "").strip()
        return g, n, m

    def _get_config(self) -> TaurisConnectionConfig:
        try:
            baud = int(self.baud_var.get().strip() or "9600")
        except ValueError:
            baud = 9600
        port = self._resolve_com_device(self.port_var.get())
        if self.slow_tauris_var.get():
            return TaurisConnectionConfig(
                port=port,
                baudrate=baud,
                read_timeout=TAURIS_READ_TIMEOUT_SEC_SLOW,
                read_iterations=TAURIS_READ_MAX_ITERATIONS_SLOW,
                read_idle_empty_reads=TAURIS_READ_IDLE_EMPTY_READS_SLOW,
                tail_idle_empty_reads=TAURIS_TAIL_IDLE_EMPTY_READS_SLOW,
                tail_extra_max_iterations=TAURIS_TAIL_EXTRA_MAX_ITER_SLOW,
            )
        return TaurisConnectionConfig(port=port, baudrate=baud)

    def baza_extract_pdfs_from_locks(self) -> None:
        """Iz svih .taurislock u izabranom folderu izdvoji PDF-ove i TXT-ove u Open PDF/Open TXT pored tog foldera."""
        if tauris_is_lite_edition():
            messagebox.showinfo(
                "Lite izdanje",
                "Ova verzija programa ne podržava otvaranje zaključanog foldera.",
                parent=self.root,
            )
            return
        if self.busy:
            messagebox.showinfo("Zauzeto", "Sačekajte da završi preuzimanje ili povezivanje.")
            return
        initial = RESULTS_DESKTOP_ROOT if RESULTS_DESKTOP_ROOT.exists() else Path.home() / "Desktop"
        picked = filedialog.askdirectory(
            title="Izaberite folder sa .taurislock paketima",
            initialdir=str(initial),
        )
        if not picked:
            return
        source = Path(picked)
        locks = sorted(source.glob("*.taurislock"))
        if not locks:
            messagebox.showwarning(
                "Nema paketa",
                f"U izabranom folderu nema .taurislock fajlova:\n{source}",
            )
            return
        access_key = request_access_key_file(self.root)
        if access_key is None:
            self.status_var.set("Status: tražena radnja nije mogla da se izvrši.")
            return
        entered = simpledialog.askstring(
            "Lozinka",
            "Unesite šifru za otključavanje paketa:",
            show="*",
        )
        if entered is None or not entered.strip():
            return
        pw_unlock = entered.strip()
        logging.info("Baza otključavanje foldera odobreno ključem: %s", access_key)
        out_dir = source.resolve().parent / OPEN_PDF_FOLDER_NAME
        out_txt_dir = source.resolve().parent / OPEN_TXT_FOLDER_NAME
        try:
            n_pdf, errs = extract_all_pdfs_from_taurislocks_in_dir(
                source, pw_unlock, dest_dir=out_dir
            )
            n_txt, txt_errs = extract_all_txt_from_taurislocks_in_dir(
                source, pw_unlock, dest_dir=out_txt_dir
            )
        except Exception as exc:
            messagebox.showerror("Greška", str(exc))
            logging.exception("Baza: izdvajanje PDF/TXT iz .taurislock")
            return
        lines = [
            f"Kopirano PDF fajlova: {n_pdf}",
            f"Kopirano TXT fajlova: {n_txt}",
            "",
            f"Open PDF: {out_dir}",
            f"Open TXT: {out_txt_dir}",
        ]
        all_errs = errs + txt_errs
        if all_errs:
            lines.append("")
            lines.append("Upozorenja / greške:")
            lines.extend(all_errs[:20])
            if len(all_errs) > 20:
                lines.append(f"… i još {len(all_errs) - 20}.")
        messagebox.showinfo("Baza — Open PDF / Open TXT", "\n".join(lines))
        try:
            if sys.platform == "win32":
                os.startfile(str(out_dir))  # type: ignore[attr-defined]
                os.startfile(str(out_txt_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(out_dir)], start_new_session=True)
                subprocess.Popen(["open", str(out_txt_dir)], start_new_session=True)
            else:
                subprocess.Popen(["xdg-open", str(out_dir)], start_new_session=True)
                subprocess.Popen(["xdg-open", str(out_txt_dir)], start_new_session=True)
        except Exception:
            logging.exception("Neuspešno otvaranje Open PDF/Open TXT foldera.")

    def baza_send_pending(self) -> None:
        """Baza → Pošalji neposlate pakete: ručno pokretanje naknadnog slanja iz reda."""
        from tauris_desktop.pending_sends import load_pending_sends, summarize_pending_for_user

        items = load_pending_sends()
        if not items:
            messagebox.showinfo("Neposlati paketi", "Nema neposlatih paketa — sve je poslato.", parent=self.root)
            return
        if not messagebox.askyesno(
            "Neposlati paketi",
            summarize_pending_for_user(items) + "\n\nPokušati slanje sada?",
            parent=self.root,
        ):
            return
        self._retry_pending_sends_background(silent=False)

    def _retry_pending_sends_background(self, *, silent: bool) -> None:
        """Pokušaj slanja paketa iz reda u pozadinskoj niti; rezultat stiže kroz worker_queue."""
        from tauris_desktop.pending_sends import pending_count

        if self._pending_retry_running:
            return
        if silent and pending_count() == 0:
            return
        self._pending_retry_running = True
        if not silent:
            self._set_busy(True)
            self.status_var.set("Status: šaljem neposlate pakete na obradu…")

        def _worker() -> None:
            from tauris_desktop.pending_sends import retry_pending_sends

            try:
                sent, remaining, notes = retry_pending_sends(self.delivery_config)
            except Exception as exc:
                logging.exception("Naknadno slanje paketa iz reda nije uspelo.")
                sent, remaining, notes = 0, pending_count(), [f"Greška: {exc}"]
            finally:
                self._pending_retry_running = False
            if silent and sent == 0:
                return
            self.worker_queue.put(("pending_sends_done", (sent, remaining, notes, silent)))

        threading.Thread(target=_worker, daemon=True).start()

    def check_for_updates(self, *, silent_if_current: bool = False, auto_install: bool = False) -> None:
        if self._upgrade_in_progress:
            return
        if self.busy:
            if not silent_if_current:
                messagebox.showinfo("Zauzeto", "Sačekajte da završi tekuća operacija.")
            return
        if not silent_if_current:
            self.status_var.set("Status: proveravam da li postoji novija verzija na GitHub-u...")
            self.root.update_idletasks()

        def worker() -> None:
            release: dict[str, Any] | None = None
            err_text: str | None = None
            try:
                release = fetch_github_latest_release()
            except Exception as exc:
                err_text = str(exc).strip() or "Nepoznata greška."

            def done() -> None:
                if err_text:
                    if silent_if_current:
                        logging.warning("Automatska provera nadogradnje: %s", err_text)
                        return
                    self.status_var.set("Status: provera nadogradnje nije uspela (mreža / GitHub).")
                    if messagebox.askyesno(
                        "Provera Nadogradnje",
                        err_text + "\n\nOtvoriti stranicu GitHub Releases u pregledaču?",
                        parent=self.root,
                    ):
                        try:
                            import webbrowser

                            webbrowser.open(GITHUB_RELEASES_URL)
                        except Exception:
                            logging.exception("Otvaranje GitHub Releases u pregledaču.")
                    return

                assert release is not None
                latest_version = str(release.get("latest_version") or "").strip() or APP_VERSION
                html_url = str(release.get("html_url") or GITHUB_RELEASES_URL)
                assets = release.get("assets")
                asset_list = assets if isinstance(assets, list) else []

                if compare_version_strings(APP_VERSION, latest_version) >= 0:
                    self.status_var.set(f"Status: verzija {APP_VERSION} je aktuelna.")
                    if not silent_if_current:
                        messagebox.showinfo(
                            "Provera Nadogradje",
                            f"Nema novije verzije na GitHub-u.\n\n"
                            f"Aktuelna verzija programa: {APP_VERSION}\n"
                            f"Poslednji release na mreži: {latest_version}",
                        )
                    return

                picked = pick_github_release_installer_asset(
                    asset_list,
                    lite=tauris_is_lite_edition(),
                )
                if not picked:
                    if silent_if_current:
                        logging.warning("Nadogradnja %s bez Setup.exe na GitHub-u.", latest_version)
                        return
                    messagebox.showinfo(
                        "Provera Nadogradje",
                        "Nova verzija postoji, ali release nema Setup.exe.\n"
                        f"Otvorite ručno: {html_url}",
                    )
                    return

                asset_name = str(picked.get("name") or "TaurisPdfApp_Setup.exe").strip()
                download_url = str(picked.get("browser_download_url") or "").strip()
                if not download_url:
                    if not silent_if_current:
                        messagebox.showinfo(
                            "Provera Nadogradje",
                            f"Nema link za preuzimanje.\nOtvorite: {html_url}",
                        )
                    return

                if auto_install:
                    if not getattr(sys, "frozen", False):
                        if not silent_if_current:
                            messagebox.showinfo(
                                "Provera Nadogradje",
                                f"Dostupna je verzija {latest_version}.\n"
                                "Automatska instalacija radi samo iz instaliranog programa (.exe).",
                            )
                        return
                    self._run_automatic_upgrade(latest_version, download_url, asset_name)
                    return

                self.status_var.set(
                    f"Status: dostupna je novija verzija ({latest_version}). Ručno preuzimanje…"
                )
                initial_dir = Path.home() / "Downloads"
                save_path = filedialog.asksaveasfilename(
                    title="Sačuvaj nadogradnju",
                    initialdir=str(initial_dir),
                    initialfile=asset_name,
                    defaultextension=Path(asset_name).suffix,
                )
                if not save_path:
                    return
                target = Path(save_path)
                self.status_var.set("Status: preuzimam nadogradnju sa GitHub-a...")
                self.root.update_idletasks()
                try:
                    download_release_asset(download_url, target)
                except Exception as exc:
                    self.status_var.set("Status: preuzimanje nadogradnje nije uspelo.")
                    messagebox.showerror("Provera Nadogradje", f"Neuspešno preuzimanje.\n\n{exc}")
                    return
                self.status_var.set(f"Status: nadogradnja sačuvana u {target}.")
                messagebox.showinfo("Provera Nadogradje", f"Preuzeto:\n{target}")

            self.root.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _run_automatic_upgrade(
        self, latest_version: str, download_url: str, asset_name: str
    ) -> None:
        self._upgrade_in_progress = True
        self.busy = True
        self.status_var.set(
            f"Status: automatska nadogradnja na ver {latest_version} — preuzimanje…"
        )
        self.root.update_idletasks()
        try:
            self.download_btn.config(state="disabled")
        except Exception:
            pass

        def worker() -> None:
            err: BaseException | None = None
            target = automatic_update_installer_target(latest_version, asset_name)
            try:
                download_release_asset(download_url, target)
            except Exception as exc:
                err = exc

            def finish() -> None:
                if err is not None:
                    self._upgrade_in_progress = False
                    self.busy = False
                    self.status_var.set("Status: automatska nadogradnja nije uspela.")
                    logging.exception("Automatska nadogradnja: %s", download_url)
                    messagebox.showerror(
                        "Nadogradnja",
                        f"Preuzimanje verzije {latest_version} nije uspelo.\n\n{err}",
                    )
                    return
                self.status_var.set(
                    f"Status: instaliram ver {latest_version} — zatvaram program…"
                )
                self.root.update_idletasks()
                try:
                    launch_automatic_windows_upgrade(target)
                except Exception as exc:
                    self._upgrade_in_progress = False
                    self.busy = False
                    messagebox.showerror(
                        "Nadogradnja",
                        f"Preuzeto, ali pokretanje instalacije nije uspelo.\n\n{exc}",
                    )
                    return
                self.root.after(350, self._exit_for_upgrade)

            self.root.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _exit_for_upgrade(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    def bricon_capture_com_clicked(self) -> None:
        """Snimi sav TX/RX na COM portu (~3 min) za uporedbu sa Print Managerom."""
        if self.busy:
            return
        if serial is None:
            messagebox.showerror(
                "Greska",
                "Nedostaje pyserial. Instalirajte: pip install pyserial",
                parent=self.root,
            )
            return
        if self._get_device_kind() != CLOCK_DEVICE_BRICON:
            return
        self.refresh_ports()
        port = self._resolve_com_device(self.port_var.get())
        if not port or port not in self._reliable_com_ports():
            messagebox.showwarning(
                "BRICON snimak",
                "Izaberite BRICON USB port (F5) pre snimanja.\n\n"
                "Zatvorite Print Manager — port moze samo jedan program.",
                parent=self.root,
            )
            return
        if not messagebox.askokcancel(
            "Snimi BRICON COM",
            "Snima se sav saobracaj na portu "
            f"{port} (~3 minuta).\n\n"
            "1) Zatvorite Print Manager\n"
            "2) Kliknite OK — program pita sat PM protokolom (9600 8N2)\n"
            "3) Na satu ne treba nista da pritiskate\n\n"
            "Log se cuva u logs/bricon_capture/ za analizu.",
            parent=self.root,
        ):
            return
        self._set_busy(True)
        self.status_var.set(f"Status: snimam COM {port} — čekam odgovor sata…")
        threading.Thread(
            target=self._worker_bricon_com_capture,
            args=(port, 180.0),
            daemon=True,
        ).start()

    def _worker_bricon_com_capture(self, port: str, duration_sec: float) -> None:
        try:
            from tauris_desktop.bricon_capture import (
                analyze_capture_file,
                run_com_capture_session,
            )

            path, summary = run_com_capture_session(
                port,
                duration_sec=duration_sec,
                use_pm_protocol=True,
            )
            report = analyze_capture_file(path)
            self.worker_queue.put(
                ("bricon_capture_ok", (path, summary, report)),
            )
        except Exception as exc:
            logging.exception("BRICON COM snimak.")
            self.worker_queue.put(("bricon_capture_err", exc))

    def _bricon_import_initial_dir(self) -> str:
        """Poslednji korišćeni folder, pa Print Manager folderi (C:\\ReadOuts\\Printmanager…), pa Preuzimanja."""
        return bricon_import_initial_dir(load_gui_settings_dict().get("bricon_import_dir"))

    def import_bricon_file_clicked(self) -> None:
        if self.busy:
            return
        initial = self._bricon_import_initial_dir()
        picked = filedialog.askopenfilename(
            title="Uvezite izvoz sa BRICON Print Managera",
            initialdir=initial,
            filetypes=[
                ("BRICON / tekst", "*.txt *.csv *.log *.brc *.bricon *.dat"),
                ("Svi fajlovi", "*.*"),
            ],
            parent=self.root,
        )
        if not picked:
            return
        try:
            d = load_gui_settings_dict()
            d["bricon_import_dir"] = str(Path(picked).parent)
            save_gui_settings_dict(d)
        except Exception:
            logging.debug("Pamćenje foldera za BRICON uvoz nije uspelo.", exc_info=True)
        lock_pw = get_configured_taurislock_password() or DEFAULT_TAURISLOCK_BUNDLE_PASSWORD
        self._set_busy(True)
        self.status_var.set("Status: učitavam BRICON fajl…")
        staza = self._capture_staza_from_ui()
        pilot_name = self.pilot_var.get().strip() or UNKNOWN_PILOT_LABEL
        threading.Thread(
            target=self._worker_bricon_file_import,
            args=(Path(picked), pilot_name, staza, lock_pw),
            daemon=True,
        ).start()

    def download_data(self) -> None:
        if serial is None:
            messagebox.showerror("Greska", "Nedostaje pyserial paket. Instalirajte: pip install pyserial")
            return
        if self.busy:
            return
        self.refresh_ports()
        port = self._resolve_com_device(self.port_var.get())
        available = set(self._port_devices)
        is_bricon = self._get_device_kind() == CLOCK_DEVICE_BRICON
        dev = self._device_display_name()
        unusable = com_port_usability_issue(port) if port else None
        if unusable:
            messagebox.showwarning("Port nije pouzdan", unusable, parent=self.root)
            self.port_var.set("")
            self._sync_saved_port_in_settings("")
            self.connected = False
            self.download_btn.config(state="disabled")
            self._update_download_btn_appearance()
            return

        if not port or port not in available:
            if is_bricon and not available:
                messagebox.showwarning(
                    "BRICON — nema COM porta",
                    self._bricon_no_port_dialog_text(),
                    parent=self.root,
                )
            elif port and port not in available:
                alive = ", ".join(sorted(available)) if available else "nema"
                messagebox.showwarning(
                    "Port nije dostupan",
                    f"Port {port} Windows trenutno ne vidi.\n\n"
                    f"Pritisnite F5 (Osveži). Dostupno: {alive}.\n\n"
                    + (
                        "Za BRICON bez COM-a koristite «Uvezi BRICON fajl»."
                        if is_bricon
                        else f"Izaberite {dev} port iz liste."
                    ),
                    parent=self.root,
                )
                self.port_var.set("")
                self._sync_saved_port_in_settings("")
                self.connected = False
                self.download_btn.config(state="disabled")
                self._update_download_btn_appearance()
            else:
                messagebox.showwarning(
                    "Upozorenje",
                    self._bricon_no_port_dialog_text() if is_bricon else (
                        f"Izaberite {dev} COM port sa liste ili pritisnite F5 (Osveži)."
                    ),
                    parent=self.root,
                )
            return

        lock_pw = get_configured_taurislock_password() or DEFAULT_TAURISLOCK_BUNDLE_PASSWORD

        self._cancel_download.clear()
        self._set_busy(True)
        if self._get_device_kind() == CLOCK_DEVICE_BRICON:
            self.status_var.set(
                "Status: BRICON USB — tražim sat na portu (Print Manager protokol)…"
            )
        else:
            self.status_var.set(
                "Status: čitam COM. Na TAURIS-u pokrenite slanje podataka u tom trenutku."
            )
        config = self._get_config()
        if is_bricon:
            config.candidate_ports = tuple(self._bricon_auto_port_candidates(port))
        pilot_name = self.pilot_var.get().strip() or UNKNOWN_PILOT_LABEL
        staza = self._capture_staza_from_ui()
        device_kind = self._get_device_kind()

        thread = threading.Thread(
            target=self._worker_live_download,
            args=(config, pilot_name, staza, lock_pw, device_kind),
            daemon=True,
        )
        thread.start()

    def _resolve_or_choose_upload_folder(self) -> Path | None:
        """Prvo automatski folder za izabranu grupaciju i broj trke na Desktopu; inače dijalog."""
        g, n, _m = self._capture_staza_from_ui()
        b = f"{max(1, min(MAX_BROJ_TRKE, n)):03d}"
        found = resolve_upload_folder_for_current_staza(g, b)
        if found is not None:
            logging.info("Automatski izabran folder za slanje: %s", found)
            return found
        initial = str(RESULTS_DESKTOP_ROOT) if RESULTS_DESKTOP_ROOT.exists() else str(Path.home() / "Desktop")
        folder = filedialog.askdirectory(
            title="Nije pronađen folder za izabranu grupaciju i broj trke — izaberite ručno",
            initialdir=initial,
            parent=self.root,
        )
        if not folder:
            return None
        return Path(folder)

    def send_folder_email_clicked(self) -> None:
        if self.busy:
            return
        if not smtp_settings_complete(self.delivery_config):
            dd = distribution_dir()
            messagebox.showwarning(
                "Slanje na obradu — podešavanje",
                "Nema kompletnog podešavanja za slanje podataka na obradu.\n\n"
                "Distribucija: pored programa stavite fajl\n"
                f"  {dd / TAURIS_CLUB_EMAIL_FILENAME}\n"
                "(kopirajte od tauris_club_email.sample.json i popunite SMTP polja), "
                "ili postavite Windows promenljive TAURIS_SMTP_* (imaju prioritet).\n\n"
                "Konstante u izvornom kodu ne sadrže lozinku — za klupski build koristite JSON pored .exe.\n\n"
                "Zatim ponovo pokrenite program.",
                parent=self.root,
            )
            return
        folder_path = self._resolve_or_choose_upload_folder()
        if folder_path is None:
            return

        g, n, _m = self._capture_staza_from_ui()
        broj_trke = f"{max(1, min(MAX_BROJ_TRKE, n)):03d}"

        self._set_busy(True)
        self.status_var.set(
            "Status: šalju se podaci na obradu (prilozi bez ZIP arhive)…"
        )
        threading.Thread(
            target=self._worker_folder_email_upload,
            args=(folder_path, g, broj_trke),
            daemon=True,
        ).start()

    def _worker_folder_email_upload(
        self, folder: Path, grupacija: str, broj_trke: str
    ) -> None:
        try:
            notes = send_folder_to_obrada_by_email(
                folder,
                broj_trke=broj_trke,
                grupacija=grupacija,
                config=self.delivery_config,
            )
            summary = "\n".join(notes[:30])
            if len(notes) > 30:
                summary += f"\n… (+ još {len(notes) - 30} linija)"
            self.worker_queue.put(("folder_email_ok", (len(notes), summary)))
        except Exception as exc:
            logging.exception("Email slanje foldera.")
            if _is_network_unreachable_error(exc):
                try:
                    fp = folder.resolve()
                except Exception:
                    fp = folder
                self.worker_queue.put(("folder_email_err", _offline_send_instructions_for_data_folder(fp)))
            else:
                self.worker_queue.put(("folder_email_err", exc))

    def _worker_bricon_file_import(
        self,
        path: Path,
        pilot_name: str,
        staza: tuple[str, int, str],
        lock_password: str,
    ) -> None:
        try:
            from tauris_desktop.bricon import read_race_from_bricon_file

            race_data = read_race_from_bricon_file(path)
            g, n, m = staza
            race_data = apply_staza_ui_to_race_data(
                race_data,
                grupacija_label=g,
                broj_trke_num=n,
                mesto_pustanja=m,
            )
            pilot_effective = resolve_pilot_name_for_output(race_data, pilot_name)
            locked_bundle_path, delivery_note, preview_pdf_bytes = run_pipeline(
                race_data,
                pilot_effective,
                f"BRICON:{path.name}",
                self.delivery_config,
                lock_password=lock_password,
            )
            self.worker_queue.put(
                (
                    "ok",
                    (
                        locked_bundle_path,
                        delivery_note,
                        preview_pdf_bytes,
                        pilot_effective,
                        race_data,
                    ),
                )
            )
        except Exception as exc:
            logging.exception("BRICON uvoz fajla.")
            self.worker_queue.put(("err", exc))

    def _worker_live_download(
        self,
        config: TaurisConnectionConfig,
        pilot_name: str,
        staza: tuple[str, int, str],
        lock_password: str,
        device_kind: str,
    ) -> None:
        last_show = [0.0]
        dev_label = "BRICON" if device_kind == CLOCK_DEVICE_BRICON else "TAURIS"
        hint = (
            "BRICON: program čita sat — sačekajte, ne isključujte kabl."
            if device_kind == CLOCK_DEVICE_BRICON
            else "Na TAURIS-u zadržite slanje podataka do kraja liste."
        )

        def on_bytes(n: int) -> None:
            t = time.monotonic()
            if t - last_show[0] < 0.22:
                return
            last_show[0] = t
            self.root.after(
                0,
                lambda nb=n: self.status_var.set(
                    f"Status: čitam {dev_label} COM — primljeno {nb} bajtova. {hint}"
                ),
            )

        last_phase = [0.0]

        def on_phase(msg: str) -> None:
            t = time.monotonic()
            if t - last_phase[0] < 0.15:
                return
            last_phase[0] = t
            self.root.after(0, lambda m=msg: self.status_var.set(f"Status: {dev_label} — {m}"))

        def should_cancel() -> bool:
            return self._cancel_download.is_set()

        race_data: dict[str, Any] | None = None
        try:
            race_data = read_race_from_clock(
                config,
                device_kind,
                on_com_bytes=on_bytes,
                on_phase=on_phase,
                should_cancel=should_cancel,
                slow_uart=bool(self.slow_tauris_var.get()),
            )
            g, n, m = staza
            race_data = apply_staza_ui_to_race_data(
                race_data,
                grupacija_label=g,
                broj_trke_num=n,
                mesto_pustanja=m,
            )
            pilot_effective = resolve_pilot_name_for_output(race_data, pilot_name)
            locked_bundle_path, delivery_note, preview_pdf_bytes = run_pipeline(
                race_data,
                pilot_effective,
                config.port,
                self.delivery_config,
                lock_password=lock_password,
            )
            self.worker_queue.put(
                (
                    "ok",
                    (
                        locked_bundle_path,
                        delivery_note,
                        race_data,
                        preview_pdf_bytes,
                        pilot_effective,
                    ),
                )
            )
            logging.info("Preuzimanje uspesno sa porta %s", config.port)
        except Exception as exc:
            from tauris_desktop.bricon import BriconCancelled

            if isinstance(exc, (BriconCancelled, TaurisCancelled)):
                logging.info("Preuzimanje otkazano od strane korisnika (port %s).", config.port)
                self.worker_queue.put(("cancelled", str(exc)))
                return
            logging.exception("Greska tokom live preuzimanja.")
            if race_data is not None:
                # Podaci su uspešno pročitani sa uređaja — greška je u koraku POSLE čitanja
                # (PDF/zaključavanje/slanje). Preuzimanje sa satova je često jednokratno
                # (korisnik mora ponovo pokrenuti slanje), pa se već primljena lista ne sme
                # izgubiti — snimi je za oporavak pre prijave greške.
                try:
                    if device_kind == CLOCK_DEVICE_BRICON:
                        from tauris_desktop.bricon_capture import save_race_data_recovery_snapshot

                        snap_path = save_race_data_recovery_snapshot(
                            race_data, port=config.port, reason=f"{type(exc).__name__}: {exc}"
                        )
                        label = "BRICON"
                    else:
                        from tauris_desktop.tauris_capture import (
                            save_tauris_race_data_recovery_snapshot,
                        )

                        snap_path = save_tauris_race_data_recovery_snapshot(
                            race_data, port=config.port, reason=f"{type(exc).__name__}: {exc}"
                        )
                        label = "TAURIS"
                    exc = RuntimeError(
                        f"{exc}\n\n{label} podaci su uspešno pročitani i sačuvani radi oporavka:\n{snap_path}"
                    )
                except Exception:
                    logging.exception("Snimanje race_data recovery snapshot-a nije uspelo.")
            self.worker_queue.put(("error", exc))

    def _poll_worker_queue(self) -> None:
        try:
            event_type, payload = self.worker_queue.get_nowait()
        except Empty:
            self.root.after(200, self._poll_worker_queue)
            return

        self._set_busy(False)
        if event_type == "folder_email_ok":
            count, summary = payload
            self.status_var.set(
                f"Status: podaci poslati na obradu ({count} mejl"
                f"{'ova' if count != 1 else ''}; prilozi bez ZIP-a)."
            )
            messagebox.showinfo("Slanje na obradu", summary, parent=self.root)
        elif event_type == "folder_email_err":
            if isinstance(payload, str):
                self.status_var.set(
                    "Status: nema internet veze. Ručno pošaljite administratoru podatke (lokacija u poruci)."
                )
                messagebox.showwarning("Slanje na obradu", payload, parent=self.root)
            else:
                self.status_var.set("Status: greška pri slanju podataka na obradu.")
                messagebox.showerror("Slanje na obradu", str(payload), parent=self.root)
        elif event_type == "ok":
            locked_bundle_path, delivery_note, race_data, preview_pdf_bytes, pilot_effective = payload
            self.last_locked_bundle_path = locked_bundle_path
            found_port = race_data.get("bricon_port") if isinstance(race_data, dict) else None
            if found_port and found_port != self._resolve_com_device(self.port_var.get()):
                # Auto-port je našao sat na drugom portu — zapamti ga za sledeći put.
                logging.info("BRICON auto-port: izabran %s (pronađen sat)", found_port)
                self.port_var.set(self._label_for_com_device(found_port))
                self._sync_saved_port_in_settings(found_port)
            if (
                isinstance(pilot_effective, str)
                and pilot_effective.strip()
                and pilot_effective != UNKNOWN_PILOT_LABEL
            ):
                self.pilot_var.set(_clean_takmicar_display(pilot_effective))
                self._persist_gui_settings()
            status_tak = (self.pilot_var.get() or "").strip()
            if status_tak and status_tak != UNKNOWN_PILOT_LABEL:
                self.status_var.set(
                    f"Status: gotovo — sačuvan paket (.taurislock). Takmičar: {status_tak}."
                )
            else:
                self.status_var.set("Status: gotovo. Sačuvan je samo zaključan paket (.taurislock).")
            logging.info(
                "Zaključan paket sačuvan — %s | %s",
                locked_bundle_path,
                delivery_note,
            )
            self.root.after(80, lambda: notify_download_complete(self.root))
            self.root.after(30000, lambda: self._retry_pending_sends_background(silent=True))
            self.root.after(
                180,
                lambda p=locked_bundle_path, rd=race_data, dn=delivery_note: show_download_result_preview(
                    self.root, p, rd if isinstance(rd, dict) else {}, dn
                ),
            )
            try:
                preview_path = write_preview_pdf_temp(preview_pdf_bytes)
                self.root.after(260, lambda p=preview_path: show_pdf_embedded_preview(self.root, p))
                self.root.after(240000, lambda p=preview_path: p.unlink(missing_ok=True))
            except Exception:
                logging.exception("Neuspešna priprema PDF preview-a iz privremenog fajla.")
        elif event_type == "pending_sends_done":
            sent, remaining, notes, silent = payload
            if sent or remaining:
                self.status_var.set(
                    f"Status: naknadno slanje — poslato {sent}, u redu još {remaining}."
                )
            if not silent:
                body = "\n".join(notes) if notes else "Nema neposlatih paketa."
                messagebox.showinfo("Neposlati paketi", f"Poslato: {sent}\nPreostalo u redu: {remaining}\n\n{body}", parent=self.root)
            elif sent:
                messagebox.showinfo(
                    "Neposlati paketi",
                    f"Internet je dostupan — naknadno poslato {sent} paket(a) na obradu."
                    + (f"\nU redu je ostalo još {remaining}." if remaining else ""),
                    parent=self.root,
                )
        elif event_type == "bricon_capture_ok":
            path, summary, report = payload
            self.status_var.set(f"Status: COM snimak sacuvan — {path.name}")
            if messagebox.askyesno(
                "BRICON COM snimak",
                f"Sacuvano:\n{path}\n\n{summary}\n\n--- Analiza ---\n{report}\n\n"
                "Otvoriti folder sa logovima?",
                parent=self.root,
            ):
                try:
                    import os

                    os.startfile(str(path.parent))
                except Exception:
                    messagebox.showinfo("Folder", str(path.parent), parent=self.root)
        elif event_type == "bricon_capture_err":
            self.status_var.set("Status: COM snimak nije uspeo.")
            messagebox.showerror("BRICON COM snimak", str(payload), parent=self.root)
        elif event_type == "error":
            self.status_var.set("Status: greska tokom obrade.")
            messagebox.showerror("Greska", str(payload), parent=self.root)
        elif event_type == "cancelled":
            self.status_var.set("Status: preuzimanje otkazano.")
        else:
            logging.warning("Nepoznat događaj iz radne niti: %s", event_type)
        self.root.after(200, self._poll_worker_queue)

    def open_locked_bundle(self) -> None:
        if tauris_is_lite_edition():
            messagebox.showinfo(
                "Lite izdanje",
                "Ova verzija programa ne podržava otvaranje zaključanog paketa.",
                parent=self.root,
            )
            return
        initial_dir = RESULTS_DESKTOP_ROOT if RESULTS_DESKTOP_ROOT.exists() else Path.home() / "Desktop"
        initial_file = str(self.last_locked_bundle_path) if self.last_locked_bundle_path else ""
        selected = filedialog.askopenfilenames(
            title="Izaberite jedan ili više .taurislock paketa (Ctrl+klik za više)",
            initialdir=str(initial_dir),
            initialfile=initial_file,
            filetypes=[("Tauris zaključan paket", "*.taurislock"), ("Svi fajlovi", "*.*")],
        )
        if not selected:
            return
        bundles = [
            Path(p).resolve()
            for p in selected
            if Path(p).is_file() and Path(p).suffix.lower() == ".taurislock"
        ]
        if not bundles:
            messagebox.showwarning(
                "Izbor",
                "Nije izabran nijedan važeći .taurislock fajl.\n\n"
                "U dijalogu držite Ctrl i kliknite više fajlova, ili izaberite jedan.",
            )
            return
        access_key = request_access_key_file(self.root)
        if access_key is None:
            self.status_var.set("Status: tražena radnja nije mogla da se izvrši.")
            return

        entered = simpledialog.askstring(
            "Lozinka",
            "Unesite šifru za otključavanje paketa:",
            show="*",
        )
        if entered is None or not entered.strip():
            return
        pw_unlock = entered.strip()
        logging.info("Baza otključavanje paketa odobreno ključem: %s", access_key)

        mode = show_locked_bundle_action_choice(self.root)
        if mode is None:
            return

        def _schedule_cleanup_tmp(tmp: Path) -> None:
            def _cleanup() -> None:
                try:
                    shutil.rmtree(tmp, ignore_errors=True)
                except Exception:
                    logging.exception("Neuspešno brisanje privremenog foldera: %s", tmp)

            self.root.after(180000, _cleanup)

        if mode == "prikaz":
            ok_n = 0
            for bundle in bundles:
                tmp_dir = Path(tempfile.mkdtemp(prefix="tauris_unlocked_"))
                try:
                    unlock_results_bundle(bundle, pw_unlock, tmp_dir)
                except Exception as exc:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    messagebox.showerror("Greška", f"{bundle.name}:\n{exc}")
                    continue
                ok_n += 1
                _schedule_cleanup_tmp(tmp_dir)
                for p in sorted(tmp_dir.glob("*.pdf")):
                    try:
                        os.startfile(str(p))  # type: ignore[attr-defined]
                    except Exception:
                        logging.exception("Neuspešno otvaranje PDF-a iz paketa: %s", p)
                    break
                try:
                    if sys.platform == "win32":
                        os.startfile(str(tmp_dir))  # type: ignore[attr-defined]
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", str(tmp_dir)], start_new_session=True)
                    else:
                        subprocess.Popen(["xdg-open", str(tmp_dir)], start_new_session=True)
                except Exception:
                    logging.exception("Neuspešno otvaranje privremenog foldera: %s", tmp_dir)
            self.status_var.set(
                f"Status: otključano {ok_n}/{len(bundles)} paketa (privremeni folderi; PDF + Explorer po paketu)."
            )
            return

        # Raspakovanje .txt — za svaki paket poseban dijalog takmičara, izlaz u txt dokumenti pored tog .taurislock
        opened_out_dirs: list[Path] = []
        total_txt = 0
        for bundle in bundles:
            tmp_dir = Path(tempfile.mkdtemp(prefix="tauris_unlocked_"))
            try:
                unlock_results_bundle(bundle, pw_unlock, tmp_dir)
            except Exception as exc:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                messagebox.showerror("Greška", f"{bundle.name}:\n{exc}")
                continue
            _schedule_cleanup_tmp(tmp_dir)

            json_paths = sorted(tmp_dir.glob("*.json"))
            if not json_paths:
                messagebox.showerror("Greška", f"{bundle.name}: u paketu nema JSON fajla.")
                shutil.rmtree(tmp_dir, ignore_errors=True)
                continue
            try:
                race_data = load_race_data_from_bundle_json(json_paths[0])
            except Exception as exc:
                messagebox.showerror("Greška", f"{bundle.name}:\n{exc}")
                continue

            raw = race_data.get("raw_com_text")
            if not isinstance(raw, str) or not raw.strip():
                txts = sorted(tmp_dir.glob("*.txt"))
                if txts:
                    try:
                        raw = txts[0].read_text(encoding="utf-8")
                    except Exception:
                        raw = ""
                else:
                    raw = ""
            if not (raw or "").strip():
                messagebox.showerror("Greška", f"{bundle.name}: nema sirovog COM teksta za .txt.")
                continue

            segments = discover_takmicar_segments_from_raw_com_text(
                raw,
                race_data=race_data,
                bundle_json_path=json_paths[0],
            )
            if not segments:
                messagebox.showerror("Greška", f"{bundle.name}: nije moguće prepoznati takmičare.")
                continue

            picked = show_takmicar_multi_select_dialog(
                self.root, segments, bundle_label=bundle.name
            )
            if picked is None:
                self.status_var.set(
                    f"Status: raspakivanje .txt otkazano (obrađeno {total_txt} fajlova do tog koraka)."
                )
                return

            out_dir = bundle.parent / TXT_EXPORT_DIRNAME
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                messagebox.showerror("Greška", f"Ne mogu folder:\n{out_dir}\n\n{exc}")
                return

            used_base: dict[str, int] = {}
            written_here = 0
            for idx in picked:
                if idx < 0 or idx >= len(segments):
                    continue
                name, body = segments[idx]
                base = sanitize_filename(name) or "takmicar"
                n = used_base.get(base, 0) + 1
                used_base[base] = n
                fname = f"{base}.txt" if n == 1 else f"{base}_{n}.txt"
                dest = out_dir / fname
                normalized = body.replace("\r\n", "\n").replace("\r", "\n")
                try:
                    clear_export_file_readonly(dest)
                    dest.write_text(normalized, encoding="utf-8", newline="\n")
                    set_export_file_readonly(dest)
                    written_here += 1
                    total_txt += 1
                except Exception as exc:
                    messagebox.showerror("Greška", f"Ne mogu da sačuvam:\n{dest}\n\n{exc}")
                    self.status_var.set("Status: greška pri snimanju .txt.")
                    return

            opened_out_dirs.append(out_dir)

        seen: set[str] = set()
        for od in opened_out_dirs:
            key = str(od.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                if sys.platform == "win32":
                    os.startfile(str(od))  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(od)], start_new_session=True)
                else:
                    subprocess.Popen(["xdg-open", str(od)], start_new_session=True)
            except Exception:
                logging.exception("Neuspešno otvaranje foldera: %s", od)

        self.status_var.set(
            f"Status: ukupno sačuvano {total_txt} .txt u foldere „{TXT_EXPORT_DIRNAME}“ (više paketa obrađeno)."
        )

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    setup_logging()
    configure_https_certificates()
    _cleanup_obsolete_taurislock_local_secrets()
    if is_app_expired():
        _show_block_message(
            "Program je istekao i zaključan posle 01.05.2027. Kontaktirajte autora za novu verziju."
        )
        return 3
    if len(sys.argv) >= 4 and sys.argv[1].lower() == "verify":
        key = os.getenv("TAURIS_SIGNING_KEY", "").strip()
        if not key or key == "CHANGE_ME_SECRET":
            print(
                "Postavite TAURIS_SIGNING_KEY u okruženju na istu tajnu kao kod pošiljaoca programa "
                "(ne koristite CHANGE_ME_SECRET u produkciji)."
            )
            return 2
        ok, msg = verify_tauris_export(Path(sys.argv[2]), Path(sys.argv[3]), key)
        print(msg)
        return 0 if ok else 1

    delivery_config = load_delivery_config()
    app = TaurisApp(delivery_config)
    app.run()
    return 0
