#!/usr/bin/env python3
"""
531xx_gpib_tool.py  -  live GPIB/serial service tool for the Agilent/HP
                       53131A / 53132A / 53181A universal counters.

Scans the bus, finds the counter(s), and operates on the instrument directly:

  * Identify (*IDN?, *OPT?) and decode the installed Channel 3 option
  * Back up / restore the calibration data block (:CALibration:DATA)
  * Show / change the calibration security code, or help reset a lost one
  * Set the Channel 3 option/prescaler over the bus (:DIAGnostic:OPTion:HFR <prescale>,<Nxxx>,<coupling>)
  * Send raw SCPI and read the error queue

Transports, tried in this order (first that works is offered):
  1. PyVISA         - any VISA backend: Keysight IO Libs, NI-488.2/NI-VISA,
                      linux-gpib, or the pure-python 'pyvisa-py'.  Handles GPIB
                      cards/dongles and RS-232 (ASRL) resources.
        pip install pyvisa pyvisa-py     (pyvisa-py also needs pyserial/pyusb)
  2. Prologix       - the popular Prologix GPIB-USB adapter (a serial device).
        pip install pyserial
  3. Mock           - '--selftest' only; a fake counter for exercising the menus.

This tool sends real commands to real hardware.  The calibration and Channel 3
operations change stored data; read the prompts.  Nothing here can reset a
FORGOTTEN security code without erasing calibration - see the password menu.
"""

import sys, time, argparse

# --------------------------------------------------------------- decode help --
# *OPT? second field -> Channel 3 capability (option numbers are NNN/10 GHz)
CH3_OPT = {
    "015": "1.5 GHz  (Option 015 - Ch2 RF input, 53181A)",
    "030": "3.0 GHz  (Option 030 - Channel 3)",
    "050": "5.0 GHz  (Option 050 - Channel 3)",
    "124": "12.4 GHz (Option 124 - Channel 3)",
    "160": "16.0 GHz (Option 160 - Channel 3)",
    "200": "20.0 GHz (Option 200 - Channel 3)",
    "0":   "none installed",
    "":    "none installed",
}
# Channel 3 option -> :DIAG:OPT:HFREQuency enum keyword (firmware spells them N###)
OPT_ENUM = {"015": "N015", "030": "N030", "050": "N050",
            "124": "N124", "160": "N160", "200": "N200", "none": "NONE"}
DEFAULT_CODES = {"53131A": "53131", "53132A": "53132", "53181A": "53181"}

# Canonical prescaler divide ratio per Channel 3 option (EEPROM $1C = ratio/128).
#   030=128 and 050,124=512 are confirmed on hardware/dumps; 015 inferred (shares
#   the 1.5/3.0 assembly); 160/200 provisional. The option keyword ALSO fixes the
#   frequency ceiling, so the prescale and the option must describe one real board.
OPT_PRESCALE = {"015": 128, "030": 128, "050": 512, "124": 512, "160": 512, "200": 512}

def enum_to_opt(enum):
    """'N124' -> '124'; pass a bare '124' through."""
    e = (enum or "").upper()
    return e[1:] if e.startswith("N") else e

def opts_for_ratio(ratio):
    """Options whose canonical prescaler matches this divide ratio."""
    return [o for o in ("015", "030", "050", "124", "160", "200")
            if OPT_PRESCALE[o] == ratio]

def ask(p, d=None):
    try:
        s = input(p).strip()
    except EOFError:
        return d if d is not None else ""
    return s if s else (d if d is not None else "")

def confirm(p):
    return ask(p + " [y/N]: ").lower() in ("y", "yes")

# =============================================================== transports ====
class TransportError(Exception):
    pass

class VisaTransport:
    """PyVISA wrapper. One instance == the resource manager; open() per address."""
    name = "PyVISA"
    def __init__(self):
        import pyvisa
        self.pyvisa = pyvisa
        try:
            self.rm = pyvisa.ResourceManager()          # default backend
        except Exception:
            self.rm = pyvisa.ResourceManager("@py")     # pure-python fallback
        self.inst = None
    def list_addresses(self):
        out = []
        for r in self.rm.list_resources():
            if r.startswith("GPIB") or r.startswith("ASRL") or r.startswith("TCPIP"):
                out.append(r)
        return out
    def open(self, res):
        self.inst = self.rm.open_resource(res)
        self.inst.timeout = 4000
        try:
            self.inst.read_termination = "\n"
            self.inst.write_termination = "\n"
        except Exception:
            pass
        return self.inst
    def write(self, s):  self.inst.write(s)
    def write_raw(self, b): self.inst.write_raw(b)
    def query(self, s):  return self.inst.query(s).strip()
    def query_raw(self, s):
        self.inst.write(s)
        return self.inst.read_raw()
    def close(self):
        try:
            if self.inst: self.inst.close()
            self.rm.close()
        except Exception:
            pass

class PrologixTransport:
    """Prologix GPIB-USB (serial). Controller-in-charge, addressed per query."""
    name = "Prologix GPIB-USB"
    def __init__(self, port=None, baud=115200):
        import serial, serial.tools.list_ports as lp
        if port is None:
            cand = [p.device for p in lp.comports()
                    if "prolog" in (p.description or "").lower()
                    or "ttyUSB" in p.device or "ttyACM" in p.device or "usbserial" in p.device]
            if not cand:
                raise TransportError("No serial port found for a Prologix adapter; "
                                     "pass one with --port.")
            port = cand[0]
        self.port = port
        self.ser = serial.Serial(port, baud, timeout=3)
        time.sleep(0.1)
        for c in ("++mode 1", "++auto 0", "++eoi 1", "++eos 2", "++read_tmo_ms 1500"):
            self._send(c)
        self.addr = None
    def _send(self, s):
        self.ser.write((s + "\n").encode()); self.ser.flush()
    def set_addr(self, a):
        if a != self.addr:
            self._send("++addr %d" % a); self.addr = a
    def write(self, s):
        self._send(s)
    def write_raw(self, b):
        esc=bytearray()
        for byte in b:
            if byte in (13,10,27,43): esc.append(27)
            esc.append(byte)
        self.ser.write(bytes(esc)); self.ser.flush()
    def query(self, s):
        self._send(s); self._send("++read eoi")
        return self.ser.readline().decode(errors="replace").strip()
    def query_raw(self, s):
        self._send(s); self._send("++read eoi")
        # read until timeout; caller parses the 488.2 block
        buf = b""
        t = time.time()
        while time.time() - t < 3:
            chunk = self.ser.read(256)
            if not chunk: break
            buf += chunk
        return buf
    def list_addresses(self):
        return ["PROLOGIX:%d" % a for a in range(1, 31)]
    def open(self, res):
        self.set_addr(int(res.split(":")[1]))
        return self
    def close(self):
        try: self.ser.close()
        except Exception: pass

class MockTransport:
    """Fake 53132A for --selftest; supports the commands the menus use."""
    name = "Mock (self-test)"
    def __init__(self):
        self.secured = True
        self.code = "53132"
        self.opt = "0,030"
        self.count = 7
        self.errq = []
        self.caldata = b"#3016" + bytes(range(16))
    def list_addresses(self): return ["GPIB0::3::INSTR", "GPIB0::14::INSTR"]
    def open(self, res):
        self.addr = res; self.present = res.endswith("3::INSTR")
        return self
    def write(self, s):
        s = s.strip()
        u = s.upper()
        if u.startswith(":CAL:SEC:STAT OFF") or u.startswith(":CALIBRATION:SECURITY:STATE OFF"):
            code = s.split(",")[-1].strip()
            if code == self.code: self.secured = False
            else: self.errq.append('-222,"Data out of range;BAD CODE"')
        elif u.startswith(":CAL:SEC:CODE") or u.startswith(":CALIBRATION:SECURITY:CODE"):
            if self.secured: self.errq.append('-221,"Settings conflict;secured"')
            else: self.code = s.split(None, 1)[1].strip()
        elif ":DIAG:OPT:HFR" in u or ":DIAGNOSTIC:OPTION:HFR" in u:
            arg = s.split(None, 1)[1] if " " in s else ""
            for k in ("N015","N030","N050","N124","N160","N200"):
                if k in arg.upper(): self.opt = "0," + k[1:]
        elif u.startswith(":DIAG") or u.startswith("*"):
            pass
    def query(self, s):
        u = s.strip().upper().rstrip("?")
        if u in ("*IDN",):        return "HEWLETT-PACKARD,53132A,0,4613" if self.present else ""
        if u in ("*OPT",):        return self.opt
        if u in (":CAL:SEC:STAT", ":CALIBRATION:SECURITY:STATE"): return "1" if self.secured else "0"
        if u in (":CAL:COUN", ":CALIBRATION:COUNT"): return str(self.count)
        if u in (":SYST:ERR", ":SYSTEM:ERROR"):
            return self.errq.pop(0) if self.errq else '+0,"No error"'
        return ""
    def query_raw(self, s):
        if "DATA" in s.upper(): return self.caldata
        return b""
    def write_raw(self, b): pass
    def close(self): pass

class BridgeTransport:
    """Talks to instrument_bridge.py running on the bench PC (JSON-over-TCP)."""
    name = "Bridge (TCP)"
    def __init__(self, hostport):
        import socket
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 5555)), timeout=10)
        self.sock.settimeout(15)
        self.f = self.sock.makefile("rwb", buffering=0)
        self.res = None
    def _rpc(self, req):
        import json
        self.f.write((json.dumps(req) + "\n").encode())
        line = self.f.readline()
        if not line:
            raise TransportError("bridge closed the connection")
        r = json.loads(line.decode())
        if not r.get("ok"):
            raise TransportError(r.get("error", "bridge error"))
        return r
    def list_addresses(self):
        return [row[0] for row in self._rpc({"op": "list"})["result"]]
    def list_idn(self):
        return self._rpc({"op": "list"})["result"]
    def open(self, res):
        self.res = res; return self
    def write(self, s):
        self._rpc({"op": "write", "res": self.res, "cmd": s})
    def write_raw(self, b):
        import base64
        self._rpc({"op": "write_raw", "res": self.res, "b64": base64.b64encode(b).decode()})
    def query(self, s):
        return self._rpc({"op": "query", "res": self.res, "cmd": s})["result"]
    def query_raw(self, s):
        import base64
        return base64.b64decode(self._rpc({"op": "query_raw", "res": self.res, "cmd": s})["b64"])
    def close(self):
        try: self.sock.close()
        except Exception: pass

# =============================================================== instrument ====
class Counter:
    def __init__(self, transport):
        self.t = transport
    def q(self, s):   return self.t.query(s)
    def w(self, s):   self.t.write(s)
    def idn(self):    return self.q("*IDN?")
    def opt(self):    return self.q("*OPT?")
    _model=None
    def model(self):
        if self._model is None:
            try: self._model=self.idn().split(",")[1].strip()
            except Exception: self._model="?"
        return self._model
    def ch3(self):
        parts = [p.strip() for p in self.opt().split(",")]
        code = parts[-1] if parts else ""
        return code, CH3_OPT.get(code, "unknown (%s)" % code)
    def secured(self):  return self.q(":CAL:SEC:STAT?").startswith("1")
    def calcount(self): return self.q(":CAL:COUN?")
    def errors(self):
        out = []
        for _ in range(30):
            e = self.q(":SYST:ERR?")
            out.append(e)
            if e.startswith("+0") or e.startswith("0") or "No error" in e: break
        return out

# =============================================================== operations ====
def pick_transport(args):
    order = []
    if args.prologix:
        order = ["prologix"]
    elif args.visa:
        order = ["visa"]
    else:
        order = ["visa", "prologix"]
    errs = []
    for kind in order:
        try:
            if kind == "visa":
                return VisaTransport()
            if kind == "prologix":
                return PrologixTransport(port=args.port)
        except Exception as e:
            errs.append("  %-16s unavailable: %s" % (kind, e))
    print("No usable transport.\n" + "\n".join(errs))
    print("\nInstall one of:\n"
          "  pip install pyvisa pyvisa-py pyserial     (VISA / pure-python)\n"
          "  pip install pyserial                       (Prologix GPIB-USB)")
    return None

def scan(t):
    print("\n  Scanning %s ..." % t.name)
    found = []
    for res in t.list_addresses():
        try:
            t.open(res)
            idn = t.query("*IDN?")
        except Exception:
            idn = ""
        if idn and ("53131" in idn or "53132" in idn or "53181" in idn
                    or ("PACKARD" in idn.upper() and "5313" in idn) ):
            print("    %-22s %s" % (res, idn))
            found.append((res, idn))
        elif idn:
            print("    %-22s (%s)" % (res, idn.split(',')[0]))
    if not found:
        print("    No 531xx counter found.")
    return found

def read_block(raw):
    """Parse an IEEE-488.2 definite-length block '#<n><len><bytes>'."""
    i = raw.find(b"#")
    if i < 0: return raw
    n = int(chr(raw[i+1]))
    length = int(raw[i+2:i+2+n])
    start = i + 2 + n
    return raw[start:start+length]

def backup_cal(c, t):
    path = ask("  Save calibration block to file [cal_backup.blk]: ", "cal_backup.blk")
    raw = t.query_raw(":CAL:DATA?")
    data = read_block(raw)
    open(path, "wb").write(data)
    print("  Wrote %d bytes to %s." % (len(data), path))
    print("  (This is the instrument's calibration data - the meaningful backup")
    print("   over the bus. A full raw-chip image needs a programmer + the dump tool.)")

def restore_cal(c, t):
    path = ask("  Calibration block file to restore: ")
    try:
        data = open(path, "rb").read()
    except OSError as e:
        print("  ! %s" % e); return
    print("  WARNING: this overwrites the instrument's calibration data.")
    if not confirm("  Send %d bytes back with :CAL:DATA?" % len(data)):
        return
    n = len(data)
    block = (":CAL:DATA #%d%d" % (len(str(n)), n)).encode() + data + b"\n"
    try:
        t.write_raw(block)
    except Exception as e:
        print("  ! Binary write failed (%s)." % e); return
    errs = c.errors()
    print("  Sent %d bytes. error queue: %s" % (n, "; ".join(errs)))

def show_info(c):
    idn = c.idn()
    print("\n  *IDN? : %s" % idn)
    print("  *OPT? : %s" % c.opt())
    code, desc = c.ch3()
    print("  Channel 3 : %s   %s" % (code, desc))
    try:
        print("  Cal secured : %s     Cal count : %s"
              % ("YES" if c.secured() else "no", c.calcount()))
    except Exception as e:
        print("  Cal status  : (query failed: %s)" % e)

def measure_ch3_ratio(c, opt_enum):
    # Determine the fitted A3 board's true prescaler divide ratio empirically.
    # Physics: the board divides the input by a fixed hardware ratio N; the counter
    # multiplies the prescaled reading by whatever prescale P is stored, so
    #   displayed = F_in * P / N   ->   N = P * F_in / displayed.
    # With P and F_in known and the reading R observed, N falls out directly, and
    # N is a power of two (128..16384). Useful for the 16/20 GHz (160/200) boards
    # that were not on hand to confirm from a dump.
    # HFR args: <prescale>,<Nxxx>,<coupling flag>. arg3 = bit7 of $1B, a stored
    # coupling flag (echoed by HFR?); on a prescaler board it has no visible effect
    # (Channel 3 is AC-coupled, :INP3:COUP? = AC). Use 1 to match the factory setting.
    import math
    print("\n  --- Measure Channel 3 prescaler divide ratio ---")
    print("  You will need a signal source of accurately known frequency connected")
    print("  to Channel 3, inside the fitted board's frequency range.")
    enum = ask("  Option keyword to store while testing [N030]: ", "N030").upper()
    if enum not in opt_enum.values():
        print("  Cancelled (must be one of %s)." % ", ".join(opt_enum.values())); return
    P = 128  # trial prescale
    if confirm("  Enable service access first (:DIAG:SYST:HMACCESS 1)?"):
        c.w(":DIAG:SYST:HMACCESS 1")
    c.w(":DIAGnostic:OPTion:HFR %d,%s,1" % (P, enum))
    print("  Stored trial prescale P = %d (coupling flag 1)." % P)
    fin = ask("  Apply the known signal, then enter its frequency in Hz (e.g. 120e6): ")
    try:
        F = float(fin)
    except ValueError:
        print("  Cancelled (not a number)."); return
    c.w(':FUNC "FREQ 3"')
    try:
        R = float(c.q(":READ?"))
    except Exception as e:
        print("  Read failed: %s" % e); return
    if R <= 0:
        print("  Channel 3 read %s - check the signal level/frequency." % R); return
    n_raw = P * F / R
    n_pow2 = 2 ** round(math.log2(n_raw)) if n_raw > 0 else 0
    print("  Channel 3 read R = %.6g Hz with P = %d and F = %.6g Hz." % (R, P, F))
    print("  Raw ratio  N = P*F/R = %.3f" % n_raw)
    print("  Nearest power of two: N = %d" % n_pow2)
    if not (128 <= n_pow2 <= 16384):
        print("  WARNING: %d is outside the firmware range 128..16384 - re-check F and the"
              " connection." % n_pow2)
    # Guard: the option keyword sets the frequency ceiling as well as the *OPT?
    # string, so the measured ratio and the option must describe ONE real board.
    # Storing the right ratio against the wrong (old) keyword leaves that option's
    # ceiling in place; inputs above it read over-range (9.9E37) and Channel 3
    # then returns no valid reading (-230 "Data corrupt or stale", display dashes).
    store_enum = enum
    opt = enum_to_opt(enum)
    expected = OPT_PRESCALE.get(opt)
    if expected is not None and n_pow2 != expected and 128 <= n_pow2 <= 16384:
        cand = opts_for_ratio(n_pow2)
        print("\n  ! The measured ratio (/%d) does NOT match option %s, which uses /%d."
              % (n_pow2, opt, expected))
        print("    The option keyword also sets the frequency ceiling, so storing /%d"
              % n_pow2)
        print("    against %s would keep option %s's ceiling: inputs above it read as"
              % (enum, opt))
        print("    over-range (9.9E37) and Channel 3 returns no valid reading")
        print("    (-230 'Data corrupt or stale', display all dashes) until corrected.")
        if cand:
            print("    A /%d prescaler matches - pick the one that matches your board's" % n_pow2)
            print("    top frequency:")
            for o in cand:
                print("        %s  %s" % (o, CH3_OPT[o]))
        new = ask("  Option to store the ratio against (e.g. %s), blank = keep %s: "
                  % ("/".join(cand) if cand else "N124", opt))
        if new:
            ne = "N" + enum_to_opt(new).zfill(3)
            if ne in opt_enum.values():
                store_enum = ne
            else:
                print("  '%s' not recognised - keeping %s (likely will not read above its"
                      " ceiling)." % (new, enum))
    if confirm("  Store prescale %d now (:DIAG:OPT:HFR %d,%s,1)?"
               % (n_pow2, n_pow2, store_enum)):
        c.w(":DIAGnostic:OPTion:HFR %d,%s,1" % (n_pow2, store_enum))
        try:
            R2 = float(c.q(":READ?"))
            print("  Channel 3 now reads %.6g Hz (input is %.6g Hz)." % (R2, F))
            print("  *OPT? now reports: %s" % c.opt())
        except Exception:
            pass
    else:
        print("  Left trial value P=%d stored; re-run to set the final ratio." % P)


def change_ch3(c):
    # VERIFIED ON HARDWARE (53131A rev 4243) + three module EEPROM dumps:
    #   :DIAGnostic:OPTion:HFR <prescale>,<Nxxx option>,<coupling>
    #   - short form is HFR (not HFREQ); THREE arguments.
    #   - arg1 = PRESCALER DIVIDE RATIO: the counter multiplies the prescaled
    #     reading by it. Stored in EEPROM as byte $1C = prescale/128.
    #   - arg2 = option keyword Nxxx -> $1B option code -> *OPT? string.
    #   - arg3 = stored coupling FLAG (bit7 of $1B), echoed by HFR? but with no
    #     visible effect on a prescaler board: Channel 3 is fixed AC-coupled and
    #     :INP3:COUP? is query-only. Use 1 to match the factory setting.
    #   :DIAG:OPT:HFR? reads the set back (030 unit returns 128,N030,1).
    # Prescale ratios (all now confirmed except 160/200):
    #   030 = 128  (measured; $1C=$01)   050 = 512  (dump+hw; $1C=$04)
    #   124 = 512  (dump+hw; $1C=$04)    015 = 128  (inferred, shares 1.5/3.0 assy)
    #   160/200 = 512+ (53181A, untested - read $1C from a dump or measure).
    PRESCALE = OPT_PRESCALE
    PCONF    = {"015": "infer", "030": "CONF", "050": "CONF", "124": "CONF",
                "160": "untst", "200": "untst"}
    code, desc = c.ch3()
    print("\n  Current Channel 3 option: %s (%s)" % (code, desc))
    print("  Sets :DIAG:OPT:HFR <prescale>,<Nxxx>,<coupling>.  arg1 is the board's")
    print("  divide ratio (stored as EEPROM $1C = prescale/128); arg3 is a stored coupling flag.")
    print("  030=128, 050/124=512 are confirmed; 'm' measures a board (e.g. 160/200).")
    print("  All three HFR arguments are required; the command is refused if any is missing.")
    opts = ["015", "030", "050", "124", "160", "200"]
    for i, o in enumerate(opts, 1):
        print("    %d) %s  %-30s prescale=%-5d [%s]" % (i, o, CH3_OPT[o], PRESCALE[o], PCONF[o]))
    print("    m) Measure this board's divide ratio (feed a known frequency, compute N)")
    sel = ask("  Option number, 'm' to measure (blank = cancel): ")
    if sel.strip().lower() == "m":
        measure_ch3_ratio(c, OPT_ENUM); return
    if not (sel.isdigit() and 1 <= int(sel) <= len(opts)):
        print("  Cancelled."); return
    opt = opts[int(sel) - 1]; enum = OPT_ENUM[opt]
    dflt = PRESCALE[opt]
    pr = ask("  Prescaler divide ratio [%s]: " % (dflt if dflt else "enter 128-16384"),
             str(dflt) if dflt else "")
    if not pr.isdigit() or not (128 <= int(pr) <= 16384):
        print("  Cancelled (prescale must be 128-16384)."); return
    if int(pr) != PRESCALE[opt]:
        print("  Note: /%s differs from option %s's usual /%d. The option keyword sets the"
              % (pr, opt, PRESCALE[opt]))
        print("  frequency ceiling too, so make sure %s matches the fitted board - a mismatched"
              % opt)
        print("  pair reads over-range above the ceiling (-230 'Data corrupt or stale', dashes).")
        if not confirm("  Proceed with /%s on option %s anyway?" % (pr, opt)):
            print("  Cancelled."); return
    cp = ask("  Coupling flag 0 or 1 [1] (stored; 1 = factory setting): ", "1")
    if cp not in ("0", "1"):
        print("  Cancelled (coupling must be 0 or 1)."); return
    if confirm("  Enable service access first (:DIAG:SYST:HMACCESS 1)?"):
        c.w(":DIAG:SYST:HMACCESS 1")
    cmd = ":DIAGnostic:OPTion:HFR %s,%s,%s" % (pr, enum, cp)
    print("  -> %s" % cmd)
    c.w(cmd)
    errs = c.errors()
    got, gdesc = c.ch3()
    try:
        print("  :DIAG:OPT:HFR? now: %s" % c.q(":DIAG:OPT:HFR?"))
    except Exception:
        pass
    if errs and not errs[0].startswith(("+0", "0")):
        print("  error queue: %s" % "; ".join(errs))
    print("  *OPT? now reports: %s (%s)" % (got, gdesc))
    print("  Verify with a known Channel 3 signal before trusting the reading.")

def password_menu(c):
    model = c.model()
    default = DEFAULT_CODES.get(model, "the model number")
    print("\n  --- Calibration security code (%s) ---" % model)
    print("  Secured now: %s" % ("YES" if c.secured() else "no"))
    print("""
  If you KNOW the code you can unsecure and change it here.
  If it is FORGOTTEN, it cannot be reset over the bus without erasing
  calibration.  The only resets are:
    - read it on the instrument: hold '+/-' at power-on (enable the menu first
      with :DIAG:SYST:HMACCESS if needed), select 'CAL CODE ?';
    - clear all NVRAM: hold LEFT+RIGHT arrows at power-on. Code returns to the
      default (%s) but ALL calibration is erased.
""" % default)
    print("  1) Unsecure with a known code")
    print("  2) Change the code (must be unsecured first)")
    print("  3) Back")
    ch = ask("  Choice: ")
    if ch == "1":
        code = ask("  Present code: ")
        c.w(":CAL:SEC:STAT OFF,%s" % code)
        errs = c.errors()
        print("  Secured now: %s" % ("YES" if c.secured() else "no"))
        if errs and not errs[0].startswith(("+0", "0")):
            print("  error queue: %s" % "; ".join(errs))
    elif ch == "2":
        if c.secured():
            print("  ! Unsecure first (option 1)."); return
        new = ask("  New numeric code: ")
        c.w(":CAL:SEC:CODE %s" % new)
        errs = c.errors()
        print("  Sent. error queue: %s" % "; ".join(errs))

def raw_menu(c):
    print("\n  Raw SCPI - type a command; add '?' for a query. Blank to exit.")
    print("  Examples:  *OPT?    :DIAG:SYST:HMACCESS 1    :DIAG:SYST:NVCOUNT?")
    while True:
        cmd = ask("  scpi> ")
        if not cmd: break
        try:
            if cmd.rstrip().endswith("?"):
                print("     " + c.q(cmd))
            else:
                c.w(cmd); print("     (sent)")
        except Exception as e:
            print("     ! %s" % e)

# =============================================================== main ==========
MENU = """
========= 531xx GPIB Service Tool  -  %s @ %s =========
  1) Rescan / choose instrument
  2) Show info (IDN, OPT, Channel 3, cal status)
  3) Back up calibration data (:CAL:DATA?)
  4) Restore calibration data (:CAL:DATA)
  5) Change Channel 3 option (experimental)
  6) Calibration security code
  7) Send raw SCPI
  8) Read error queue (:SYST:ERR?)
  9) Quit
"""

def choose_instrument(t):
    found = scan(t)
    if not found:
        return None, None
    if len(found) == 1:
        res = found[0][0]
    else:
        for i, (r, idn) in enumerate(found, 1):
            print("    %d) %s  %s" % (i, r, idn))
        s = ask("  Choose instrument #: ", "1")
        res = found[int(s) - 1][0] if s.isdigit() and 1 <= int(s) <= len(found) else found[0][0]
    t.open(res)
    return Counter(t), res

def main():
    ap = argparse.ArgumentParser(description="Live GPIB service tool for 53131A/132A/181A")
    ap.add_argument("--visa", action="store_true", help="force PyVISA transport")
    ap.add_argument("--prologix", action="store_true", help="force Prologix GPIB-USB")
    ap.add_argument("--bridge", help="host:port of instrument_bridge.py on the bench PC")
    ap.add_argument("--port", help="serial port for Prologix (e.g. COM4, /dev/ttyUSB0)")
    ap.add_argument("--selftest", action="store_true", help="run against a mock instrument")
    args = ap.parse_args()

    if args.selftest:
        t = MockTransport()
    elif args.bridge:
        t = BridgeTransport(args.bridge)
    else:
        t = pick_transport(args)
    if t is None:
        return 1
    print("Transport: %s" % t.name)

    c, res = choose_instrument(t)
    if c is None:
        print("No instrument selected. Exiting."); t.close(); return 1

    actions = {"2": lambda: show_info(c), "3": lambda: backup_cal(c, t),
               "4": lambda: restore_cal(c, t), "5": lambda: change_ch3(c),
               "6": lambda: password_menu(c), "7": lambda: raw_menu(c),
               "8": lambda: print("  " + "\n  ".join(c.errors()))}
    while True:
        print(MENU % (c.model(), res))
        ch = ask("  Choice: ")
        if ch in ("9", "q", "quit", "exit"):
            break
        if ch == "1":
            c2, res2 = choose_instrument(t)
            if c2: c, res = c2, res2
            continue
        act = actions.get(ch)
        if not act:
            print("  ? Unknown choice."); continue
        try:
            act()
        except (KeyboardInterrupt, EOFError):
            print("\n  (cancelled)")
        except Exception as e:
            print("  ! Error: %s" % e)
    t.close()
    print("  Bye.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
