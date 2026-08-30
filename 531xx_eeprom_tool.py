#!/usr/bin/env python3
"""
531xx_eeprom_tool.py  -  NVRAM helper for Agilent/HP 53131A / 53132A / 53181A counters

Works on an 8192-byte binary DUMP of the instrument's AT28C64B (or equivalent
8Kx8) EEPROM at CPU address $400000.  Read the chip with any programmer to make
the dump, edit it with this tool, then write it back.

  * Backup / restore whole-EEPROM dumps
  * Show and change the Channel 3 prescaler option
  * Reset (factory-clear) the calibration security code   -- see the warning

When changing the option, if the validity signature ("HP53131 is a winner!") is
missing -- as on a unit that never shipped with the optional channel, whose
32-byte header is blank -- the tool rebuilds a complete, firmware-valid header
(signature, $16 config word, $1A status and the header checksum) around your
chosen option, leaving the calibration records at $20 onward untouched. This
mirrors what the instrument's own firmware writes when the option is set live
over GPIB (:DIAGnostic:OPTion:HFR), so the two routes produce the same header.

Nothing here talks to the instrument; it only edits dump files.  Stdlib only,
Python 3.6+.  Layout and encodings were recovered by disassembly of the counter
firmware (revs 3427-4613); see the accompanying Firmware Supplement.

USE AT YOUR OWN RISK.  Always keep the original dump.  A bad write to this
EEPROM can lose the instrument's calibration.
"""

import os, sys, struct, shutil, datetime

EE_SIZE      = 8192                       # AT28C64B = 8K x 8
SIGNATURE    = b"HP53131 is a winner!"    # firmware validity string, stored at +2
SIG_OFF      = 0x02
CKSUM_OFF    = 0x00                       # BE 16-bit word
HDR_LEN      = 0x20                       # checksummed header record = first 32 bytes
CH3_STATUS   = 0x1A                       # status / revision byte
CH3_OPTBYTE  = 0x1B                       # bits 0-6 option code, bit 7 coupling
CH3_VALUE    = 0x1C                       # prescaler ratio / 128 (boot uses $1C*128)
CH3_CFG32    = 0x16                       # 32-bit config value (BE)

# Fixed (non-option) header field values used when rebuilding a blank header.
# The firmware compares the $16 config word at read; every factory option dump
# examined (53181A 1.5/3.0 GHz, and the 53131A example in the supplement) stores
# the same constant 0x00000A3B (= 2619, the value :DIAG:OPT:CODE? reports), so it
# is a fixed record marker, not an option-specific field.
CH3_CFG_DEFAULT    = 0x00000A3B
CH3_STATUS_DEFAULT = 0x01

# --- Channel 3 option encoding (EEPROM $1B low 7 bits -> *OPT? string) ---------
#     recovered from the boot reader jump table + the *OPT? formatter, and
#     cross-checked against real 3/5/12.4 GHz module dumps.
CODE_TO_OPT = {0: "015", 1: "030", 2: "050", 3: "124", 4: "160", 5: "200", 6: "030"}
# canonical value to WRITE for each option (030 has two encodings; use 1)
OPT_TO_CODE = {"015": 0, "030": 1, "050": 2, "124": 3, "160": 4, "200": 5}
# prescaler divide ratio per option, stored as $1C = ratio/128.
#   030=128 ($1C=1) measured; 050/124=512 ($1C=4) from dumps+hardware;
#   015=128 inferred; 160/200 untested (512+ placeholder - verify per board).
OPT_TO_PRESCALE = {"015": 128, "030": 128, "050": 512, "124": 512, "160": 512, "200": 512}
OPT_DESC = {
    "015": "1.5 GHz  (Option 015 - Ch2 RF input on 53181A)",
    "030": "3.0 GHz  (Option 030 - Channel 3)",
    "050": "5.0 GHz  (Option 050 - Channel 3)",
    "124": "12.4 GHz (Option 124 - Channel 3)",
    "160": "16.0 GHz (Option 160 - Channel 3)",
    "200": "20.0 GHz (Option 200 - Channel 3)",
}
DEFAULT_CODES = {"53131A": "53131", "53132A": "53132", "53181A": "53181"}

# ------------------------------------------------------------------ helpers ----
def checksum(buf):
    """Header checksum the firmware verifies: sum of bytes $02..$1F, low 16 bits."""
    return sum(buf[SIG_OFF:HDR_LEN]) & 0xFFFF

def hdr_valid(buf):
    sig_ok = bytes(buf[SIG_OFF:SIG_OFF + len(SIGNATURE)]) == SIGNATURE
    cs_ok  = struct.unpack(">H", bytes(buf[CKSUM_OFF:CKSUM_OFF + 2]))[0] == checksum(buf)
    return sig_ok, cs_ok

def signature_present(buf):
    return bytes(buf[SIG_OFF:SIG_OFF + len(SIGNATURE)]) == SIGNATURE

def rebuild_blank_header_fields(buf):
    """Fill the fixed, non-option header fields for a blank ($FF) header so that
    writing the signature, option bytes and checksum yields a complete,
    firmware-valid header. Only touches fields that are blank/uninitialised, so a
    header that already carries real values is left alone. Leaves the record data
    at $20 onward (the $5313 calibration slots) untouched."""
    cfg = struct.unpack(">I", bytes(buf[CH3_CFG32:CH3_CFG32 + 4]))[0]
    if cfg in (0xFFFFFFFF, 0x00000000):
        struct.pack_into(">I", buf, CH3_CFG32, CH3_CFG_DEFAULT)
    if buf[CH3_STATUS] in (0x00, 0xFF):
        buf[CH3_STATUS] = CH3_STATUS_DEFAULT

def fix_header(buf):
    """Write the validity signature and recompute the header checksum."""
    buf[SIG_OFF:SIG_OFF + len(SIGNATURE)] = SIGNATURE
    struct.pack_into(">H", buf, CKSUM_OFF, checksum(buf))

def decode_ch3(buf):
    ob   = buf[CH3_OPTBYTE]
    code = ob & 0x7F
    opt  = CODE_TO_OPT.get(code, "030")
    coup = "AC" if (ob & 0x80) else "DC"
    val  = buf[CH3_VALUE]
    cfg  = struct.unpack(">I", bytes(buf[CH3_CFG32:CH3_CFG32 + 4]))[0]
    stat = buf[CH3_STATUS]
    return dict(optbyte=ob, code=code, opt=opt, coupling=coup,
                value=val, cfg=cfg, status=stat)

def ask(prompt, default=None):
    s = input(prompt).strip()
    return s if s else (default if default is not None else "")

def confirm(prompt):
    return ask(prompt + " [y/N]: ").lower() in ("y", "yes")

# ------------------------------------------------------------------- state -----
class Tool:
    def __init__(self):
        self.path = None
        self.buf  = None

    # ---- file ops
    def load(self, path=None):
        path = path or ask("Path to EEPROM dump (.bin): ")
        if not path:
            return
        if not os.path.isfile(path):
            print("  ! No such file:", path); return
        data = open(path, "rb").read()
        if len(data) != EE_SIZE:
            print("  ! Expected %d bytes, got %d." % (EE_SIZE, len(data)))
            if not confirm("  Load anyway (pad/truncate to 8192)?"):
                return
            data = (data + b"\xFF" * EE_SIZE)[:EE_SIZE]
        self.path = path
        self.buf  = bytearray(data)
        print("  Loaded %s (%d bytes)." % (path, len(self.buf)))
        self.info()

    def save(self, path=None):
        if self.buf is None:
            print("  ! Nothing loaded."); return
        path = path or ask("Save as [%s]: " % self.path, self.path)
        open(path, "wb").write(bytes(self.buf))
        self.path = path
        print("  Wrote %s." % path)

    def backup(self):
        if self.buf is None:
            print("  ! Nothing loaded."); return
        ts  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = "%s.%s.bak" % (self.path or "eeprom", ts)
        open(dst, "wb").write(bytes(self.buf))
        print("  Backup written: %s" % dst)

    def restore(self):
        src = ask("Backup file to restore from: ")
        if not os.path.isfile(src):
            print("  ! No such file."); return
        data = open(src, "rb").read()
        if len(data) != EE_SIZE:
            print("  ! Not an 8192-byte image."); return
        if not confirm("  Replace the working image with %s?" % src):
            return
        self.buf = bytearray(data)
        print("  Restored from %s." % src)
        self.info()

    # ---- info
    def info(self):
        if self.buf is None:
            print("  ! Nothing loaded."); return
        sig_ok, cs_ok = hdr_valid(self.buf)
        stored_cs = struct.unpack(">H", bytes(self.buf[0:2]))[0]
        print("\n  --- EEPROM header ($400000 region) ---")
        print("    Signature @ $02 : %s" % ("OK ('%s')" % SIGNATURE.decode()
              if sig_ok else "MISSING / bad  (NVRAM looks uninitialised)"))
        print("    Checksum   @ $00 : stored $%04X, computed $%04X  -> %s"
              % (stored_cs, checksum(self.buf), "OK" if cs_ok else "MISMATCH"))
        c = decode_ch3(self.buf)
        print("\n  --- Channel 3 option ---")
        print("    Option byte $1B  : $%02X  (code %d, coupling %s)"
              % (c["optbyte"], c["code"], c["coupling"]))
        print("    Reported *OPT?   : %s   %s"
              % (c["opt"], OPT_DESC.get(c["opt"], "")))
        print("    Prescale $1C     : $%02X  (ratio = %d x 128 = %d)"
              % (c["value"], c["value"], c["value"] * 128))
        print("    Cfg $16 : $%08X     Status $1A : $%02X"
              % (c["cfg"], c["status"]))
        if not sig_ok:
            print("\n    NOTE: header invalid - the counter would reinitialise this")
            print("          record to defaults (Option 030) on next power-up.")
        print()

    # ---- channel 3
    def change_ch3(self):
        if self.buf is None:
            print("  ! Nothing loaded."); return
        c = decode_ch3(self.buf)
        print("\n  Current Channel 3 option: %s (%s)" % (c["opt"], OPT_DESC.get(c["opt"], "")))
        print("  Choose the option that matches the INSTALLED A3 prescaler board:")
        opts = ["015", "030", "050", "124", "160", "200"]
        for i, o in enumerate(opts, 1):
            print("    %d) %s   %s" % (i, o, OPT_DESC[o]))
        sel = ask("  Option number (blank = cancel): ")
        if not sel.isdigit() or not (1 <= int(sel) <= len(opts)):
            print("  Cancelled."); return
        opt = opts[int(sel) - 1]
        code = OPT_TO_CODE[opt]
        coup_ac = confirm("  AC coupling? (No = DC)")
        newb = (code & 0x7F) | (0x80 if coup_ac else 0x00)
        # prescaler ratio -> $1C = ratio/128. Default per option; allow override
        # for a board whose divider differs from the table (e.g. 160/200).
        pdef = OPT_TO_PRESCALE.get(opt, 128)
        pr = ask("  Prescaler divide ratio [%d]: " % pdef, str(pdef))
        try:
            pval = int(pr)
        except ValueError:
            print("  Cancelled (not a number)."); return
        if not (128 <= pval <= 16384) or (pval // 128) * 128 != pval:
            print("  Cancelled (ratio must be a multiple of 128 in 128..16384)."); return
        new1c = pval // 128
        # If the validity signature is missing (a header never written, e.g. a
        # unit not shipped with the optional channel), rebuild the fixed header
        # fields so writing the option produces a complete, valid header rather
        # than one the firmware would reject. The $5313 calibration records at
        # $20 onward are left untouched.
        sig_missing = not signature_present(self.buf)
        if sig_missing:
            rebuild_blank_header_fields(self.buf)
        # keep status sane if the header was blank
        if self.buf[CH3_STATUS] in (0x00, 0xFF): self.buf[CH3_STATUS] = CH3_STATUS_DEFAULT
        old   = self.buf[CH3_OPTBYTE]
        old1c = self.buf[CH3_VALUE]
        self.buf[CH3_OPTBYTE] = newb
        self.buf[CH3_VALUE]   = new1c
        fix_header(self.buf)
        if sig_missing:
            print("  Header signature was MISSING - rebuilt it:")
            print("    wrote '%s' @ $02, config $%08X @ $16, status $%02X @ $1A."
                  % (SIGNATURE.decode(), CH3_CFG_DEFAULT, self.buf[CH3_STATUS]))
        print("  $1B: $%02X -> $%02X  (option %s, %s)"
              % (old, newb, opt, "AC" if coup_ac else "DC"))
        print("  $1C: $%02X -> $%02X  (prescale %d) ; header signature + checksum written."
              % (old1c, new1c, pval))
        print("  Review with 'Show info', then Save and write the dump back to the chip.")
        print("  (Equivalent live command:  :DIAGnostic:OPTion:HFR %d,N%s,%d  - see supplement)"
              % (pval, opt, 1 if coup_ac else 0))

    # ---- calibration password
    def reset_password(self):
        print("""
  --- Calibration security code ---
  The security code is NOT stored at a fixed byte in this dump: the calibration
  records ($400020 onward) are bit-packed, so it cannot be read or rewritten
  safely by editing the image. The only reliable resets are:

    (a) FRONT PANEL, no gear needed:
        power off, hold the LEFT and RIGHT arrow keys, switch on.
        Display shows 'EEPROM CLEAR'. The code returns to the model default
        (%s), but ALL calibration is erased too.

    (b) THIS TOOL - do the same thing to the dump: blank the whole EEPROM so
        the counter reinitialises to defaults on next power-up. Same effect as
        (a): the code becomes the model default and CALIBRATION IS ERASED.

    (c) If you still KNOW the code, no reset is needed - unsecure and change it
        over the bus:
            :CALibration:SECurity:STATe OFF, <present_code>
            :CALibration:SECurity:CODE <new_code>
""" % " / ".join(DEFAULT_CODES.values()))
        if self.buf is None:
            print("  (No dump loaded - option (b) needs a loaded image.)"); return
        if not confirm("  Do option (b): factory-blank this image now?"):
            print("  Left unchanged."); return
        print("\n  This ERASES CALIBRATION. Keep the backup this tool just makes so you")
        print("  can restore it if you recover or reset the code by other means.")
        if ask("  Type ERASE to proceed: ") != "ERASE":
            print("  Aborted."); return
        self.backup()
        self.buf = bytearray(b"\xFF" * EE_SIZE)
        print("  Image blanked to $FF. Save it and write it to the chip; on next")
        print("  power-up the counter rebuilds NVRAM with the default security code.")

# ------------------------------------------------------------------- menu ------
MENU = """
======== 531xx EEPROM Tool (AT28C64B / $400000 NVRAM) ========
  Loaded: %s
  1) Load dump file
  2) Show info / decode
  3) Backup (timestamped copy)
  4) Restore from backup
  5) Change Channel 3 option
  6) Reset calibration password (help + factory-blank)
  7) Save dump
  8) Quit
"""

def main():
    t = Tool()
    if len(sys.argv) > 1:
        t.load(sys.argv[1])
    actions = {"1": t.load, "2": t.info, "3": t.backup, "4": t.restore,
               "5": t.change_ch3, "6": t.reset_password, "7": t.save}
    while True:
        print(MENU % (t.path or "(none)"))
        ch = ask("  Choice: ")
        if ch == "8" or ch.lower() in ("q", "quit", "exit"):
            break
        act = actions.get(ch)
        if act:
            try:
                act()
            except (KeyboardInterrupt, EOFError):
                print("\n  (cancelled)")
            except Exception as e:
                print("  ! Error:", e)
        else:
            print("  ? Unknown choice.")
    print("  Bye.")

if __name__ == "__main__":
    main()
