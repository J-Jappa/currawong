/*
  Currawong haptic knob firmware (v0.6)

  Hardware: Seeed XIAO ESP32-S3, SparkFun TMC6300 (six-PWM), DFRobot 2804 motor
  with its AS5600 encoder, all as wired on the Currawong v1.0 PCB.

  The haptic model follows the open-source SmartKnob (github.com/scottbez1/smartknob):
  a detent is a spring pulling towards its centre, the next detent takes over once the
  knob passes a snap point, the ends of a bounded range are stiffer walls, and the
  motor is switched off while the knob rests on a detent.

  Modes (cycle with the button, or pick one over USB serial). The first six are the ones the Pi
  uses; the sk- modes copy SmartKnob's demo configs; the last three are extras to try.
    coarse   12 strong clicks per turn, endless
    fine     36 light clicks per turn, endless
    list8    8 positions with end stops (what a menu will feel like)
    smooth   no clicks, a little viscous drag
    elastic  springs back to centre when released (a jog / shuttle wheel)
    binary   a two-position switch: off and on, with walls either side
    sk-unbounded / sk-bounded / sk-multirev   no clicks; endless, 0-10, or two turns with walls
    sk-return          one position: walls both sides, springs back
    sk-fine-free / sk-fine-detent             256 values at 1 deg, without and with a fine texture
    sk-coarse-strong / sk-coarse-weak         32 values at 8.2 deg
    sk-magnetic        32 values, but only 2, 10, 21 and 22 pull you in
    sk-return-detents  13 positions, 60 deg apart, starting in the middle
    volume             0-100 in light 3 deg clicks, starting at 30
    viscous            no clicks, heavy drag
    clickwheel         24 medium clicks per turn, endless
    scrub              48 light clicks per turn, endless (song scrubbing on Now Playing)

  Button: the XIAO's BOOT button (or a button from D4 to GND, see PIN_BUTTON).
    short press = next mode, hold 3 s = re-run motor calibration.

  Direction: v0.1 turned the wrong way for lists. "Forward" is now stored against the sensor itself
  (so a re-calibration can't flip it), set the first time this version runs to the opposite of v0.1.
  Type r on USB to flip and save it; the Pi can flip it again with $DIR.

  USB serial at 115200: a readable log, plus a console for trying modes (type ? for help).
  Serial1 (the Pi link): lines like $POS,3,1*4A (NMEA-style XOR checksum).
    To the Pi:   $HELLO,currawong-knob,6   $POS,<position>,<delta>   $JOG,<-100..100>
                 $MODE,<index>,<name>   $GRAB (a hand stopped a spin or roll)
    From the Pi: $PING   $MODE,<index>   $STR,<0|1|2>   $CFG,<kind>,<positions>,<position>
                 (kind = list, or any mode name above)
                 $SPIN,<1|-1>       turn one full revolution forward or back (track skip)
                 $ROLL,<mrev/s>     keep turning at this rate, e.g. 500 = half a turn a second; 0 stops
                 $LOCK,<1|0>        pocket lock: motor off and no positions until unlocked
                 $DIR,<1|-1>        the Pi's direction setting (1 = as stored here)
                 $TICK[,<n>[,<pct>]] one short click (e.g. to confirm an unlock), or n stronger ones as an
                                    alert (felt even while pocket-locked); pct scales it (the
                                    button clicks are a soft 30-100%)
                 $RANGE,<n>,<pos>   walls after n positions (0 = endless), and the knob's position, without
                                    moving the detents (song scrubbing: 0:00 and the end are walls)
                 $MARK[,<p>,...]    landmark positions (up to 64) get a stronger click; none = clear
                 $MAG,<pos>         this position pulls the knob in from one away (-1 = off)
                 $FEEL,<pct>        the dial's strength, as a percent of normal (finer than $STR)
                 $OTA,BEGIN,<size>,<md5> / $OTA,DATA,<n>,<base64> / $OTA,END / $OTA,ABORT
                                    new firmware over this same cable (see "Updates from the Pi")
  Fast spins on long lists coast: the clicks fade with speed and come back as it slows.

  Power: the motor switches off 3 s after the knob stops moving, and while it's off the ESP32 drops
  to 80 MHz and checks the sensor every 10 ms instead of running flat out (LOW_POWER_IDLE).

  Limits: see docs/hardware.md. PHASE_RESISTANCE is measured (6.6 ohm lead to lead, halved);
  re-measure if the motor changes, as the thermal estimate depends on it.
*/
#include <Arduino.h>
#include <Wire.h>
#include <Preferences.h>
#include <SimpleFOC.h>
#include <Update.h>
#include "driver/gpio.h"
#include "mbedtls/base64.h"
#include "feel.h"

// ---------------------------------------------------------------- pins
constexpr int PIN_UH = 1;    // D0
constexpr int PIN_UL = 2;    // D1
constexpr int PIN_VH = 3;    // D2 (a strapping pin, harmless on a stock XIAO)
constexpr int PIN_VL = 4;    // D3
constexpr int PIN_WH = 7;    // D8
constexpr int PIN_WL = 44;   // D7 (UART0's default RX, so UART0 must stay unused)
constexpr int PIN_SDA = 8;   // D9  \ swap these two if the AS5600 isn't found
constexpr int PIN_SCL = 9;   // D10 /
constexpr int PIN_PI_RX = 6;   // D5, from Pi pin 8
constexpr int PIN_PI_TX = 43;  // D6, to Pi pin 10 (the ROM also prints its boot log here)
constexpr int PIN_BUTTON = 0;  // BOOT button. Use 5 (D4) for an external button to GND.
constexpr int PIN_LED = 21;    // user LED, active low

#define SENSOR_SELFTEST 0  // 1: print the raw angle for 5 s at boot (turn the knob by hand)
#define PHASE_TEST 0      // 1: at boot, drive each phase in turn and log where the shaft goes
#define LOW_POWER_IDLE 1  // 0: stay at 240 MHz and full loop speed while the motor is off

// ---------------------------------------------------------------- limits
constexpr float SUPPLY_VOLTS = 5.0f;      // set to the battery voltage after the VBAT rework
constexpr float VOLTAGE_LIMIT = 2.0f;     // never above 2.5 V without re-measuring the rail
constexpr float ALIGN_VOLTS = 1.0f;       // first calibration nudge voltage
constexpr float ALIGN_TRY[] = {1.0f, 1.5f, 2.0f};  // tried in order if the shaft doesn't follow
constexpr float PHASE_RESISTANCE = 3.3f;  // ohms per phase: 6.6 measured lead to lead, halved
constexpr float THERMAL_LIMIT_W = 1.0f;   // derate above this 10 s average
constexpr uint32_t IDLE_OFF_MS = 3000;    // motor off after resting this long
constexpr float ENDSTOP_FULL_S = 0.5f;    // full-strength wall for this long...
constexpr float ENDSTOP_FADE_S = 1.0f;    // ...then fade over this long...
constexpr float ENDSTOP_HOLD = 0.33f;     // ...down to this fraction
constexpr float LIST_WIDTH_DEG = 20.0f;   // detent spacing for menus from the Pi
constexpr float DAMPING = 0.02f;          // volts per rad/s, steadies the detents (d on USB)
constexpr float RUNAWAY_RAD_S = 60.0f;    // no torque above this speed (SmartKnob's runaway guard)
constexpr float ELASTIC_MAX = 1.5f;       // volts, spring force ceiling in elastic mode
constexpr float ELASTIC_RANGE_DEG = 90.0f;  // wall beyond this deflection
constexpr uint32_t SPIN_MS = 700;         // one full turn on a track skip, eased in and out
constexpr float MOTION_KP = 6.0f;         // V per rad pulling the knob along a spin or roll
constexpr float MOTION_KD = 0.04f;        // V per rad/s, against the speed error
constexpr float SPIN_GRAB_DEG = 60.0f;    // this far behind for GRAB_MS = a hand is holding it
constexpr float ROLL_GRAB_DEG = 40.0f;
constexpr uint32_t GRAB_MS = 150;
constexpr float TICK_VOLTS = 1.2f;        // one confirmation click: a push, then a pull back
constexpr uint32_t TICK_MS = 12;
constexpr float ALERT_VOLTS = 1.8f;       // alert ticks ($TICK,n): stronger and a little longer
constexpr uint32_t ALERT_MS = 16;
constexpr uint32_t ALERT_GAP_MS = 140;    // between the ticks of one alert
constexpr float COAST_FROM = 6.0f;        // rad/s (about 1 turn/s): clicks start to fade...
constexpr float COAST_TO = 14.0f;         // ...and are gone by this (about 2.2 turns/s)
constexpr int COAST_MIN_POSITIONS = 16;   // only endless ranges and lists longer than this coast
constexpr float MARK_GAIN = 2.0f;         // landmark clicks are this much stronger
constexpr float MARK_COAST_MIN = 0.6f;    // and keep this much of that while coasting
constexpr float MAGNET_VOLTS = 0.6f;      // the magnet's peak pull
constexpr float MAGNET_RANGE = 2.0f;      // its reach, in positions either side
constexpr float MAGNET_NEIGHBOUR = 0.45f; // the detents beside it are weakened so it can pull you in
constexpr float RANGE_ENDSTOP = 12.0f;    // V/rad: the walls $RANGE adds to an endless mode
constexpr uint32_t IDLE_LOOP_MS = 10;     // sensor check interval while the motor is off

// Drive all six bridge inputs low as early as possible, before setup() runs.
// The board has no pull-downs, so until now the driver inputs float.
struct EarlyPinsLow {
  EarlyPinsLow() {
    const int pins[] = {PIN_UH, PIN_UL, PIN_VH, PIN_VL, PIN_WH, PIN_WL};
    for (int p : pins) {
      gpio_reset_pin((gpio_num_t)p);
      gpio_set_direction((gpio_num_t)p, GPIO_MODE_OUTPUT);
      gpio_set_level((gpio_num_t)p, 0);
    }
  }
} earlyPinsLow;

inline float rad(float deg) { return deg * 0.017453293f; }
inline float deg(float r) { return r * 57.29578f; }

// ---------------------------------------------------------------- motor objects
BLDCMotor motor(7);  // DFRobot 2804: 7 pole pairs
BLDCDriver6PWM driver(PIN_UH, PIN_UL, PIN_VH, PIN_VL, PIN_WH, PIN_WL);
MagneticSensorI2C sensor(AS5600_I2C);
Preferences prefs;

// ---------------------------------------------------------------- haptic profiles
enum Kind : uint8_t { DETENT, SMOOTH, ELASTIC };

struct Profile {
  const char* name;
  Kind kind;
  int positions;    // 0 = endless
  int start;        // position a mode starts on
  float widthDeg;   // detent spacing (smooth: reporting step)
  float strength;   // detent: peak volts of each hill. smooth: V per rad/s. elastic: V per rad
  float snap;       // fraction of the spacing where the next detent takes over (above 1 = lag)
  float endstop;    // volts per radian beyond the ends
  uint32_t detentMask;  // 0 = every position pulls; else only positions whose bit is set
};

// The sk- rows are SmartKnob's demo configs (firmware/src/interface_task.cpp). Its strengths are
// in its own units for a different motor, so those are translated by feel: SmartKnob's endstop
// unit 1 is its gain of 4 V/rad, and the detent peaks are picked by hand. Spacing, range, start
// and snap points are copied as they are.
const Profile PRESETS[] = {
    // name               kind     pos  start width           strength snap   endstop
    {"coarse",            DETENT,  0,   0,    30.0f,          0.9f,    0.55f, 0.0f},
    {"fine",              DETENT,  0,   0,    10.0f,          0.4f,    0.55f, 0.0f},
    {"list8",             DETENT,  8,   0,    LIST_WIDTH_DEG, 0.8f,    0.55f, 12.0f},
    {"smooth",            SMOOTH,  0,   0,    2.0f,           0.06f,   0.0f,  0.0f},
    {"elastic",           ELASTIC, 0,   0,    0.0f,           2.5f,    0.0f,  8.0f},
    {"binary",            DETENT,  2,   0,    60.0f,          1.5f,    0.5f,  12.0f},
    {"sk-unbounded",      DETENT,  0,   0,    10.0f,          0.0f,    1.1f,  0.0f},
    {"sk-bounded",        DETENT,  11,  0,    10.0f,          0.0f,    1.1f,  4.0f},
    {"sk-multirev",       DETENT,  73,  0,    10.0f,          0.0f,    1.1f,  4.0f},
    {"sk-return",         DETENT,  1,   0,    60.0f,          0.0f,    1.1f,  2.4f},
    {"sk-fine-free",      DETENT,  256, 127,  1.0f,           0.0f,    1.1f,  4.0f},
    {"sk-fine-detent",    DETENT,  256, 127,  1.0f,           0.1f,    1.1f,  4.0f},
    {"sk-coarse-strong",  DETENT,  32,  0,    8.225806f,      0.5f,    1.1f,  4.0f},
    {"sk-coarse-weak",    DETENT,  32,  0,    8.225806f,      0.15f,   1.1f,  4.0f},
    {"sk-magnetic",       DETENT,  32,  0,    7.0f,           0.8f,    0.7f,  4.0f,
     (1u << 2) | (1u << 10) | (1u << 21) | (1u << 22)},
    {"sk-return-detents", DETENT,  13,  6,    60.0f,          1.0f,    0.55f, 4.0f},
    {"volume",            DETENT,  101, 30,   3.0f,           0.12f,   0.55f, 12.0f},
    {"viscous",           SMOOTH,  0,   0,    2.0f,           0.2f,    0.0f,  0.0f},
    {"clickwheel",        DETENT,  0,   0,    15.0f,          0.5f,    0.55f, 0.0f},
    {"scrub",             DETENT,  0,   0,    7.5f,           0.25f,   0.55f, 0.0f},
};
constexpr int NUM_PRESETS = sizeof(PRESETS) / sizeof(PRESETS[0]);

Profile prof = PRESETS[0];
int presetIndex = 0;
int position = 0;        // current detent index
float centre = 0;        // angle (rad) of the current detent or spring centre
float strengthScale = 1.0f;
float damping = DAMPING;
bool logOn = true;

// Direction. The knob's angle is knobSign * the raw sensor angle, so "forward" is physical.
// SimpleFOC's own frame is sensor_direction * raw, so torque is converted with toMotor().
int knobSign = 1;  // stored in flash ("ks")
int piDir = 1;     // the Pi's setting, not stored (the Pi resends it after HELLO)
inline int dirSign() { return knobSign * piDir; }
float readKnobAngle() { return dirSign() * sensor.getAngle(); }
inline float toMotor(float u) { return u * dirSign() * (int)motor.sensor_direction; }

// Pocket lock, motions (spin on skip, roll while the Pi scrubs) and the confirmation tick.
bool locked = false;
enum Motion : uint8_t { NONE, SPIN, ROLL };
Motion motion = NONE;
float motionFrom = 0, motionTo = 0, setpoint = 0, rollRate = 0;  // rad, rad, rad, rad/s
uint32_t motionStartMs = 0, grabSinceMs = 0;
uint32_t tickStartMs = 0;
bool tickOn = false;
int tickLeft = 0;         // ticks still to come in this alert
bool tickAlert = false;   // the stronger alert kind
float tickScale = 1.0f;   // this tick's strength: button clicks are softer than the rest

// Landmarks and the magnet, set by the Pi per screen (cleared by every new mode).
feel::Marks marks;
int magnetPos = -1;

// Low-power idle: 80 MHz and a 10 ms loop while the motor is off.
bool slow = false;
void setSlow(bool on) {
#if LOW_POWER_IDLE
  if (on == slow) return;
  slow = on;
  setCpuFrequencyMhz(on ? 80 : 240);
#else
  (void)on;
#endif
}
// Back to full speed, then the motor on. Every path that drives the motor comes through here.
void motorOn() {
  setSlow(false);
  if (!motor.enabled) motor.enable();
}

// ---------------------------------------------------------------- loop state
float angleNow = 0, prevAngle = 0, knobVel = 0;
uint32_t lastUs = 0;
float moveRef = 0;
uint32_t lastMoveMs = 0;
uint32_t endstopSince = 0;
float powerAvg = 0, thermalFactor = 1.0f;
int lastJog = 9999;
uint32_t lastJogMs = 0, lastLogMs = 0;
float lastU = 0;
float idleVel = 0;
uint32_t stillSinceMs = 0;  // 0 = moving

// ---------------------------------------------------------------- Pi link
void sendLine(const String& body) {
  uint8_t cs = 0;
  for (size_t i = 0; i < body.length(); i++) cs ^= (uint8_t)body[i];
  char tail[8];
  snprintf(tail, sizeof(tail), "*%02X\n", cs);
  Serial1.print('$');
  Serial1.print(body);
  Serial1.print(tail);
}

void emitPosition(int delta) {
  sendLine("POS," + String(position) + "," + String(delta));
}

// ---------------------------------------------------------------- LED (non-blocking blinks)
int blinksLeft = 0;
uint32_t ledNext = 0;
bool ledOn = false;

void blink(int n) {
  blinksLeft = n * 2;
  ledNext = 0;
}

void updateLed(uint32_t nowMs) {
  if (blinksLeft <= 0 || nowMs < ledNext) return;
  ledOn = !ledOn;
  digitalWrite(PIN_LED, ledOn ? LOW : HIGH);
  blinksLeft--;
  ledNext = nowMs + 150;
}

// ---------------------------------------------------------------- profiles
void applyProfile(const Profile& p, int startPos, int index, bool quiet = false) {
  prof = p;
  marks.clear();
  magnetPos = -1;
  presetIndex = index;
  position = constrain(startPos, 0, p.positions > 0 ? p.positions - 1 : startPos);
  centre = angleNow;
  endstopSince = 0;
  lastMoveMs = millis();
  moveRef = angleNow;
  motion = NONE;
  if (!locked) motorOn();
  Serial.printf("Mode %d: %s (positions %d, start %d)\n", index, p.name, p.positions, position);
  sendLine("MODE," + String(index) + "," + String(p.name));
  if (!quiet) blink(min(index + 1, 6));  // only for the button and USB: the LED costs current
}

int presetByName(const String& name) {
  for (int i = 0; i < NUM_PRESETS; i++)
    if (name.equalsIgnoreCase(PRESETS[i].name)) return i;
  return -1;
}

// ---------------------------------------------------------------- haptics
float endstopFactor(bool inEndstop, uint32_t nowMs) {
  if (!inEndstop) {
    endstopSince = 0;
    return 1.0f;
  }
  if (endstopSince == 0) endstopSince = nowMs;
  float t = (nowMs - endstopSince) / 1000.0f;
  if (t <= ENDSTOP_FULL_S) return 1.0f;
  float f = min(1.0f, (t - ENDSTOP_FULL_S) / ENDSTOP_FADE_S);
  return 1.0f - f * (1.0f - ENDSTOP_HOLD);
}

using feel::hill;  // one detent hill (see feel.h)

// True when the knob sits on an end position and is pushed outwards.
bool beyondEnd(float err) {
  return prof.positions > 0 &&
         ((position == prof.positions - 1 && err > 0) || (position == 0 && err < 0));
}

// Returns the q-axis voltage to apply; sets resting when the knob sits at rest.
float haptics(float angle, float vel, uint32_t nowMs, bool& resting) {
  resting = false;
  float u = 0;

  if (prof.kind == DETENT) {
    float width = rad(prof.widthDeg);
    float err = angle - centre;
    bool canUp = prof.positions == 0 || position < prof.positions - 1;
    bool canDown = prof.positions == 0 || position > 0;
    if (err > width * prof.snap && canUp) {
      centre += width;
      position++;
      err -= width;
      emitPosition(+1);
    } else if (err < -width * prof.snap && canDown) {
      centre -= width;
      position--;
      err += width;
      emitPosition(-1);
    }
    float deadzone = min(width * 0.2f, rad(1.0f));  // SmartKnob's: 20% of the spacing, max 1 deg
    bool detentHere = prof.detentMask == 0 || (position < 32 && (prof.detentMask >> position & 1));
    // Flick to coast on long lists; landmarks stay felt (weaker) as it glides past them.
    bool coastable = prof.positions == 0 || prof.positions > COAST_MIN_POSITIONS;
    float coast = coastable ? feel::coastFactor(fabsf(vel), COAST_FROM, COAST_TO) : 1.0f;
    int nearest = position + (int)roundf(err / width);
    float gain = coast;
    if (marks.has(nearest)) gain = MARK_GAIN * max(coast, MARK_COAST_MIN);
    else if (magnetPos >= 0 && abs(nearest - magnetPos) == 1) gain *= MAGNET_NEIGHBOUR;
    if (beyondEnd(err) && fabsf(err) > deadzone) {
      // The wall is measured from the dead-zone edge, so it starts at zero instead of a step.
      u = -prof.endstop * (err - copysignf(deadzone, err)) * endstopFactor(true, nowMs);
    } else {
      endstopFactor(false, nowMs);
      if (prof.detentMask == 0) {
        // Every position pulls: the hills repeat every spacing (measured from the nearest detent),
        // so the force carries smoothly across a snap instead of flipping in one step.
        u = hill(err - width * roundf(err / width), deadzone, width / 2, prof.strength * gain);
      } else if (detentHere) {
        // Only some positions pull (SmartKnob's magnetic detents): one hill out to the snap point.
        u = hill(err, deadzone, width * prof.snap, prof.strength * gain);
      }
      if (magnetPos >= 0)  // the playing track pulls the knob in (zero at rest, so no current)
        u += feel::magnetPull(angle - (centre + (magnetPos - position) * width), MAGNET_RANGE * width, MAGNET_VOLTS);
      if (prof.strength > 0 && detentHere) resting = fabsf(err) <= deadzone;
      else resting = fabsf(vel) < 0.3f;  // nothing pulls here, so still is enough
    }
    u -= damping * coast * vel;  // no braking while it coasts

  } else if (prof.kind == SMOOTH) {
    float step = rad(prof.widthDeg);
    while (angle - centre > step) { centre += step; position++; emitPosition(+1); }
    while (angle - centre < -step) { centre -= step; position--; emitPosition(-1); }
    u = -prof.strength * vel;
    resting = fabsf(vel) < 0.3f;

  } else {  // ELASTIC
    float defl = angle - centre;
    float range = rad(ELASTIC_RANGE_DEG);
    u = constrain(-prof.strength * defl, -ELASTIC_MAX, ELASTIC_MAX);
    bool inEndstop = fabsf(defl) > range;
    if (inEndstop) u += -prof.endstop * (defl - copysignf(range, defl)) * endstopFactor(true, nowMs);
    else endstopFactor(false, nowMs);
    u -= damping * vel;
    resting = fabsf(defl) < rad(2.0f);
    int jog = (int)constrain(defl / rad(60.0f) * 100.0f, -100.0f, 100.0f);
    if (jog != lastJog && nowMs - lastJogMs >= 100) {
      sendLine("JOG," + String(jog));
      lastJog = jog;
      lastJogMs = nowMs;
    }
  }
  return u;
}

// ---------------------------------------------------------------- motions, lock and direction
// Keep the current position but treat wherever the knob now sits as its detent centre.
void recentre() {
  centre = angleNow;
  moveRef = angleNow;
  endstopSince = 0;
  lastMoveMs = millis();
}

void endMotion(bool grabbed) {
  motion = NONE;
  recentre();
  if (grabbed) {
    sendLine("GRAB");
    Serial.println("Motion stopped: the knob was held.");
  }
}

void startSpin(int dir) {
  if (locked) return;
  motion = SPIN;
  motionFrom = setpoint = angleNow;
  motionTo = angleNow + (dir < 0 ? -TWO_PI : TWO_PI);
  motionStartMs = millis();
  grabSinceMs = 0;
  motorOn();
}

void startRoll(float revPerSec) {
  if (locked) return;
  if (revPerSec == 0) {
    if (motion == ROLL) endMotion(false);
    return;
  }
  if (motion != ROLL) setpoint = angleNow;
  motion = ROLL;
  rollRate = revPerSec * TWO_PI;
  grabSinceMs = 0;
  motorOn();
}

void setLocked(bool on) {
  locked = on;
  if (on) {
    motion = NONE;
    motor.disable();
  } else {
    recentre();  // it may have turned in the pocket: no jump, no positions sent
    motorOn();
  }
  Serial.printf("Pocket lock %s\n", on ? "on" : "off");
}

void startTick(int n = 1, float scale = 1.0f) {
  bool alert = n > 1;
  if (locked && !alert) return;  // the plain click is for unlocking; alerts get through the lock
  tickLeft = constrain(n, 1, 5);
  tickAlert = alert;
  tickScale = constrain(scale, 0.05f, 1.0f);
  tickOn = true;
  tickStartMs = millis();
  motorOn();
}

// The tick's voltage: a short push one way, then the same pull back, so the knob ends where it
// was. An alert repeats that with a pause between.
float tickDrive(uint32_t nowMs) {
  if (!tickOn) return 0;
  uint32_t len = tickAlert ? ALERT_MS : TICK_MS;
  float volts = (tickAlert ? ALERT_VOLTS : TICK_VOLTS) * tickScale;
  uint32_t t = nowMs - tickStartMs;
  if (t < len) return volts;
  if (t < 2 * len) return -volts;
  if (tickLeft <= 1) {
    tickOn = false;
    tickLeft = 0;
  } else if (t >= 2 * len + ALERT_GAP_MS) {
    tickLeft--;
    tickStartMs = nowMs;
  }
  return 0;
}

// Voltage for a running spin or roll, or NAN when there's none. A spring pulls the knob towards a
// moving setpoint, and if a hand keeps it well behind, the motion stops rather than fighting it.
float motionDrive(float angle, float vel, float dt, uint32_t nowMs) {
  float spVel;
  float grabDeg;
  if (motion == SPIN) {
    float t = (nowMs - motionStartMs) / (float)SPIN_MS;
    if (t >= 1.0f) {
      setpoint = motionTo;
      spVel = 0;
      if (fabsf(angle - motionTo) < rad(10.0f) || t > 1.6f) {
        endMotion(false);
        return NAN;
      }
    } else {
      float s = t * t * (3 - 2 * t);  // smoothstep: eases in and out
      setpoint = motionFrom + (motionTo - motionFrom) * s;
      spVel = (motionTo - motionFrom) * 6 * t * (1 - t) * 1000.0f / SPIN_MS;
    }
    grabDeg = SPIN_GRAB_DEG;
  } else if (motion == ROLL) {
    setpoint += rollRate * dt;
    spVel = rollRate;
    grabDeg = ROLL_GRAB_DEG;
  } else {
    return NAN;
  }
  float err = angle - setpoint;
  if (fabsf(err) > rad(grabDeg)) {
    if (grabSinceMs == 0) grabSinceMs = nowMs;
    else if (nowMs - grabSinceMs > GRAB_MS) {
      endMotion(true);
      return NAN;
    }
  } else {
    grabSinceMs = 0;
  }
  return -MOTION_KP * err - MOTION_KD * (vel - spVel);
}

void loadDirection() {
  prefs.begin("knob", false);
  int ks = prefs.getInt("ks", 0);
  if (ks != 1 && ks != -1) {
    // First run of v0.2: start opposite to v0.1, which turned the wrong way for lists.
    ks = -(int)motor.sensor_direction;
    prefs.putInt("ks", ks);
    Serial.println("Direction set opposite to v0.1 and saved (type r to flip it).");
  }
  prefs.end();
  knobSign = ks;
}

void saveDirection() {
  prefs.begin("knob", false);
  prefs.putInt("ks", knobSign);
  prefs.end();
}

void updateThermal(float u, float dt) {
  // Copper loss at stall in voltage mode: phase current = u / R, power = 1.5 * u^2 / R.
  float p = 1.5f * u * u / PHASE_RESISTANCE;
  powerAvg += (p - powerAvg) * min(1.0f, dt / 10.0f);
  if (powerAvg > THERMAL_LIMIT_W) thermalFactor = max(0.4f, thermalFactor - dt * 0.5f);
  else if (powerAvg < 0.8f * THERMAL_LIMIT_W) thermalFactor = min(1.0f, thermalFactor + dt * 0.2f);
}

// ---------------------------------------------------------------- calibration
bool loadCalibration() {
  prefs.begin("knob", false);  // read-write so a missing namespace is created, not an error
  float zea = prefs.getFloat("zea", NAN);
  int dir = prefs.getInt("dir", 0);
  prefs.end();
  if (isnan(zea) || (dir != 1 && dir != -1)) return false;
  motor.zero_electric_angle = zea;
  motor.sensor_direction = dir == 1 ? Direction::CW : Direction::CCW;
  return true;
}

void saveCalibration() {
  prefs.begin("knob", false);
  prefs.putFloat("zea", motor.zero_electric_angle);
  prefs.putInt("dir", (int)motor.sensor_direction);
  prefs.end();
}

// The same sweep SimpleFOC uses to find the sensor direction: one electrical turn forward, then
// back. Logs how far the AS5600 moved. Expect about 360 / pole pairs = 51 degrees each way.
// Returns true when the shaft followed the field well enough to calibrate.
bool sweepTest(float volts) {
  motor.setPhaseVoltage(volts, 0, _3PI_2);
  delay(700);
  sensor.update();
  float a0 = sensor.getAngle();
  for (int i = 0; i <= 500; i++) {
    motor.setPhaseVoltage(volts, 0, _3PI_2 + _2PI * i / 500.0f);
    sensor.update();
    delay(2);
  }
  sensor.update();
  float a1 = sensor.getAngle();
  for (int i = 500; i >= 0; i--) {
    motor.setPhaseVoltage(volts, 0, _3PI_2 + _2PI * i / 500.0f);
    sensor.update();
    delay(2);
  }
  sensor.update();
  float a2 = sensor.getAngle();
  motor.setPhaseVoltage(0, 0, 0);
  delay(200);

  float fwd = deg(a1 - a0), back = deg(a2 - a1);
  float expect = 360.0f / motor.pole_pairs;
  Serial.printf("  sweep at %.1f V: forward %+.1f deg, back %+.1f deg (expect about +/-%.0f)\n",
                volts, fwd, back, expect);
  if (fabsf(fwd) > 5) Serial.printf("  that implies about %.1f pole pairs (set: %d)\n", 360.0f / fabsf(fwd), (int)motor.pole_pairs);
  return fabsf(fwd) > 0.6f * expect && fabsf(back) > 0.6f * expect;
}

bool calibrate(bool forceNew) {
  setSlow(false);
  if (!forceNew && loadCalibration()) {
    Serial.println("Using stored calibration (hold the button 3 s to redo it).");
    return motor.initFOC();
  }
  // Remember what worked, so a failed attempt can't leave the motor half-configured.
  float prevZero = motor.zero_electric_angle;
  Direction prevDir = motor.sensor_direction;

  // initFOC() never switches the driver on, and the nudge needs it on. (After motor.disable() the
  // alignment moved nothing and always failed with "Failed to notice movement".)
  motor.enable();
  Serial.println("Calibrating: the knob will nudge. Don't touch it.");
  for (float volts : ALIGN_TRY) {
    Serial.printf("Calibration attempt at %.1f V\n", volts);
    if (!sweepTest(volts)) continue;  // shaft didn't follow: try a stronger nudge
    motor.voltage_sensor_align = volts;
    motor.zero_electric_angle = NOT_SET;
    motor.sensor_direction = Direction::UNKNOWN;
    if (motor.initFOC()) {
      saveCalibration();
      Serial.printf("Calibration saved: zero %.3f rad, direction %d, nudge %.1f V\n",
                    motor.zero_electric_angle, (int)motor.sensor_direction, volts);
      return true;
    }
  }
  // Failed: put the last good calibration back. Leaving the direction UNKNOWN (0) would make the
  // angle read as zero and drive the motor with a meaningless electrical angle.
  motor.voltage_sensor_align = ALIGN_VOLTS;
  motor.zero_electric_angle = prevZero;
  motor.sensor_direction = prevDir;
  if (prevDir != Direction::UNKNOWN && _isset(prevZero)) {
    motor.initFOC();  // both values set, so this skips the nudge
    Serial.println("Kept the previous calibration.");
  } else {
    motor.disable();
  }
  return false;
}

#if PHASE_TEST
// Drives one phase at a time (that phase at 1.5 V, the other two low) and logs where the shaft
// settles. Each step holds for 3 s, long enough to check with a multimeter: the driven pin should
// read about 1.5 V to GND and the other two about 0 V. With all three phases working, every step
// moves the shaft about 17 deg (120 electrical deg / 7 pole pairs) in the same direction. With
// one phase open, the shaft flips back and forth by about 26 deg and one step doesn't move.
void phaseTest() {
  const char* names[] = {"U", "V", "W"};
  const float v = 1.5f;
  float last = 0;
  Serial.println("Phase test: one phase at a time, 3 s each. Don't touch the knob.");
  for (int step = 0; step < 6; step++) {
    int ph = step % 3;
    driver.setPwm(ph == 0 ? v : 0, ph == 1 ? v : 0, ph == 2 ? v : 0);
    for (int i = 0; i < 300; i++) {  // keep the sensor's turn counting current while it settles
      sensor.update();
      delay(10);
    }
    float a = deg(sensor.getAngle());
    Serial.printf("  %s high: angle %7.1f deg", names[ph], a);
    if (step > 0) Serial.printf("  (moved %+.1f, expect about 17 the same way each time)", a - last);
    Serial.println();
    last = a;
  }
  driver.setPwm(0, 0, 0);
}

// Holds one phase high (1.5 V) and the other two low until told otherwise, so every pin can be
// measured with a multimeter at leisure. Keys over USB serial: u, v, w = hold that phase high,
// 0 = all low, c = carry on booting. A multimeter reads the PWM pins as averages.
void phaseHold() {
  const float v = 1.5f;
  const char names[] = "UVW";
  Serial.println("Phase hold: type u, v or w to hold that phase high, 0 for all low, c to continue.");
  while (true) {
    if (!Serial.available()) {
      delay(10);
      continue;
    }
    char k = tolower(Serial.read());
    if (k == 'c') break;
    int ph = k == 'u' ? 0 : k == 'v' ? 1 : k == 'w' ? 2 : k == '0' ? -1 : -2;
    if (ph == -2) continue;  // newlines and stray keys
    driver.setPwm(ph == 0 ? v : 0, ph == 1 ? v : 0, ph == 2 ? v : 0);
    if (ph < 0) {
      Serial.println("All low. Expect on every phase: xH 0 V, xL 3.3 V, output 0 V.");
      continue;
    }
    Serial.printf("%c high. Expect %cH about 0.9 V, %cL about 2.1 V, %c output about 1.5 V.\n",
                  names[ph], names[ph], names[ph], names[ph]);
    Serial.println("  The other two phases: xH 0 V, xL 3.3 V, output about 0 V.");
  }
  driver.setPwm(0, 0, 0);
}
#endif

// Re-read the sensor after a calibration changed the direction or zero.
void syncAngle() {
  sensor.update();
  angleNow = prevAngle = readKnobAngle();
  knobVel = 0;
  moveRef = angleNow;
}

// ---------------------------------------------------------------- button
bool btnDown = false, longDone = false;
uint32_t btnSince = 0;

void pollButton(uint32_t nowMs) {
  bool down = digitalRead(PIN_BUTTON) == LOW;
  if (down && !btnDown) {
    btnDown = true;
    longDone = false;
    btnSince = nowMs;
  } else if (down && btnDown && !longDone && nowMs - btnSince > 3000) {
    longDone = true;
    if (!calibrate(true)) {
      Serial.println("Calibration failed (see the MOT: lines above). Check the motor wiring, the 5 V rail and any load on the shaft.");
      blink(10);
    }
    syncAngle();
    applyProfile(prof, position, presetIndex);
  } else if (!down && btnDown) {
    btnDown = false;
    if (!longDone && nowMs - btnSince > 30) {
      int next = (presetIndex + 1) % NUM_PRESETS;
      applyProfile(PRESETS[next], PRESETS[next].start, next);
    }
  }
}

// ---------------------------------------------------------------- Pi link: receive
String rxBuf;

// ---------------------------------------------------------------- updates from the Pi
// The Pi can't use the ROM bootloader (EN and BOOT aren't wired to it), so new firmware comes in
// over the normal link: $OTA,BEGIN,<size>,<md5>, then $OTA,DATA,<n>,<base64> chunks, each acked
// before the next, then $OTA,END. It's written to the spare app partition, checked against the
// MD5, and only then made the boot image and restarted into; any failure leaves the running
// firmware as it was. The motor stays off throughout.
bool otaActive = false;
uint32_t otaNext = 0;       // the chunk expected next
uint32_t otaLastMs = 0;
constexpr uint32_t OTA_IDLE_MS = 20000;  // nothing for this long: give up and carry on as before

void otaFail(const char* why) {
  if (otaActive) Update.abort();
  otaActive = false;
  sendLine(String("OTA,ERR,") + why);
}

void handleOta(const String& rest) {
  int c = rest.indexOf(',');
  String sub = c < 0 ? rest : rest.substring(0, c);
  String args = c < 0 ? String("") : rest.substring(c + 1);
  otaLastMs = millis();
  if (sub == "BEGIN") {
    int c2 = args.indexOf(',');
    uint32_t size = (uint32_t)args.substring(0, c2).toInt();
    String md5 = c2 < 0 ? String("") : args.substring(c2 + 1);
    if (otaActive) Update.abort();
    otaActive = false;
    motion = NONE;
    tickOn = false;
    motor.disable();
    setSlow(false);
    if (size == 0 || md5.length() != 32 || !Update.begin(size)) {
      sendLine(String("OTA,ERR,") + (size == 0 || md5.length() != 32 ? "bad request" : Update.errorString()));
      return;
    }
    Update.setMD5(md5.c_str());
    otaActive = true;
    otaNext = 0;
    Serial.printf("Update: receiving %u bytes\n", size);
    sendLine("OTA,READY");
  } else if (sub == "DATA") {
    if (!otaActive) {
      sendLine("OTA,ERR,not started");
      return;
    }
    int c2 = args.indexOf(',');
    uint32_t seq = (uint32_t)args.substring(0, c2).toInt();
    if (seq + 1 == otaNext) {  // a repeat because our ack went missing: ack it again
      sendLine(String("OTA,ACK,") + seq);
      return;
    }
    if (c2 < 0 || seq != otaNext) {
      sendLine(String("OTA,NAK,") + otaNext);
      return;
    }
    String b64 = args.substring(c2 + 1);
    uint8_t buf[320];
    size_t n = 0;
    if (mbedtls_base64_decode(buf, sizeof(buf), &n, (const uint8_t*)b64.c_str(), b64.length()) != 0 || n == 0) {
      sendLine(String("OTA,NAK,") + otaNext);
      return;
    }
    if (Update.write(buf, n) != n) {
      otaFail(Update.errorString());
      return;
    }
    otaNext++;
    sendLine(String("OTA,ACK,") + seq);
  } else if (sub == "END") {
    if (!otaActive) {
      sendLine("OTA,ERR,not started");
      return;
    }
    otaActive = false;
    if (!Update.end(true)) {  // checks the size and the MD5 before switching the boot image
      sendLine(String("OTA,ERR,") + Update.errorString());
      return;
    }
    sendLine("OTA,DONE");
    Serial.println("Update: done, restarting");
    Serial1.flush();
    delay(300);
    ESP.restart();
  } else if (sub == "ABORT") {
    otaFail("aborted");
  }
}

void handleCommand(const String& body) {
  int c1 = body.indexOf(',');
  String cmd = c1 < 0 ? body : body.substring(0, c1);
  String rest = c1 < 0 ? "" : body.substring(c1 + 1);

  if (cmd == "OTA") {
    handleOta(rest);
    return;
  }
  if (otaActive) return;  // nothing else while new firmware comes in
  if (cmd == "PING") {
    sendLine("HELLO,currawong-knob,6");
  } else if (cmd == "SPIN") {
    startSpin(rest.toInt() < 0 ? -1 : 1);
  } else if (cmd == "ROLL") {
    startRoll(rest.toInt() / 1000.0f);
  } else if (cmd == "LOCK") {
    setLocked(rest.toInt() != 0);
  } else if (cmd == "TICK") {
    int vals[2] = {1, 100};  // how many, and how strong (percent)
    feel::parseInts(rest.c_str(), vals, 2);
    startTick(vals[0], vals[1] / 100.0f);
  } else if (cmd == "MARK") {
    int buf[feel::Marks::kMax];
    marks.set(buf, feel::parseInts(rest.c_str(), buf, feel::Marks::kMax));
  } else if (cmd == "MAG") {
    magnetPos = rest.length() ? rest.toInt() : -1;
  } else if (cmd == "RANGE") {
    // New walls and position, but the detent grid stays where it is: nothing moves under the hand.
    int vals[2] = {0, 0};
    feel::parseInts(rest.c_str(), vals, 2);
    prof.positions = max(0, vals[0]);
    if (prof.positions > 0 && prof.endstop <= 0) prof.endstop = RANGE_ENDSTOP;
    position = prof.positions > 0 ? constrain(vals[1], 0, prof.positions - 1) : vals[1];
    endstopSince = 0;
  } else if (cmd == "DIR") {
    piDir = rest.toInt() < 0 ? -1 : 1;
    syncAngle();
    recentre();
  } else if (cmd == "MODE") {
    int i = rest.toInt();
    if (i >= 0 && i < NUM_PRESETS) applyProfile(PRESETS[i], PRESETS[i].start, i);
  } else if (cmd == "FEEL") {
    // The dial's strength as a percent of normal (Settings > Dial feel, 50-150): finer than STR.
    strengthScale = constrain(rest.toInt(), 20, 200) / 100.0f;
    Serial.printf("Strength: %.2f\n", strengthScale);
  } else if (cmd == "STR") {
    int s = constrain(rest.toInt(), 0, 2);
    const float scales[] = {0.7f, 1.0f, 1.3f};
    strengthScale = scales[s];
    Serial.printf("Strength: %.1f\n", strengthScale);
  } else if (cmd == "CFG") {
    int c2 = rest.indexOf(',');
    int c3 = rest.indexOf(',', c2 + 1);
    String kind = c2 < 0 ? rest : rest.substring(0, c2);
    int positions = c2 < 0 ? 0 : rest.substring(c2 + 1, c3).toInt();
    int pos = c3 < 0 ? 0 : rest.substring(c3 + 1).toInt();
    if (kind == "list") {
      Profile p = {"list", DETENT, max(1, positions), 0, LIST_WIDTH_DEG, 0.8f, 0.55f, 12.0f};
      applyProfile(p, pos, presetByName("list8"), true);
    } else {
      int i = presetByName(kind);
      if (i >= 0) applyProfile(PRESETS[i], pos, i, true);
    }
  }
}

void pollSerial() {
  while (Serial1.available()) {
    char ch = (char)Serial1.read();
    if (ch == '\n' || ch == '\r') {
      int s = rxBuf.lastIndexOf('$');
      int star = rxBuf.lastIndexOf('*');
      if (s >= 0 && star > s) {
        String body = rxBuf.substring(s + 1, star);
        uint8_t cs = 0;
        for (size_t i = 0; i < body.length(); i++) cs ^= (uint8_t)body[i];
        if (strtol(rxBuf.substring(star + 1).c_str(), nullptr, 16) == cs) handleCommand(body);
      }
      rxBuf = "";
    } else if (rxBuf.length() < 400) {  // room for a $MARK line with 64 landmarks
      rxBuf += ch;
    } else {
      rxBuf = "";  // junk
    }
  }
}

// ---------------------------------------------------------------- USB console (for trying modes)
String usbBuf;

void printTuning() {
  Serial.printf("%s: strength %.3f, spacing %.2f deg, snap %.2f, endstop %.1f V/rad, damping %.3f\n",
                prof.name, prof.strength, prof.widthDeg, prof.snap, prof.endstop, damping);
}

void printHelp() {
  Serial.println("Modes:");
  for (int i = 0; i < NUM_PRESETS; i++)
    Serial.printf("  %2d %s%s\n", i, PRESETS[i].name, i == presetIndex ? "  <- current" : "");
  Serial.println("Type a number or name to switch. Tune the current mode (until you switch):");
  Serial.println("  s <v>  strength: detent peak volts, smooth drag V per rad/s, elastic V per rad");
  Serial.println("  w <deg> spacing    n <f> snap point    e <v> endstop V/rad    d <v> damping");
  Serial.println("  l      log on/off  r       flip the direction (saved)  ?  this help");
  printTuning();
}

void handleUsb(String line) {
  line.trim();
  if (line.length() == 0) return;
  if (line == "?") return printHelp();
  if (line == "l") {
    logOn = !logOn;
    return;
  }
  if (line == "r") {
    knobSign = -knobSign;
    saveDirection();
    syncAngle();
    recentre();
    Serial.printf("Direction flipped and saved (%d).\n", knobSign);
    return;
  }
  // "<letter> <number>" tunes the current mode.
  if (line.length() > 2 && line[1] == ' ') {
    float v = line.substring(2).toFloat();
    switch (line[0]) {
      case 's': prof.strength = max(0.0f, v); break;
      case 'w': if (v > 0.1f) prof.widthDeg = v; break;
      case 'n': if (v > 0.05f) prof.snap = v; break;
      case 'e': prof.endstop = max(0.0f, v); break;
      case 'd': damping = constrain(v, 0.0f, 0.2f); break;
      default: Serial.println("Unknown setting. Type ? for help."); return;
    }
    return printTuning();
  }
  int i = isDigit(line[0]) ? line.toInt() : presetByName(line);
  if (i < 0 || i >= NUM_PRESETS) {
    Serial.println("No such mode. Type ? for the list.");
    return;
  }
  applyProfile(PRESETS[i], PRESETS[i].start, i);
  printTuning();
}

void pollUsb() {
  while (Serial.available()) {
    char ch = (char)Serial.read();
    if (ch == '\n' || ch == '\r') {
      handleUsb(usbBuf);
      usbBuf = "";
    } else if (usbBuf.length() < 40) {
      usbBuf += ch;
    }
  }
}

// ---------------------------------------------------------------- setup / loop
void setup() {
  Serial.begin(115200);
  SimpleFOCDebug::enable(&Serial);  // prints the MOT: lines that explain a failed calibration
  Serial1.setRxBufferSize(1024);  // a firmware chunk (about 340 bytes) arrives in one go
  Serial1.begin(115200, SERIAL_8N1, PIN_PI_RX, PIN_PI_TX);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, HIGH);
  delay(300);
  Serial.println("\nCurrawong knob v0.6");

  Wire.begin(PIN_SDA, PIN_SCL, 400000);
  Wire.beginTransmission(0x36);
  if (Wire.endTransmission() != 0) {
    Serial.println("AS5600 not found at 0x36. Swap PIN_SDA and PIN_SCL, or check J3.");
    while (true) {  // motor stays off
      digitalWrite(PIN_LED, !digitalRead(PIN_LED));
      delay(100);
    }
  }
  sensor.init(&Wire);
#if SENSOR_SELFTEST
  for (int i = 0; i < 20; i++) {  // turn the knob by hand: the angle must change
    sensor.update();
    Serial.printf("AS5600 angle: %.1f deg\n", deg(sensor.getAngle()));
    delay(250);
  }
#endif

  driver.voltage_power_supply = SUPPLY_VOLTS;
  driver.voltage_limit = SUPPLY_VOLTS;
  driver.pwm_frequency = 25000;  // above hearing
  driver.dead_zone = 0.05f;
  if (!driver.init()) {
    Serial.println("Driver init failed.");
    while (true) delay(1000);
  }

  motor.linkDriver(&driver);
  motor.linkSensor(&sensor);
  motor.controller = MotionControlType::torque;
  motor.torque_controller = TorqueControlType::voltage;
  motor.foc_modulation = FOCModulationType::SpaceVectorPWM;
  motor.voltage_limit = VOLTAGE_LIMIT;
  motor.voltage_sensor_align = ALIGN_VOLTS;
  motor.init();
#if PHASE_TEST
  phaseTest();
  phaseHold();
#endif

  if (!calibrate(false)) {
    Serial.println("Calibration failed (see the MOT: lines above). Check the motor wiring, the 5 V rail and any load on the shaft.");
    motor.disable();
    while (true) {
      digitalWrite(PIN_LED, !digitalRead(PIN_LED));
      delay(400);
    }
  }

  loadDirection();
  motor.loopFOC();
  angleNow = prevAngle = readKnobAngle();
  lastUs = micros();
  applyProfile(PRESETS[0], PRESETS[0].start, 0);
  sendLine("HELLO,currawong-knob,6");
  Serial.println("Type ? in this monitor for the modes and tuning commands.");
}

void loop() {
  if (otaActive) {  // new firmware coming in: the motor's off, and nothing else runs
    pollSerial();
    if (millis() - otaLastMs > OTA_IDLE_MS) otaFail("timed out");
    return;
  }
  uint32_t nowUs = micros();
  float dt = (nowUs - lastUs) * 1e-6f;
  lastUs = nowUs;
  if (dt <= 0 || dt > 0.05f) dt = 0.001f;
  uint32_t nowMs = millis();

  motor.loopFOC();  // also keeps the sensor updated while the motor is off
  angleNow = readKnobAngle();
  knobVel += ((angleNow - prevAngle) / dt - knobVel) * min(1.0f, dt / 0.01f);
  prevAngle = angleNow;

  // Pocket lock: keep reading the sensor (so unlocking doesn't jump) but drive nothing, send nothing.
  if (locked) {
    float t = tickDrive(nowMs);  // an alert still gets through
    if (tickOn && motor.sensor_direction != Direction::UNKNOWN) {
      motorOn();
      motor.move(toMotor(constrain(t, -VOLTAGE_LIMIT, VOLTAGE_LIMIT)));
    } else {
      if (motor.enabled) motor.disable();
      motor.move(0);
    }
    lastU = tickOn ? t : 0;
    pollButton(nowMs);
    pollSerial();
    pollUsb();
    updateLed(nowMs);
    if (!tickOn) {
      setSlow(true);
      delay(IDLE_LOOP_MS);
    }
    return;
  }

  // SmartKnob's idle correction: once the knob has sat still for half a second near a detent,
  // ease the detent centre onto where it actually rests. Otherwise friction or cogging can hold it
  // just outside the dead zone, where the motor keeps pushing and can buzz.
  idleVel += (knobVel - idleVel) * min(1.0f, dt / 0.2f);
  if (fabsf(idleVel) > 0.05f) stillSinceMs = 0;
  else if (stillSinceMs == 0) stillSinceMs = nowMs;
  float drift = angleNow - centre;
  if (motion == NONE && prof.kind == DETENT && stillSinceMs != 0 && nowMs - stillSinceMs > 500 && !beyondEnd(drift) &&
      fabsf(drift) < min(rad(5.0f), rad(prof.widthDeg) / 2))
    centre += drift * min(1.0f, dt / 1.0f);

  bool resting = false;
  float u;
  float drive = motionDrive(angleNow, knobVel, dt, nowMs);
  if (!isnan(drive)) {
    u = drive * thermalFactor;  // a spin or roll: no detents, and no positions sent to the Pi
    lastMoveMs = nowMs;
  } else {
    u = haptics(angleNow, knobVel, nowMs, resting) * strengthScale * thermalFactor;
  }
  float tick = tickDrive(nowMs);
  if (tick != 0) {
    u += tick;
    resting = false;
  }
  u = constrain(u, -VOLTAGE_LIMIT, VOLTAGE_LIMIT);
  if (fabsf(knobVel) > RUNAWAY_RAD_S) u = 0;

  // Idle: the motor goes off once the knob has stayed within 1.5 deg for IDLE_OFF_MS, and back on
  // at the first bigger movement. (v0.2 also waited for the knob to sit within 1 deg of a detent
  // centre, which friction could prevent indefinitely, leaving the motor on.) Only the elastic
  // mode waits until it's back at centre, or letting go would drop the spring mid-twist.
  if (fabsf(angleNow - moveRef) > rad(1.5f)) {
    moveRef = angleNow;
    lastMoveMs = nowMs;
    motorOn();
  }
  bool canRest = motion == NONE && !tickOn && (prof.kind != ELASTIC || resting);
  if (motor.enabled && canRest && nowMs - lastMoveMs > IDLE_OFF_MS) motor.disable();
  if (!motor.enabled) u = 0;

  if (motor.sensor_direction == Direction::UNKNOWN) u = 0;  // no valid calibration: never drive
  updateThermal(u, dt);
  motor.move(toMotor(u));  // the knob's frame -> SimpleFOC's
  lastU = u;

  pollButton(nowMs);
  pollSerial();
  pollUsb();
  updateLed(nowMs);

  if (logOn && nowMs - lastLogMs > 1000) {
    lastLogMs = nowMs;
    Serial.printf("%-17s pos %4d  angle %7.1f deg  u %5.2f V  avg %4.2f W  derate %.2f  %s%s\n",
                  prof.name, position, deg(angleNow), lastU, powerAvg, thermalFactor,
                  motor.enabled ? "on" : slow ? "idle 80MHz" : "idle",
                  motion == SPIN ? "  spin" : motion == ROLL ? "  roll" : "");
  }

  // Nothing to drive: sleep between sensor checks at 80 MHz (the FreeRTOS idle task halts the CPU).
  if (!motor.enabled && motion == NONE && !tickOn) {
    setSlow(true);
    delay(IDLE_LOOP_MS);
  }
}
