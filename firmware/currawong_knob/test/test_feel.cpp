// Host tests for src/feel.h:  g++ -std=c++17 -Wall -Isrc test/test_feel.cpp -o /tmp/tf && /tmp/tf
#include <cstdio>
#include "feel.h"

static int fails = 0;
#define CHECK(cond, what) do { bool ok_ = (cond); std::printf("  %s: %s\n", ok_ ? "pass" : "FAIL", what); if (!ok_) fails++; } while (0)
static float rad(float d) { return d * 0.017453293f; }

int main() {
  using namespace feel;
  std::puts("== coasting ==");
  CHECK(coastFactor(2, 6, 14) == 1.0f && coastFactor(20, 6, 14) == 0.0f, "full clicks when slow, none when flung");
  bool mono = true; float prev = 2;
  for (float v = 0; v < 20; v += 0.1f) { float c = coastFactor(v, 6, 14); if (c > prev + 1e-6f) mono = false; prev = c; }
  CHECK(mono, "fades steadily as the speed rises");
  CHECK(fabsf(coastFactor(10, 6, 14) - 0.5f) < 1e-4f, "halfway at the middle speed");

  std::puts("== magnet ==");
  float reach = rad(40);
  CHECK(magnetPull(0, reach, 0.6f) == 0 && magnetPull(reach, reach, 0.6f) == 0 && magnetPull(-reach * 1.2f, reach, 0.6f) == 0,
        "no force at its centre or beyond its reach");
  CHECK(magnetPull(rad(10), reach, 0.6f) < 0 && magnetPull(rad(-10), reach, 0.6f) > 0, "always pulls towards the centre");
  CHECK(fabsf(magnetPull(reach / 2, reach, 0.6f) + 0.6f) < 1e-4f, "strongest halfway out");

  // Statics with the firmware's numbers: list spacing 20 deg, 1 deg dead zone, 0.8 V detents, the two
  // detents beside the magnet weakened to 0.45, magnet 0.6 V reaching 2 positions. Released anywhere
  // from the neighbour's centre to where home takes over, the net push must point home (and by a
  // margin, for friction); released on the detent two away, it must stay put.
  const float w = rad(20), dz = rad(1), strength = 0.8f, neighbour = 0.45f, mag = 0.6f, reach2 = 2 * w;
  float worst = 1e9f;
  for (float e = 0; e <= 0.55f * w; e += rad(0.1f)) {
    float hillForce = hill(-e, dz, w / 2, strength * neighbour);  // neighbour's hill, displaced e towards home
    float pull = magnetPull(w - e, reach2, mag);                   // home is at -w from the neighbour
    float net = -pull - hillForce;                                 // positive = towards home
    if (net < worst) worst = net;
  }
  CHECK(worst > 0.15f, "one position away, it slides into the playing track");
  std::printf("        (smallest push towards home: %.2f V)\n", worst);
  CHECK(magnetPull(2 * w, reach2, mag) == 0, "two positions away, it stays where it was left");

  std::puts("== hills ==");
  CHECK(hill(dz * 0.5f, dz, w / 2, strength) == 0 && hill(w / 2, dz, w / 2, strength) == 0, "nothing in the dead zone or at the edge");
  CHECK(hill(rad(5), dz, w / 2, strength) < 0 && hill(rad(-5), dz, w / 2, strength) > 0, "pushes back towards the detent");

  std::puts("== landmarks ==");
  Marks m; int in[] = {42, 3, 17, 3, -5, 0};
  m.set(in, 6);
  CHECK(m.n == 4 && m.v[0] == 0 && m.v[1] == 3 && m.v[2] == 17 && m.v[3] == 42, "sorted, duplicates and negatives dropped");
  CHECK(m.has(17) && m.has(0) && !m.has(16) && !m.has(-1) && !m.has(1000), "finds marked positions only");
  int many[100]; for (int i = 0; i < 100; i++) many[i] = i * 3;
  m.set(many, 100);
  CHECK(m.n == Marks::kMax && m.has(189) && !m.has(192), "stops at 64 marks");

  std::puts("== parsing ==");
  int out[8];
  CHECK(parseInts("3,17,-1,42", out, 8) == 4 && out[0] == 3 && out[2] == -1 && out[3] == 42, "reads a list");
  CHECK(parseInts("", out, 8) == 0 && parseInts("5,x,6", out, 8) == 1, "stops at junk");
  CHECK(parseInts("1,2,3,4,5,6,7,8,9,10", out, 8) == 8, "never writes past the buffer");

  std::puts("== $TICK arguments ==");
  {
    int v[2] = {1, 100}; parseInts("", v, 2);
    CHECK(v[0] == 1 && v[1] == 100, "$TICK: one click, full strength (as before v0.4)");
    int w[2] = {1, 100}; parseInts("3", w, 2);
    CHECK(w[0] == 3 && w[1] == 100, "$TICK,3: a three-tick alert, full strength (as before)");
    int x[2] = {1, 100}; parseInts("1,30", x, 2);
    CHECK(x[0] == 1 && x[1] == 30, "$TICK,1,30: one soft button click");
  }

  std::printf("\n%s\n", fails ? "SOME FAILED" : "ALL PASSED");
  return fails ? 1 : 0;
}
