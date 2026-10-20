#!/usr/bin/env python3
"""NordVPN for SteamOS: a GTK4 front end over NetworkManager's OpenVPN plugin.

Uses only what ships with SteamOS (Python, GTK4, nmcli, NetworkManager-openvpn),
so it lives in $HOME and survives OS updates. Server configs come straight from
NordVPN's API/CDN; the password is handed to nmcli at connect time through a
0600 temp file instead of being stored in NetworkManager, so no keyring or
secret agent is needed (important in Game Mode).
"""
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

__version__ = "1.0.0"

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

APP_ID = "io.github.hmmnd0.NordVPNGui"
CONN_PREFIX = "NordVPN GUI"
API = "https://api.nordvpn.com/v1"
OVPN_URL = "https://downloads.nordcdn.com/configs/files/ovpn_{proto}/servers/{host}.{proto}.ovpn"
HEADERS = {"User-Agent": f"nordvpn-gui-steamos/{__version__}"}
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "nordvpn-gui")
CACHE_DIR = os.path.join(GLib.get_user_cache_dir(), "nordvpn-gui")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CERT_DIR = os.path.expanduser("~/.cert/nm-openvpn")


class VpnError(Exception):
    pass


# ---------------------------------------------------------------- config

def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------- NordVPN API

def http_get(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def api_get(path, params=None):
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        return json.loads(http_get(url))
    except OSError as e:
        raise VpnError(f"Couldn't reach the NordVPN API: {e}") from e


def fetch_countries():
    countries = api_get("servers/countries")
    return sorted(
        ({"id": c["id"], "name": c["name"], "code": c["code"], "servers": c["serverCount"]}
         for c in countries if c.get("serverCount")),
        key=lambda c: c["name"],
    )


def recommended_server(proto, country_id=None):
    params = {"limit": 1, "filters[servers_technologies][identifier]": f"openvpn_{proto}"}
    if country_id:
        params["filters[country_id]"] = country_id
    servers = api_get("servers/recommendations", params)
    if not servers:
        raise VpnError("NordVPN has no OpenVPN servers available for that location right now.")
    s = servers[0]
    country = s["locations"][0]["country"]
    return {
        "hostname": s["hostname"],
        "name": s["name"],
        "load": s["load"],
        "country": country["name"],
        "city": country.get("city", {}).get("name", ""),
    }


def ip_insights():
    return api_get("helpers/ips/insights")


# ---------------------------------------------------------------- NetworkManager

def nmcli(*args, timeout=30):
    try:
        p = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise VpnError("NetworkManager didn't respond in time.") from e
    if p.returncode != 0:
        raise VpnError((p.stderr or p.stdout).strip() or f"nmcli {args[0]} failed")
    return p.stdout


def managed_connections():
    """(uuid, name) of every connection this app created."""
    out = nmcli("-g", "UUID,TYPE,NAME", "connection", "show")
    conns = []
    for line in out.splitlines():
        uuid, ctype, name = line.split(":", 2)
        if ctype == "vpn" and name.startswith(CONN_PREFIX):
            conns.append((uuid, name))
    return conns


def remove_connection(uuid):
    # Imported certs are written to ~/.cert/nm-openvpn and never cleaned up by
    # NetworkManager, so delete the ones this connection references.
    try:
        data = nmcli("-g", "vpn.data", "connection", "show", uuid)
    except VpnError:
        data = ""
    nmcli("connection", "delete", uuid)
    for path in re.findall(r"(/[^,\s]+\.pem)", data):
        if os.path.dirname(path) == CERT_DIR:
            try:
                os.unlink(path)
            except OSError:
                pass


def active_vpns():
    """(state, name, uuid) of every active VPN connection, this app's first.

    Connections made elsewhere (Plasma's network applet, TunnelDeck) count too:
    two tunnels at once would fight over routes, so we show and replace them.
    """
    out = nmcli("-g", "UUID,TYPE,STATE,NAME", "connection", "show", "--active")
    vpns = []
    for line in out.splitlines():
        uuid, ctype, state, name = line.split(":", 3)
        if ctype == "vpn":
            vpns.append((state, name, uuid))
    return sorted(vpns, key=lambda v: not v[1].startswith(CONN_PREFIX))


def active_vpn():
    vpns = active_vpns()
    return vpns[0] if vpns else None


def create_connection(host, proto, username):
    for uuid, _ in managed_connections():
        remove_connection(uuid)
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{host}.{proto}.ovpn")
    try:
        data = http_get(OVPN_URL.format(proto=proto, host=host))
    except OSError as e:
        raise VpnError(f"Couldn't download the config for {host}: {e}") from e
    with open(path, "wb") as f:
        f.write(data)
    try:
        out = nmcli("connection", "import", "type", "openvpn", "file", path)
    finally:
        os.unlink(path)
    m = re.search(r"\(([0-9a-f-]{36})\)", out)
    if not m:
        raise VpnError(f"Unexpected nmcli import output: {out.strip()}")
    uuid = m.group(1)
    # password-flags=1 (agent-owned): NetworkManager never stores the password;
    # nmcli supplies it from passwd-file at activation.
    nmcli("connection", "modify", uuid,
          "connection.id", f"{CONN_PREFIX} {host.split('.')[0]}",
          "connection.autoconnect", "no",
          "+vpn.data", f"username={username}",
          "+vpn.data", "password-flags=1")
    return uuid


def openvpn_log(since):
    try:
        return subprocess.run(
            ["journalctl", "-b", "-t", "nm-openvpn", "--since", f"@{int(since)}", "-o", "cat", "--no-pager"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def diagnose_failure(log):
    """Turn OpenVPN log lines into something a person can act on."""
    if "AUTH_FAILED" in log:
        return ("NordVPN rejected the username/password.\n\n"
                "Use your service credentials (Nord Account → NordVPN → Manual setup), "
                "not your email and account password.")
    if "TLS key negotiation failed" in log or "TLS handshake failed" in log:
        return ("The server never answered. Your network may be blocking VPN traffic — "
                "try switching the protocol to TCP.")
    if "Cannot resolve host" in log or "RESOLVE" in log:
        return "Couldn't look up the server's address. Check your internet connection."
    return None


def activate(uuid, password):
    os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
    fd, pwfile = tempfile.mkstemp(dir=CACHE_DIR)  # created 0600
    started = time.time() - 1
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"vpn.secrets.password:{password}\n")
        proc = subprocess.Popen(
            ["nmcli", "--wait", "60", "connection", "up", uuid, "passwd-file", pwfile],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # OpenVPN keeps retrying a rejected login until NetworkManager's 60s
        # timeout, so watch its log and give up as soon as the server says no.
        error = None
        while proc.poll() is None:
            time.sleep(1)
            if "AUTH_FAILED" in openvpn_log(started):
                proc.terminate()
                error = "auth"
                break
        out, err = proc.communicate()
        if error or proc.returncode != 0:
            try:
                nmcli("connection", "down", uuid)
            except VpnError:
                pass
            raise VpnError(diagnose_failure(openvpn_log(started))
                           or (err or out).strip() or "The VPN connection failed.")
    finally:
        os.unlink(pwfile)


def deactivate():
    for _state, _name, uuid in active_vpns():
        nmcli("connection", "down", uuid)


# ---------------------------------------------------------------- UI

CSS = b"""
.status-title { font-size: 30px; font-weight: 800; }
.status-detail { font-size: 15px; opacity: 0.85; }
.state-connected { color: #33d17a; }
.state-connecting { color: #f6d32d; }
.state-disconnected { color: #ed333b; }
button.big { min-height: 52px; padding: 0 28px; font-size: 17px; font-weight: 700; }
button.big.suggested-action { background: #4687ff; color: white; }
button.big.destructive-action { background: #c01c28; color: white; }
button.big:disabled { background: alpha(currentColor, 0.08); color: alpha(currentColor, 0.4); }
.country-row { min-height: 52px; padding: 0 12px; }
.country-name { font-size: 16px; }
.server-count { opacity: 0.6; }
.card { padding: 20px; border-radius: 14px; background: alpha(currentColor, 0.06); }
"""


class CredentialsWindow(Gtk.Window):
    def __init__(self, parent, cfg, on_saved):
        super().__init__(title="NordVPN service credentials", transient_for=parent,
                         modal=True, default_width=520)
        self.cfg, self.on_saved = cfg, on_saved
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_top=20, margin_bottom=20, margin_start=20, margin_end=20)
        hint = Gtk.Label(wrap=True, xalign=0, use_markup=True, label=(
            "These are <b>not</b> your email and account password. Get them from "
            "<b>my.nordaccount.com → NordVPN → Manual setup → Service credentials</b>."))
        box.append(hint)
        self.user = Gtk.Entry(placeholder_text="Service username", text=cfg.get("username", ""))
        self.pw = Gtk.PasswordEntry(placeholder_text="Service password", show_peek_icon=True)
        self.pw.set_text(cfg.get("password", ""))
        self.pw.connect("activate", self.save)
        box.append(self.user)
        box.append(self.pw)
        note = Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label"], label=(
            f"Saved only on this Deck, readable by you alone ({CONFIG_FILE})."))
        box.append(note)
        save = Gtk.Button(label="Save", css_classes=["suggested-action", "big"], halign=Gtk.Align.END)
        save.connect("clicked", self.save)
        box.append(save)
        self.set_child(box)

    def save(self, *_):
        user, pw = self.user.get_text().strip(), self.pw.get_text()
        if not user or not pw:
            return
        self.cfg["username"], self.cfg["password"] = user, pw
        save_config(self.cfg)
        self.close()
        self.on_saved()


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="NordVPN", default_width=1100, default_height=720)
        self.cfg = load_config()
        self.busy = False
        self.polling = False
        self.last_state = None
        self.pending_connect = None
        self.countries = []

        header = Gtk.HeaderBar()
        creds_btn = Gtk.Button(icon_name="dialog-password-symbolic", tooltip_text="Service credentials")
        creds_btn.connect("clicked", lambda *_: self.open_credentials())
        header.pack_end(creds_btn)
        self.set_titlebar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                       margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)

        # status card
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card"])
        top = Gtk.Box(spacing=12)
        self.spinner = Gtk.Spinner()
        self.status_title = Gtk.Label(xalign=0, css_classes=["status-title", "state-disconnected"],
                                      label="Not connected")
        top.append(self.status_title)
        top.append(self.spinner)
        card.append(top)
        self.status_detail = Gtk.Label(xalign=0, wrap=True, css_classes=["status-detail"], label="")
        self.ip_label = Gtk.Label(xalign=0, wrap=True, css_classes=["status-detail"], label="")
        card.append(self.status_detail)
        card.append(self.ip_label)

        controls = Gtk.Box(spacing=12, margin_top=10)
        self.quick_btn = Gtk.Button(label="Quick Connect", css_classes=["suggested-action", "big"])
        self.quick_btn.connect("clicked", lambda *_: self.connect_to(None))
        self.disc_btn = Gtk.Button(label="Disconnect", css_classes=["destructive-action", "big"])
        self.disc_btn.connect("clicked", self.on_disconnect)
        self.proto = Gtk.DropDown.new_from_strings(["UDP", "TCP"])
        self.proto.set_selected(1 if self.cfg.get("protocol") == "tcp" else 0)
        self.proto.set_valign(Gtk.Align.CENTER)
        self.proto.connect("notify::selected", self.on_proto_changed)
        controls.append(self.quick_btn)
        controls.append(self.disc_btn)
        spacer = Gtk.Box(hexpand=True)
        controls.append(spacer)
        controls.append(Gtk.Label(label="Protocol"))
        controls.append(self.proto)
        card.append(controls)
        root.append(card)

        # country list
        self.search = Gtk.SearchEntry(placeholder_text="Search countries")
        self.search.connect("search-changed", lambda *_: self.country_list.invalidate_filter())
        root.append(self.search)
        self.country_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE,
                                        css_classes=["boxed-list"])
        self.country_list.set_filter_func(self.filter_row)
        self.country_list.connect("row-activated", self.on_country_activated)
        scroller = Gtk.ScrolledWindow(vexpand=True, child=self.country_list)
        root.append(scroller)

        self.set_child(root)
        self.refresh_buttons()
        self.load_countries()
        self.refresh_ip()
        self.poll()
        GLib.timeout_add_seconds(2, self.poll)

    # -- helpers

    def run_async(self, fn, on_done=None, on_error=None):
        def worker():
            try:
                res = fn()
            except Exception as e:  # report every failure in the UI rather than dying silently
                # Bind e now: Python unbinds it when the except block ends,
                # before the idle callback runs.
                GLib.idle_add(lambda err=e: (on_error or self.show_error)(err) and False)
                return
            if on_done:
                GLib.idle_add(lambda: on_done(res) and False)
        threading.Thread(target=worker, daemon=True).start()

    def show_error(self, err):
        self.set_busy(False)
        Gtk.AlertDialog(message="Couldn't complete that", detail=str(err)).show(self)

    def set_busy(self, busy):
        self.busy = busy
        self.spinner.set_spinning(busy)
        self.refresh_buttons()

    def refresh_buttons(self):
        connected = self.last_state is not None
        self.quick_btn.set_sensitive(not self.busy)
        self.country_list.set_sensitive(not self.busy)
        self.disc_btn.set_sensitive(connected or self.busy)

    def protocol(self):
        return "tcp" if self.proto.get_selected() == 1 else "udp"

    def set_status(self, title, cls, detail=None):
        self.status_title.set_label(title)
        for c in ("state-connected", "state-connecting", "state-disconnected"):
            self.status_title.remove_css_class(c)
        self.status_title.add_css_class(cls)
        if detail is not None:
            self.status_detail.set_label(detail)

    # -- countries

    def load_countries(self):
        def done(countries):
            self.countries = countries
            for c in countries:
                row = Gtk.ListBoxRow(css_classes=["country-row"])
                row.country = c
                box = Gtk.Box(spacing=12)
                box.append(Gtk.Label(label=c["name"], xalign=0, hexpand=True, css_classes=["country-name"]))
                box.append(Gtk.Label(label=f"{c['servers']} servers", css_classes=["server-count"]))
                row.set_child(box)
                self.country_list.append(row)
        self.run_async(fetch_countries, done)

    def filter_row(self, row):
        q = self.search.get_text().strip().lower()
        return not q or q in row.country["name"].lower() or q == row.country["code"].lower()

    def on_country_activated(self, _list, row):
        self.connect_to(row.country)

    # -- connect / disconnect

    def open_credentials(self, then=None):
        CredentialsWindow(self, self.cfg, then or (lambda: None)).present()

    def on_proto_changed(self, *_):
        self.cfg["protocol"] = self.protocol()
        save_config(self.cfg)

    def connect_to(self, country):
        if self.busy:
            return
        if not self.cfg.get("username") or not self.cfg.get("password"):
            self.open_credentials(then=lambda: self.connect_to(country))
            return
        proto, user, pw = self.protocol(), self.cfg["username"], self.cfg["password"]
        where = country["name"] if country else "the fastest server"
        self.set_busy(True)
        self.set_status("Connecting…", "state-connecting", f"Finding {where} ({proto.upper()})")
        self.ip_label.set_label("")

        def work():
            deactivate()
            server = recommended_server(proto, country["id"] if country else None)
            GLib.idle_add(lambda: self.status_detail.set_label(
                f"{server['name']} · {server['city']}, {server['country']} · load {server['load']}%") and False)
            uuid = create_connection(server["hostname"], proto, user)
            activate(uuid, pw)
            return server

        def done(server):
            self.cfg["last_server"] = server
            save_config(self.cfg)
            self.set_busy(False)
            self.poll()

        def failed(err):
            self.set_status("Not connected", "state-disconnected", "")
            self.show_error(err)
            self.refresh_ip()

        self.run_async(work, done, failed)

    def on_disconnect(self, *_):
        self.set_busy(True)
        self.set_status("Disconnecting…", "state-connecting")

        def done(_):
            self.set_busy(False)
            self.poll()
        self.run_async(deactivate, done)

    # -- status

    def poll(self):
        if self.polling or self.busy:
            return True
        self.polling = True

        def done(state):
            self.polling = False
            was = self.last_state
            self.last_state = state
            if not self.busy:
                if state is None:
                    self.set_status("Not connected", "state-disconnected", "Your traffic is not protected.")
                elif state[0] == "activated" and not state[1].startswith(CONN_PREFIX):
                    self.set_status("Connected", "state-connected",
                                    f"Through “{state[1]}”, set up outside this app.")
                elif state[0] == "activated":
                    srv = self.cfg.get("last_server") or {}
                    host = state[1][len(CONN_PREFIX):].strip()
                    detail = (f"{srv['name']} · {srv['city']}, {srv['country']}"
                              if srv.get("hostname", "").startswith(host) else host)
                    self.set_status("Connected", "state-connected", detail)
                else:
                    self.set_status("Connecting…", "state-connecting")
                self.refresh_buttons()
            if (was is None) != (state is None) or (state and was and state[0] != was[0]):
                self.refresh_ip()

        def failed(_err):
            self.polling = False
        self.run_async(active_vpn, done, failed)
        return True

    def refresh_ip(self):
        def done(info):
            protected = "🔒 protected" if info.get("protected") else "⚠ not protected"
            place = ", ".join(p for p in (info.get("city"), info.get("country")) if p)
            self.ip_label.set_label(f"Public IP {info.get('ip', '?')} · {place} · {protected}")
        self.run_async(ip_insights, done, lambda _e: self.ip_label.set_label(""))


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            Gtk.Settings.get_default().set_property("gtk-application-prefer-dark-theme", True)
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            win = MainWindow(self)
        win.present()


if __name__ == "__main__":
    App().run()
