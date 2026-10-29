"""Battery monitoring and low-battery shutdown (currawong_ui.Battery and App.housekeeping)."""
import importlib.util, math, os, sys
spec = importlib.util.spec_from_file_location(
    "ui", os.path.join(os.path.dirname(os.path.abspath(__file__)), "currawong_ui.py"))
ui = importlib.util.module_from_spec(spec); spec.loader.exec_module(ui)
ui.save_settings = lambda s: None
fails = 0
def check(ok, what):
    global fails
    print(("  pass: " if ok else "  FAIL: ") + what); fails += 0 if ok else 1

class Gauge:
    """A MAX17048 on a fake I2C bus. volts / soc / rate may be numbers or functions of time."""
    def __init__(self, volts=3.85, soc=60.0, rate=-8.0):
        self.volts, self.soc, self.rate = volts, soc, rate
        self.t = 0.0
        self.fail = False          # every read raises (bus error / gauge gone)
        self.fail_every = 0        # every Nth read raises
        self.zeros = False         # a glitch: everything reads 0
        self.reads = {0x02: 0, 0x04: 0, 0x16: 0}
        self.n = 0
        self.writes = []
    def _v(self, x):
        return x(self.t) if callable(x) else x
    def read_word_data(self, addr, reg):
        self.n += 1
        if self.fail or (self.fail_every and self.n % self.fail_every == 0):
            raise OSError(121, "Remote I/O error")
        self.reads[reg] = self.reads.get(reg, 0) + 1
        if self.zeros:
            return 0
        if reg == 0x02:
            raw = int(round(self._v(self.volts) / 78.125e-6))
        elif reg == 0x04:
            raw = int(self._v(self.soc) * 256)
        elif reg == 0x16:
            raw = int(round(self._v(self.rate) / 0.208)) & 0xFFFF
        else:
            raw = 0
        return ((raw & 0xFF) << 8) | (raw >> 8)   # big-endian on the wire
    def write_word_data(self, addr, reg, val):
        self.writes.append((reg, val))

class Pin:
    def __init__(self, value=0): self.value = value

def battery(gauge=None, usb=None, lbo=None, mah=2000):
    logs = []
    b = ui.Battery(bus=gauge, usb=usb, lbo=lbo, log=logs.append)
    b.configure(mah, 3.30)
    b.logs = logs
    return b

def run(b, gauge, t0, secs, step=0.5):
    """Advance time, updating like housekeeping does. Returns (time of shutdown or None, reason)."""
    t = t0
    while t < t0 + secs:
        if gauge is not None:
            gauge.t = t
        b.update(t)
        why = b.shutdown_due(t)
        if why:
            return t, why
        t += step
    return None, None

print("== normal discharge ==")
g = Gauge(volts=lambda t: 3.85 - t * 1e-5, soc=lambda t: 62 - t / 600, rate=-8.0)
b = battery(g)
t_off, why = run(b, g, 0, 1800)
check(b.source == "gauge" and b.pct is not None and 58 <= b.pct <= 62, f"reads the charge ({b.pct}%) and voltage ({b.volts:.2f} V)")
check(t_off is None and b.low_since is None and b.volt_low_since is None, "30 minutes at 60%: no shutdown, nothing armed")
check((0x0A, 0) in g.writes, "the gauge is kept out of hibernate (HIBRT = 0)")
check(g.reads[0x02] <= 1800 / 2 + 2 and g.reads[0x04] <= 1800 / 15 + 2, f"I2C as before: voltage every 2 s ({g.reads[0x02]} reads), charge every 15 s ({g.reads[0x04]})")
b5 = battery(Gauge(rate=-8.0), mah=5000); run(b5, b5._bus, 0, 20)
b2 = battery(Gauge(rate=-8.0), mah=2000); run(b2, b2._bus, 0, 20)
ma2 = float(b2.drain_text()[1:].split()[0]); ma5 = float(b5.drain_text()[1:].split()[0])
check(abs(ma5 / ma2 - 2.5) < 0.02 and abs(ma2 - 7.9 * 20) < 2,
      f"drain estimate = rate x capacity: 2000 mAh {b2.drain_text()!r}, 5000 mAh {b5.drain_text()!r}")
check("~" in b5.drain_text(), "and is shown as an estimate (~), not a measured current")

print("== low charge, discharging ==")
g = Gauge(volts=3.55, soc=4.0, rate=-9.0)
b = battery(g)
t_off, why = run(b, g, 0, 200)
check(why == "charge" and 60 < t_off <= 80, f"4% while discharging: switches off after a minute ({t_off:.0f} s, {why})")
check(b.low and "switching off in 60 s" in " ".join(b.logs), "the icon's LOW flag is set, and the journal says why")

print("== low charge while surely charging ==")
g = Gauge(volts=3.70, soc=3.0, rate=+25.0)
b = battery(g)
t_off, _ = run(b, g, 0, 900)
check(t_off is None and b.charge_confidence() == "yes" and b.low_since is None,
      "3% with the gauge's rate +25 %/h (two readings in a row): no shutdown in 15 minutes")
check(b.low, "the LOW flag still shows (icons and warnings unchanged)")
g = Gauge(volts=3.70, soc=3.0, rate=-9.0)
b = battery(g, usb=Pin(1))
t_off, _ = run(b, g, 0, 600)
check(t_off is None and b.charge_confidence() == "yes", "with the wired charger pin high: no shutdown either")

print("== charging state uncertain ==")
g = Gauge(volts=lambda t: 3.55 if t < 10 else 3.69, soc=4.0, rate=-9.0)   # a voltage step, the rate not yet
b = battery(g)
b.detector.started = -1000          # past its boot settle
t_off, why = run(b, g, 0, 400)
check(b.charging is True and b.charge_confidence() == "unsure",
      "a voltage step looks like a charger: the indicator shows it, but it's only a guess (unconfirmed)")
check(why == "charge" and 60 < t_off <= 80, f"so the charge protection still switches off ({why} at {t_off:.0f} s): conservative")
g = Gauge(volts=3.60, soc=4.0, rate=+1.5)   # a rested cell's drift, not charging
b = battery(g)
t_off, why = run(b, g, 0, 200)
check(why == "charge" and b.charge_confidence() == "no", "a +1.5 %/h drift doesn't count as charging: still switches off")

print("== the voltage protection ==")
def sag(t):     # motor kicks: down to 3.0 V for under a second, every 10 s
    return 3.0 if (t % 10) < 0.8 else 3.62
g = Gauge(volts=sag, soc=30.0, rate=-12.0)
b = battery(g)
t_off, _ = run(b, g, 0, 900, step=0.25)
check(t_off is None and b.volt_low_since is None, "motor kicks to 3.0 V for under a second: never")
g = Gauge(volts=lambda t: 3.62 if t < 60 else 3.26, soc=30.0, rate=-12.0)
b = battery(g)
t_off, why = run(b, g, 0, 300)
check(why == "voltage" and 60 + 30 < t_off <= 60 + 30 + 14, f"held at 3.26 V: off {t_off - 60:.0f} s after it dropped (median + 30 s)")
g = Gauge(volts=lambda t: 3.62 if t < 60 else 3.05, soc=30.0, rate=-12.0)
b = battery(g)
t_off, why = run(b, g, 0, 300)
check(why == "voltage" and t_off - 60 <= 14, f"at 3.05 V (critical): off {t_off - 60:.0f} s after it dropped")
g = Gauge(volts=lambda t: 3.62 if t < 60 else 3.26, soc=30.0, rate=-12.0)
b = battery(g, usb=Pin(0))
b.configure(2000, 3.20)
t_off, _ = run(b, g, 0, 300)
check(t_off is None, "the cutoff is configurable: at 3.20 V, 3.26 V is fine")
g = Gauge(volts=lambda t: 3.62 if t < 60 else 3.26, soc=30.0, rate=+20.0)
b = battery(g)
t_off, why = run(b, g, 0, 300)
check(why == "voltage", "low voltage isn't excused by an inferred charge (only the wired pin pauses it)")

print("== recovery ==")
g = Gauge(volts=lambda t: 3.62 if t < 30 or t > 50 else 3.24, soc=30.0, rate=-12.0)
b = battery(g)
t_off, _ = run(b, g, 0, 300)
check(t_off is None and b.volt_low_since is None and "back to" in " ".join(b.logs), "a 20 s dip that recovers: armed, then cancelled")
g = Gauge(volts=lambda t: 3.62 if t < 30 else 3.32, soc=30.0, rate=-12.0)
b = battery(g)
run(b, g, 0, 29)
g.volts = lambda t: 3.26 if t < 50 else 3.33        # just above the cutoff, inside the hysteresis band
t_off, why = run(b, g, 29, 120)
check(why == "voltage", "hovering just above the cutoff (inside the 50 mV band) doesn't cancel it: no flapping")
g = Gauge(volts=3.60, soc=4.0, rate=-9.0)
b = battery(g)
run(b, g, 0, 40)
armed = b.low_since is not None
g.rate = +25.0
t_off, _ = run(b, g, 40, 600)
check(armed and t_off is None and b.low_since is None, "low charge armed, then the charger goes in (rate rises): cancelled")

print("== missing gauge ==")
g = Gauge(); g.fail = True
b = battery(g)
check(b.source is None and b.pct is None and b.text() == "N/A", "no gauge: level unknown (N/A), not empty")
t_off, _ = run(b, g, 0, 600)
check(t_off is None, "and never a shutdown from not knowing")
g.fail = False; g.soc = 55.0
run(b, g, 600, 60)
check(b.source == "gauge" and b.pct == 55 and "answering" in " ".join(b.logs), "the gauge turns up later (battery plugged in): found again within 30 s")
b = battery(None, lbo=Pin(0))
check(b.source == "lbo" and b.text() == "OK", "no gauge but LBO wired: the LBO fallback, as before")
b._lbo.value = 1
t_off, why = run(b, None, 0, 200)
check(why == "charge" and 60 < t_off <= 62, "LBO low: off after a minute, as before")

print("== I2C failures and nonsense ==")
import random
class Flaky(Gauge):
    def __init__(self, p, **kw):
        super().__init__(**kw); self.rng, self.p = random.Random(3), p
    def read_word_data(self, addr, reg):
        if self.rng.random() < self.p:
            raise OSError(121, "Remote I/O error")
        return super().read_word_data(addr, reg)
g = Flaky(0.3, volts=3.80, soc=50.0, rate=-8.0)
b = battery(g)
t_off, _ = run(b, g, 0, 900)
check(t_off is None and b.pct == 50 and b.gauge_ok, "30% of reads failing at random: values hold, no shutdown")
g = Flaky(0.3, volts=3.55, soc=4.0, rate=-9.0)
b = battery(g)
t_off, why = run(b, g, 0, 300)
check(why == "charge" and t_off <= 120, f"and a real low charge is still acted on through the failures ({t_off:.0f} s)")
g = Gauge(volts=3.80, soc=50.0, rate=-8.0)
b = battery(g)
run(b, g, 0, 60)
g.zeros = True
t_off, _ = run(b, g, 60, 50)
check(t_off is None and b.volt_low_since is None and b.low_since is None and b.pct == 50,
      "a glitch reading all zeros (0 V, 0%): ignored, no shutdown")
g = Gauge(volts=3.95, soc=2.0, rate=-3.0)
b = battery(g)
t_off, _ = run(b, g, 0, 300)
check(t_off is None and "not believed" in " ".join(b.logs), "2% while the cell sits at 3.95 V (gauge settling): not believed")
g = Gauge(volts=3.80, soc=50.0, rate=-8.0)
b = battery(g)
run(b, g, 0, 60)
g.fail = True
t_off, _ = run(b, g, 60, 200)
check(t_off is None and b.pct is None and b.source == "gauge" and b.text() == "N/A",
      "the gauge goes silent while all's well: unknown after a minute, no shutdown")
g = Gauge(volts=3.60, soc=4.0, rate=-9.0)
b = battery(g)
run(b, g, 0, 30)
g.fail = True
t_off, why = run(b, g, 30, 200)
check(why == "charge" and t_off <= 30 + 60, "silent while a shutdown is counting down: it still happens (can't be checked any more)")

print("== in the app ==")
g = Gauge(volts=3.55, soc=4.0, rate=-9.0)
b = battery(g)
s = ui.Snap(); s.reachable = True
app = ui.App(ui._FakePlayer(s), ui._CaptureDisplay(), b, ui.Analyser(threaded=False), wifi=ui._FakeWifi(), bt=ui._FakeBT())
app.settings["auto_off_mins"] = 0
offs, ticks = [], []
app.power_off = lambda reason=None: offs.append(reason)
app.alert = lambda n: ticks.append(n)
t = 0.0
while t < 120 and not offs:
    g.t = t; app.housekeeping(t); t += 0.5
check(offs == ["BATTERY LOW"] and 60 < t <= 80, f"housekeeping: the same BATTERY LOW power-off, after a minute ({t:.0f} s)")
check(ticks == [3], "and the 5% alert on the dial still comes first (3 ticks)")
check(app.battery.capacity_mah == 2000, "existing installs: 2000 mAh until set")
app.settings["battery_mah"] = 5000
st = ui.advanced_menu(app); rows = {i[0]: i for i in st.items}
check("BATTERY" in rows and rows["BATTERY"][1]() == "5000 mAh", "Settings > Advanced > BATTERY: 2000 or 5000 mAh")
opt = rows["BATTERY"][2](); opt.idx = 1; opt.handle("press", "enc_sw")
check(app.battery.capacity_mah == 5000, "choosing 5000 mAh updates the estimate at once")
print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
